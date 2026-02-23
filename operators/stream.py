from __future__ import annotations

import time

import bpy
import gpu

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

    frame_buffer = gpu.state.active_framebuffer_get()
    if frame_buffer is None:
        return

    try:
        rgba_buffer = frame_buffer.read_color(0, 0, width, height, 4, 0, "UBYTE")
        sender.send_rgba_frame(width=width, height=height, pixels=bytes(rgba_buffer))
        _LAST_CAPTURE_AT = time.monotonic()
        _PENDING_CAPTURE = False
    except Exception as exc:
        _PENDING_CAPTURE = False
        print(f"[SutuBridge] 采集视口帧失败: {exc}")


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


def _reset_stream_state() -> None:
    global _LAST_CAPTURE_AT, _LAST_DIRTY_AT, _PENDING_CAPTURE, _LAST_VIEW_SIGNATURE
    _LAST_CAPTURE_AT = 0.0
    _LAST_DIRTY_AT = 0.0
    _PENDING_CAPTURE = False
    _LAST_VIEW_SIGNATURE = None


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
