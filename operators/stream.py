from __future__ import annotations

import time

import bpy
import gpu
try:
    import numpy as np
except Exception:  # pragma: no cover - Blender runtime dependency
    np = None

from ..bridge.client import get_bridge_client
from ..bridge.frame_sender import get_frame_sender, shutdown_frame_sender

STATE_POLL_INTERVAL_SEC = 0.1
SETTLE_DELAY_SEC = 0.35
REDRAW_RETRY_INTERVAL_SEC = 0.05
MIN_CAPTURE_INTERVAL_SEC = 0.2

_DRAW_HANDLER = None
_LAST_CAPTURE_AT = 0.0
_LAST_DIRTY_AT = 0.0
_PENDING_CAPTURE = False
_LAST_VIEW_SIGNATURE = None
_DEPSGRAPH_HANDLER = None
_CAPTURE_COLOR_SLOT = None
_OFFSCREEN = None
_OFFSCREEN_SIZE = (0, 0)
_CAPTURE_BACKEND = None
_OFFSCREEN_LAYOUT_LOGGED = False


def _as_optional_int(value):
    if value is None:
        return None
    return int(value)


def _iter_view3d_window_regions():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    yield region


def _tag_stream_regions_redraw() -> None:
    for region in _iter_view3d_window_regions():
        region.tag_redraw()


def _build_view_signature():
    ctx = bpy.context
    screen = getattr(ctx, "screen", None)
    if screen is None:
        return None
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        space = getattr(area, "spaces", None)
        if space is None:
            continue
        view3d = getattr(space, "active", None)
        if view3d is None or getattr(view3d, "type", None) != "VIEW_3D":
            continue
        region3d = getattr(view3d, "region_3d", None)
        if region3d is None:
            continue
        window_region = None
        for region in area.regions:
            if region.type == "WINDOW":
                window_region = region
                break
        if window_region is None:
            continue

        view_rot = getattr(region3d, "view_rotation", None)
        view_loc = getattr(region3d, "view_location", None)
        if view_rot is None or view_loc is None:
            continue

        scene = getattr(ctx, "scene", None)
        render = getattr(scene, "render", None)
        engine = getattr(render, "engine", "")
        return (
            round(float(view_loc.x), 4),
            round(float(view_loc.y), 4),
            round(float(view_loc.z), 4),
            round(float(view_rot.x), 4),
            round(float(view_rot.y), 4),
            round(float(view_rot.z), 4),
            round(float(view_rot.w), 4),
            round(float(getattr(region3d, "view_distance", 0.0)), 4),
            str(getattr(region3d, "view_perspective", "")),
            str(getattr(view3d, "shading", None).type if getattr(view3d, "shading", None) else ""),
            round(float(getattr(view3d, "lens", 0.0)), 3),
            str(engine),
            int(getattr(window_region, "width", 0)),
            int(getattr(window_region, "height", 0)),
        )
    return None


def _mark_stream_dirty() -> None:
    global _LAST_DIRTY_AT
    _LAST_DIRTY_AT = time.monotonic()


def _request_capture_now() -> None:
    global _PENDING_CAPTURE, _LAST_DIRTY_AT
    _PENDING_CAPTURE = True
    _LAST_DIRTY_AT = 0.0
    _tag_stream_regions_redraw()


def _on_depsgraph_update(scene, depsgraph) -> None:
    sender = get_frame_sender()
    if not sender.is_streaming:
        return
    if len(getattr(depsgraph, "updates", [])) == 0:
        return
    _mark_stream_dirty()


def _stream_state_timer():
    global _LAST_VIEW_SIGNATURE

    sender = get_frame_sender()
    if not sender.is_streaming:
        return None

    status = get_bridge_client().get_status()
    if status.get("state") != "streaming":
        return STATE_POLL_INTERVAL_SEC

    current_signature = _build_view_signature()
    if _LAST_VIEW_SIGNATURE is None:
        _LAST_VIEW_SIGNATURE = current_signature
    elif current_signature != _LAST_VIEW_SIGNATURE:
        _LAST_VIEW_SIGNATURE = current_signature
        _mark_stream_dirty()

    now = time.monotonic()
    if _PENDING_CAPTURE:
        _tag_stream_regions_redraw()
        return REDRAW_RETRY_INTERVAL_SEC

    if _LAST_DIRTY_AT > 0.0:
        settled = (now - _LAST_DIRTY_AT) >= SETTLE_DELAY_SEC
        cooled_down = (now - _LAST_CAPTURE_AT) >= MIN_CAPTURE_INTERVAL_SEC
        if settled and cooled_down:
            _request_capture_now()
            return REDRAW_RETRY_INTERVAL_SEC

    return STATE_POLL_INTERVAL_SEC


def _capture_draw_callback():
    global _LAST_CAPTURE_AT, _PENDING_CAPTURE

    sender = get_frame_sender()
    if not sender.is_streaming:
        return

    status = get_bridge_client().get_status()
    if status.get("state") != "streaming":
        return

    if not _PENDING_CAPTURE:
        return

    region = getattr(bpy.context, "region", None)
    if region is None or region.type != "WINDOW":
        return
    width = int(getattr(region, "width", 0))
    height = int(getattr(region, "height", 0))
    if width <= 0 or height <= 0:
        return

    try:
        pixels = _capture_with_offscreen(width, height)
        if pixels is None:
            frame_buffer = gpu.state.active_framebuffer_get()
            if frame_buffer is None:
                return
            pixels = _capture_best_color_bytes(frame_buffer, width, height)
        if pixels is None:
            return
        sender.send_rgba_frame(width=width, height=height, pixels=pixels)
        _LAST_CAPTURE_AT = time.monotonic()
        _PENDING_CAPTURE = False
    except Exception as exc:
        _PENDING_CAPTURE = False
        print(f"[SutuBridge] 采集视口帧失败: {exc}")


def _ensure_offscreen(width: int, height: int):
    global _OFFSCREEN, _OFFSCREEN_SIZE
    if width <= 0 or height <= 0:
        return None
    if _OFFSCREEN is not None and _OFFSCREEN_SIZE == (width, height):
        return _OFFSCREEN
    _free_offscreen()
    try:
        _OFFSCREEN = gpu.types.GPUOffScreen(width, height, format="RGBA8")
        _OFFSCREEN_SIZE = (width, height)
        return _OFFSCREEN
    except Exception as exc:
        print(f"[SutuBridge] 创建 offscreen 失败: {exc}")
        _OFFSCREEN = None
        _OFFSCREEN_SIZE = (0, 0)
        return None


def _free_offscreen() -> None:
    global _OFFSCREEN, _OFFSCREEN_SIZE
    offscreen = _OFFSCREEN
    _OFFSCREEN = None
    _OFFSCREEN_SIZE = (0, 0)
    if offscreen is None:
        return
    try:
        offscreen.free()
    except Exception:
        pass


def _capture_with_offscreen(width: int, height: int):
    global _CAPTURE_BACKEND

    context = bpy.context
    space = getattr(context, "space_data", None)
    region = getattr(context, "region", None)
    region_data = getattr(context, "region_data", None)
    if (
        space is None
        or region is None
        or region_data is None
        or getattr(space, "type", "") != "VIEW_3D"
        or getattr(region, "type", "") != "WINDOW"
    ):
        return None

    projection_matrix = getattr(region_data, "window_matrix", None)
    if projection_matrix is None:
        projection_matrix = getattr(region_data, "perspective_matrix", None)
    view_matrix = getattr(region_data, "view_matrix", None)
    if projection_matrix is None or view_matrix is None:
        return None

    offscreen = _ensure_offscreen(width, height)
    if offscreen is None:
        return None

    try:
        offscreen.draw_view3d(
            context.scene,
            context.view_layer,
            space,
            region,
            view_matrix.copy(),
            projection_matrix.copy(),
            do_color_management=True,
        )
        texture_data = offscreen.texture_color.read()
        pixels = _pack_offscreen_texture_to_bytes(texture_data, width, height)
    except Exception as exc:
        print(f"[SutuBridge] offscreen 采集失败: {exc}")
        return None

    if pixels is None:
        return None

    if _CAPTURE_BACKEND != "offscreen":
        _CAPTURE_BACKEND = "offscreen"
        print("[SutuBridge] 采集后端: offscreen")
    return pixels


def _pack_offscreen_texture_to_bytes(texture_data, width: int, height: int):
    global _OFFSCREEN_LAYOUT_LOGGED

    expected_len = width * height * 4
    if expected_len <= 0:
        return None

    if np is not None:
        try:
            arr = _reshape_offscreen_array(texture_data, width, height, expected_len)
            if arr is None:
                return None

            # Match BlenderLayer's send pipeline: Fortran flatten + reshape + vertical flip.
            packed = (
                np.array(arr, copy=False, dtype=np.uint8)
                .ravel(order="F")
                .reshape(height, width, 4)[::-1, :, :4]
            )
            packed = _fix_suspicious_rgab_layout(packed, width, height)

            # Bridge viewport is expected as a normal paint layer snapshot (opaque).
            packed[:, :, 3] = 255
            if not _OFFSCREEN_LAYOUT_LOGGED:
                print(f"[SutuBridge] offscreen buffer shape={tuple(arr.shape)} np_pack=fortran_flip")
                _OFFSCREEN_LAYOUT_LOGGED = True
            return packed.tobytes()
        except Exception:
            pass

    try:
        raw = bytes(texture_data)
    except Exception:
        return None
    if len(raw) < expected_len:
        return None
    flipped = bytearray(_flip_rgba_rows(raw[:expected_len], width, height))
    flipped[3::4] = b"\xff" * (len(flipped) // 4)
    return bytes(flipped)


def _reshape_offscreen_array(texture_data, width: int, height: int, expected_len: int):
    if np is None:
        return None

    arr = np.array(texture_data, copy=False, dtype=np.uint8)
    if arr.size < expected_len:
        return None
    if arr.ndim == 1:
        return arr[:expected_len].reshape(height, width, 4)
    if arr.ndim == 2 and arr.shape[1] == 4:
        return arr[: width * height, :].reshape(height, width, 4)
    if arr.ndim >= 3:
        arr = arr[:height, :width, :4]
        if arr.shape[0] != height or arr.shape[1] != width or arr.shape[2] < 4:
            return None
        return arr
    return None


def _fix_suspicious_rgab_layout(packed, width: int, height: int):
    if np is None:
        return packed

    # Some Blender/GPU combinations expose offscreen channels as RGAB-like layout.
    # Detect the suspicious signature and swap to RGBA.
    sample = packed[:: max(1, height // 256), :: max(1, width // 256), :]
    alpha_mean = float(sample[:, :, 3].mean())
    ch2_mean = float(sample[:, :, 2].mean())
    if alpha_mean < 96.0 and ch2_mean > 200.0:
        print("[SutuBridge] offscreen 检测到通道异常，已应用 RGAB->RGBA 修正")
        return packed[:, :, [0, 1, 3, 2]]
    return packed


def _flip_rgba_rows(pixels: bytes, width: int, height: int) -> bytes:
    row_bytes = width * 4
    if row_bytes <= 0 or height <= 0:
        return pixels
    flipped = bytearray(len(pixels))
    for y in range(height):
        src_start = (height - 1 - y) * row_bytes
        dst_start = y * row_bytes
        flipped[dst_start : dst_start + row_bytes] = pixels[src_start : src_start + row_bytes]
    return bytes(flipped)


def _read_color_bytes(frame_buffer, width: int, height: int, slot: int):
    try:
        rgba_buffer = frame_buffer.read_color(0, 0, width, height, 4, slot, "UBYTE")
        data = bytes(rgba_buffer)
        if len(data) != width * height * 4:
            return None
        return data
    except Exception:
        return None


def _estimate_signal_score(pixels: bytes) -> float:
    total_pixels = len(pixels) // 4
    if total_pixels <= 0:
        return 0.0

    sample_count = min(8192, total_pixels)
    step = max(1, total_pixels // sample_count)
    sampled = 0
    non_zero_rgb = 0
    rgb_sum = 0
    for pixel_index in range(0, total_pixels, step):
        i = pixel_index * 4
        r = pixels[i]
        g = pixels[i + 1]
        b = pixels[i + 2]
        rgb_sum += r + g + b
        if r != 0 or g != 0 or b != 0:
            non_zero_rgb += 1
        sampled += 1
        if sampled >= sample_count:
            break

    if sampled <= 0:
        return 0.0
    non_zero_ratio = non_zero_rgb / sampled
    mean_rgb = rgb_sum / (sampled * 3 * 255.0)
    return non_zero_ratio * 0.8 + mean_rgb * 0.2


def _capture_best_color_bytes(frame_buffer, width: int, height: int):
    global _CAPTURE_COLOR_SLOT, _CAPTURE_BACKEND

    if _CAPTURE_COLOR_SLOT is not None:
        cached_slot = _as_optional_int(_CAPTURE_COLOR_SLOT)
        cached = _read_color_bytes(frame_buffer, width, height, cached_slot) if cached_slot is not None else None
        if cached is not None:
            return cached
        _CAPTURE_COLOR_SLOT = None

    best_slot = None
    best_pixels = None
    best_score = -1.0
    for slot in (0, 1, 2, 3):
        pixels = _read_color_bytes(frame_buffer, width, height, slot)
        if pixels is None:
            continue
        score = _estimate_signal_score(pixels)
        if score > best_score:
            best_score = score
            best_slot = slot
            best_pixels = pixels

    if best_pixels is None:
        return None
    if _CAPTURE_COLOR_SLOT != best_slot:
        _CAPTURE_COLOR_SLOT = best_slot
        print(f"[SutuBridge] 采集颜色附件 slot={best_slot}, signal_score={best_score:.4f}")
    if _CAPTURE_BACKEND != "active_framebuffer":
        _CAPTURE_BACKEND = "active_framebuffer"
        print("[SutuBridge] 采集后端: active_framebuffer")
    return best_pixels


def _ensure_stream_hooks() -> None:
    global _DRAW_HANDLER, _DEPSGRAPH_HANDLER
    if _DRAW_HANDLER is None:
        _DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            _capture_draw_callback,
            (),
            "WINDOW",
            "POST_PIXEL",
        )
    if _DEPSGRAPH_HANDLER is None:
        _DEPSGRAPH_HANDLER = _on_depsgraph_update
        bpy.app.handlers.depsgraph_update_post.append(_DEPSGRAPH_HANDLER)
    if not bpy.app.timers.is_registered(_stream_state_timer):
        bpy.app.timers.register(_stream_state_timer, persistent=True)


def _remove_stream_hooks() -> None:
    global _DRAW_HANDLER, _DEPSGRAPH_HANDLER
    if _DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLER, "WINDOW")
        except Exception:
            pass
        _DRAW_HANDLER = None
    if _DEPSGRAPH_HANDLER is not None:
        try:
            bpy.app.handlers.depsgraph_update_post.remove(_DEPSGRAPH_HANDLER)
        except Exception:
            pass
        _DEPSGRAPH_HANDLER = None
    if bpy.app.timers.is_registered(_stream_state_timer):
        bpy.app.timers.unregister(_stream_state_timer)
    _free_offscreen()


def _reset_stream_state() -> None:
    global _LAST_CAPTURE_AT
    global _LAST_DIRTY_AT
    global _PENDING_CAPTURE
    global _LAST_VIEW_SIGNATURE
    global _CAPTURE_COLOR_SLOT
    global _CAPTURE_BACKEND
    global _OFFSCREEN_LAYOUT_LOGGED
    _LAST_CAPTURE_AT = 0.0
    _LAST_DIRTY_AT = 0.0
    _PENDING_CAPTURE = False
    _LAST_VIEW_SIGNATURE = None
    _CAPTURE_COLOR_SLOT = None
    _CAPTURE_BACKEND = None
    _OFFSCREEN_LAYOUT_LOGGED = False
    _free_offscreen()


class SUTU_OT_bridge_start_stream(bpy.types.Operator):
    bl_idname = "sutu_bridge.start_stream"
    bl_label = "Start Stream"
    bl_description = "开始推送 Blender 视口帧"

    def execute(self, context: bpy.types.Context):
        client = get_bridge_client()
        status = client.get_status()
        if status.get("state") != "streaming":
            self.report({"ERROR"}, "请先连接 Sutu Bridge")
            return {"CANCELLED"}

        sender = get_frame_sender()
        sender.start_stream(stream_id=None)
        _reset_stream_state()
        _ensure_stream_hooks()
        _request_capture_now()
        self.report({"INFO"}, "已开始推流")
        return {"FINISHED"}


class SUTU_OT_bridge_stop_stream(bpy.types.Operator):
    bl_idname = "sutu_bridge.stop_stream"
    bl_label = "Stop Stream"
    bl_description = "停止推送 Blender 视口帧"

    def execute(self, context: bpy.types.Context):
        sender = get_frame_sender()
        sender.stop_stream(reason="user_stopped")
        _remove_stream_hooks()
        _reset_stream_state()
        self.report({"INFO"}, "已停止推流")
        return {"FINISHED"}


def unregister() -> None:
    _remove_stream_hooks()
    _reset_stream_state()
    shutdown_frame_sender()
