from __future__ import annotations

import os
import tempfile
import time

import bpy
import gpu
try:
    import numpy as np
except Exception:  # pragma: no cover - Blender runtime dependency
    np = None

from ..bridge.client import get_addon_preferences, get_bridge_client
from ..bridge.frame_sender import get_frame_sender, shutdown_frame_sender
from ..bridge.messages import BRIDGE_TRANSPORT_SHM

STATE_POLL_INTERVAL_SEC = 0.1
SETTLE_DELAY_SEC = 0.35
REDRAW_RETRY_INTERVAL_SEC = 0.05
MIN_CAPTURE_INTERVAL_SEC = 0.2
ONE_SHOT_SHM_STOP_DELAY_SEC = 0.5

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
_DOWNSCALE_LOG_SIGNATURE = None
_DOWNSCALE_INDEX_CACHE = {}
_WAITING_HINT_LOGGED = False
_RENDER_SEND_PENDING = False
_DRAW_VIEW3D_OVERLAY_KW_SUPPORTED = None


def _show_bridge_popup(message: str, icon: str = "INFO") -> None:
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return

    def _draw(self, _context):
        self.layout.label(text=message)

    try:
        window_manager.popup_menu(_draw, title="Sutu Bridge", icon=icon)
    except Exception:
        pass


def _as_optional_int(value):
    if value is None:
        return None
    return int(value)


def _as_positive_int(value):
    if value is None:
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


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


def _iter_view3d_window_contexts():
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
            space = getattr(area.spaces, "active", None)
            if space is None or getattr(space, "type", "") != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    yield window, area, region, space


def _tag_stream_regions_redraw() -> None:
    for region in _iter_view3d_window_regions():
        region.tag_redraw()


def _stop_live_stream_for_one_shot(reason: str) -> None:
    sender = get_frame_sender()
    if sender.is_streaming:
        sender.stop_stream(reason=reason)
    _remove_stream_hooks()
    _reset_stream_state()


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


def _get_stream_target_hint() -> tuple[int | None, int | None]:
    hint_width_raw, hint_height_raw = get_bridge_client().get_stream_target_size_hint()
    return _as_positive_int(hint_width_raw), _as_positive_int(hint_height_raw)


def _ensure_stream_target_hint_ready() -> bool:
    global _WAITING_HINT_LOGGED
    hint_width, hint_height = _get_stream_target_hint()
    if hint_width is not None and hint_height is not None:
        _WAITING_HINT_LOGGED = False
        return True
    if not _WAITING_HINT_LOGGED:
        _WAITING_HINT_LOGGED = True
        print("[SutuBridge] waiting stream target hint from Sutu before first frame")
    return False


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
        if not _ensure_stream_target_hint_ready():
            return STATE_POLL_INTERVAL_SEC
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

    if not _ensure_stream_target_hint_ready():
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
        width, height, pixels = _downscale_for_stream(width, height, pixels)
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
        _draw_offscreen_view3d(
            offscreen=offscreen,
            context=context,
            space=space,
            region=region,
            view_matrix=view_matrix.copy(),
            projection_matrix=projection_matrix.copy(),
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


def _is_film_transparent_enabled(context) -> bool:
    scene = getattr(context, "scene", None)
    render = getattr(scene, "render", None)
    return bool(getattr(render, "film_transparent", False))


def _draw_offscreen_view3d(
    offscreen,
    context,
    space,
    region,
    view_matrix,
    projection_matrix,
) -> None:
    global _DRAW_VIEW3D_OVERLAY_KW_SUPPORTED

    draw_background = None
    if _is_film_transparent_enabled(context):
        draw_background = False

    def _draw_once(disable_overlay: bool, use_draw_background: bool) -> None:
        kwargs = {"do_color_management": True}
        if use_draw_background and draw_background is not None:
            kwargs["draw_background"] = draw_background
        if disable_overlay:
            kwargs["draw_overlays"] = False
            kwargs["draw_gizmo"] = False
        offscreen.draw_view3d(
            context.scene,
            context.view_layer,
            space,
            region,
            view_matrix,
            projection_matrix,
            **kwargs,
        )

    if _DRAW_VIEW3D_OVERLAY_KW_SUPPORTED is not False:
        try:
            _draw_once(disable_overlay=True, use_draw_background=True)
            _DRAW_VIEW3D_OVERLAY_KW_SUPPORTED = True
            return
        except TypeError:
            _DRAW_VIEW3D_OVERLAY_KW_SUPPORTED = False

    if draw_background is not None:
        try:
            _draw_once(disable_overlay=False, use_draw_background=True)
            return
        except TypeError:
            pass

    _draw_once(disable_overlay=False, use_draw_background=False)


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
    return _flip_rgba_rows(raw[:expected_len], width, height)


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


def _target_stream_size(width: int, height: int) -> tuple[int, int]:
    hint_width, hint_height = _get_stream_target_hint()

    width_limit = width
    height_limit = height
    if hint_width is not None:
        width_limit = min(width_limit, hint_width)
    if hint_height is not None:
        height_limit = min(height_limit, hint_height)

    if width_limit <= 0 or height_limit <= 0:
        return width, height

    scale = min(1.0, float(width_limit) / float(width), float(height_limit) / float(height))
    if scale >= 0.9999:
        return width, height
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    return target_width, target_height


def _downscale_for_stream(width: int, height: int, pixels: bytes) -> tuple[int, int, bytes]:
    global _DOWNSCALE_LOG_SIGNATURE
    if np is None:
        return width, height, pixels

    target_width, target_height = _target_stream_size(width, height)
    if target_width == width and target_height == height:
        return width, height, pixels

    expected = width * height * 4
    if len(pixels) < expected:
        return width, height, pixels
    try:
        frame = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
        cache_key = (width, height, target_width, target_height)
        index_pair = _DOWNSCALE_INDEX_CACHE.get(cache_key)
        if index_pair is None:
            x_idx = (np.arange(target_width, dtype=np.int32) * width) // target_width
            y_idx = (np.arange(target_height, dtype=np.int32) * height) // target_height
            index_pair = (x_idx, y_idx)
            _DOWNSCALE_INDEX_CACHE[cache_key] = index_pair
            if len(_DOWNSCALE_INDEX_CACHE) > 8:
                _DOWNSCALE_INDEX_CACHE.clear()
                _DOWNSCALE_INDEX_CACHE[cache_key] = index_pair
        x_idx, y_idx = index_pair
        downscaled = np.ascontiguousarray(frame[y_idx[:, None], x_idx[None, :], :])
        log_signature = (width, height, target_width, target_height)
        if _DOWNSCALE_LOG_SIGNATURE != log_signature:
            _DOWNSCALE_LOG_SIGNATURE = log_signature
            hint_width, hint_height = _get_stream_target_hint()
            print(
                "[SutuBridge] stream downscale "
                f"{width}x{height} -> {target_width}x{target_height} "
                f"(hint={hint_width}x{hint_height})"
            )
        return target_width, target_height, downscaled.tobytes()
    except Exception:
        return width, height, pixels


def _capture_viewport_frame_once() -> tuple[int, int, bytes] | None:
    for window, area, region, space in _iter_view3d_window_contexts():
        width = int(getattr(region, "width", 0))
        height = int(getattr(region, "height", 0))
        if width <= 0 or height <= 0:
            continue

        try:
            with bpy.context.temp_override(window=window, area=area, region=region, space_data=space):
                pixels = _capture_with_offscreen(width, height)
                if pixels is None:
                    frame_buffer = gpu.state.active_framebuffer_get()
                    if frame_buffer is None:
                        continue
                    pixels = _capture_best_color_bytes(frame_buffer, width, height)
        except Exception:
            continue

        if pixels is None:
            continue
        return _downscale_for_stream(width, height, pixels)
    return None


def _capture_render_result_pixels() -> tuple[int, int, bytes] | None:
    image = bpy.data.images.get("Render Result")
    if image is None:
        return None

    direct = _capture_image_pixels_rgba8(image)
    if direct is not None:
        return direct
    return _capture_render_result_via_temp_file(image)


def _capture_image_pixels_rgba8(image) -> tuple[int, int, bytes] | None:
    width = int(getattr(image, "size", [0, 0])[0] if getattr(image, "size", None) else 0)
    height = int(getattr(image, "size", [0, 0])[1] if getattr(image, "size", None) else 0)
    if width <= 0 or height <= 0:
        return None

    expected_len = width * height * 4
    try:
        if np is not None:
            pixels_f32 = np.empty(expected_len, dtype=np.float32)
            image.pixels.foreach_get(pixels_f32)
        else:
            pixels_f32 = list(image.pixels[:expected_len])
    except Exception:
        return None
    if len(pixels_f32) < expected_len:
        return None

    if np is not None:
        try:
            frame = np.array(pixels_f32[:expected_len], dtype=np.float32).reshape(height, width, 4)
            packed = np.clip(frame * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
            packed = np.ascontiguousarray(packed[::-1, :, :4])
            return _downscale_for_stream(width, height, packed.tobytes())
        except Exception:
            pass

    packed = bytearray(expected_len)
    dst = 0
    for y in range(height - 1, -1, -1):
        row_start = y * width * 4
        row_end = row_start + width * 4
        for value in pixels_f32[row_start:row_end]:
            clamped = max(0.0, min(1.0, float(value)))
            packed[dst] = int(clamped * 255.0 + 0.5)
            dst += 1
    return _downscale_for_stream(width, height, bytes(packed))


def _capture_render_result_via_temp_file(image) -> tuple[int, int, bytes] | None:
    temp_path = ""
    loaded_image = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="sutu_bridge_render_result_",
            suffix=".png",
            delete=False,
        ) as fp:
            temp_path = fp.name
        image.save_render(temp_path, scene=bpy.context.scene)
        loaded_image = bpy.data.images.load(temp_path, check_existing=False)
        return _capture_image_pixels_rgba8(loaded_image)
    except Exception:
        return None
    finally:
        if loaded_image is not None:
            try:
                bpy.data.images.remove(loaded_image)
            except Exception:
                pass
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass


def _send_single_frame_to_bridge(width: int, height: int, pixels: bytes, stop_reason: str) -> Optional[int]:
    sender = get_frame_sender()
    sender.start_stream(stream_id=None)
    try:
        frame_id = sender.send_rgba_frame(width=width, height=height, pixels=pixels)
    except Exception:
        sender.stop_stream(reason=stop_reason)
        raise

    if frame_id is None:
        sender.stop_stream(reason=stop_reason)
        return None

    if get_bridge_client().selected_transport == BRIDGE_TRANSPORT_SHM:
        _defer_one_shot_stream_stop(reason=stop_reason, delay_sec=ONE_SHOT_SHM_STOP_DELAY_SEC)
    else:
        sender.stop_stream(reason=stop_reason)
    return frame_id


def _defer_one_shot_stream_stop(reason: str, delay_sec: float) -> None:
    def _timer_callback():
        sender = get_frame_sender()
        if not sender.is_streaming:
            return None
        # If live stream resumed, do not interrupt user flow.
        if _DRAW_HANDLER is not None:
            return None
        sender.stop_stream(reason=reason)
        return None

    try:
        bpy.app.timers.register(_timer_callback, first_interval=max(0.05, float(delay_sec)))
    except Exception:
        get_frame_sender().stop_stream(reason=reason)


def _send_render_result_payload(captured: tuple[int, int, bytes], stop_reason: str) -> Optional[int]:
    width, height, pixels = captured
    return _send_single_frame_to_bridge(
        width=width,
        height=height,
        pixels=pixels,
        stop_reason=stop_reason,
    )


def _complete_render_send_from_result() -> None:
    captured = _capture_render_result_pixels()
    if captured is None:
        message = "渲染完成，但读取 Render Result 失败"
        print(f"[SutuBridge] {message}")
        _show_bridge_popup(message, icon="ERROR")
        return

    frame_id = _send_render_result_payload(
        captured=captured,
        stop_reason="render_result_sent",
    )
    if frame_id is None:
        message = "渲染完成，但发送 Render Result 失败"
        print(f"[SutuBridge] {message}")
        _show_bridge_popup(message, icon="ERROR")
        return

    print(f"[SutuBridge] Render Result 已发送 frame_id={frame_id}")
    _show_bridge_popup(f"已发送 Render Result frame_id={frame_id}", icon="INFO")


def _on_render_send_complete(_scene, _depsgraph=None) -> None:
    global _RENDER_SEND_PENDING
    if not _RENDER_SEND_PENDING:
        return
    _RENDER_SEND_PENDING = False
    _remove_render_send_handlers()
    _complete_render_send_from_result()


def _on_render_send_cancel(_scene, _depsgraph=None) -> None:
    global _RENDER_SEND_PENDING
    if not _RENDER_SEND_PENDING:
        return
    _RENDER_SEND_PENDING = False
    _remove_render_send_handlers()
    message = "渲染已取消，未发送 Render Result"
    print(f"[SutuBridge] {message}")
    _show_bridge_popup(message, icon="ERROR")


def _ensure_render_send_handlers() -> None:
    if _on_render_send_complete not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(_on_render_send_complete)
    if _on_render_send_cancel not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(_on_render_send_cancel)


def _remove_render_send_handlers() -> None:
    try:
        bpy.app.handlers.render_complete.remove(_on_render_send_complete)
    except Exception:
        pass
    try:
        bpy.app.handlers.render_cancel.remove(_on_render_send_cancel)
    except Exception:
        pass


def _trigger_async_render_send() -> tuple[bool, str]:
    global _RENDER_SEND_PENDING
    if _RENDER_SEND_PENDING:
        return False, "渲染发送任务已在进行中"

    _ensure_render_send_handlers()
    _RENDER_SEND_PENDING = True
    try:
        result = bpy.ops.render.render("INVOKE_DEFAULT", use_viewport=False, write_still=False)
    except Exception as exc:
        _RENDER_SEND_PENDING = False
        _remove_render_send_handlers()
        return False, f"触发渲染失败: {exc}"

    if "CANCELLED" in result:
        _RENDER_SEND_PENDING = False
        _remove_render_send_handlers()
        return False, "渲染已取消"
    return True, ""


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
    global _DOWNSCALE_LOG_SIGNATURE
    global _DOWNSCALE_INDEX_CACHE
    global _WAITING_HINT_LOGGED
    _LAST_CAPTURE_AT = 0.0
    _LAST_DIRTY_AT = 0.0
    _PENDING_CAPTURE = False
    _LAST_VIEW_SIGNATURE = None
    _CAPTURE_COLOR_SLOT = None
    _CAPTURE_BACKEND = None
    _OFFSCREEN_LAYOUT_LOGGED = False
    _DOWNSCALE_LOG_SIGNATURE = None
    _DOWNSCALE_INDEX_CACHE = {}
    _WAITING_HINT_LOGGED = False
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


class SUTU_OT_bridge_send_current_frame(bpy.types.Operator):
    bl_idname = "sutu_bridge.send_current_frame"
    bl_label = "Send Current Frame"
    bl_description = "发送当前视口单帧到 Sutu（会先停止实时推流）"

    def execute(self, context: bpy.types.Context):
        client = get_bridge_client()
        status = client.get_status()
        if status.get("state") != "streaming":
            self.report({"ERROR"}, "请先连接 Sutu Bridge")
            return {"CANCELLED"}

        _stop_live_stream_for_one_shot(reason="send_current_frame")

        captured = _capture_viewport_frame_once()
        if captured is None:
            self.report({"ERROR"}, "未找到可用的 3D 视口，无法采集当前帧")
            return {"CANCELLED"}

        width, height, pixels = captured
        frame_id = _send_single_frame_to_bridge(
            width=width,
            height=height,
            pixels=pixels,
            stop_reason="single_frame_sent",
        )
        if frame_id is None:
            self.report({"ERROR"}, "单帧发送失败")
            return {"CANCELLED"}

        self.report({"INFO"}, f"已发送当前单帧 frame_id={frame_id}")
        return {"FINISHED"}


class SUTU_OT_bridge_send_render_result(bpy.types.Operator):
    bl_idname = "sutu_bridge.send_render_result"
    bl_label = "Send Render Result"
    bl_description = "发送 Render Result（可配置是否先触发重渲染，会先停止实时推流）"

    def execute(self, context: bpy.types.Context):
        client = get_bridge_client()
        status = client.get_status()
        if status.get("state") != "streaming":
            self.report({"ERROR"}, "请先连接 Sutu Bridge")
            return {"CANCELLED"}

        _stop_live_stream_for_one_shot(reason="send_render_result")

        prefs = get_addon_preferences(context)
        use_existing = bool(getattr(prefs, "send_render_use_existing_result", False))
        if use_existing:
            captured = _capture_render_result_pixels()
            if captured is None:
                self.report({"ERROR"}, "当前 Render Result 不可读，请先渲染一次或关闭“Use Existing Result”")
                return {"CANCELLED"}
            frame_id = _send_render_result_payload(
                captured=captured,
                stop_reason="render_result_sent",
            )
            if frame_id is None:
                self.report({"ERROR"}, "Render Result 发送失败")
                return {"CANCELLED"}
            self.report({"INFO"}, f"已发送 Render Result frame_id={frame_id}")
            return {"FINISHED"}

        scene = getattr(context, "scene", None)
        if scene is None or getattr(scene, "camera", None) is None:
            self.report(
                {"ERROR"},
                "当前场景没有相机。请先添加相机（Shift+A -> Camera），或勾选 Use Existing Render Result 发送现有结果。",
            )
            return {"CANCELLED"}

        ok, message = _trigger_async_render_send()
        if not ok:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, "已触发渲染，完成后将自动发送 Render Result")
        return {"FINISHED"}


def unregister() -> None:
    global _RENDER_SEND_PENDING
    _RENDER_SEND_PENDING = False
    _remove_render_send_handlers()
    _remove_stream_hooks()
    _reset_stream_state()
    shutdown_frame_sender()
