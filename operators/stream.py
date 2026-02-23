from __future__ import annotations

import time

import bpy
import gpu

from ..bridge.client import get_bridge_client
from ..bridge.frame_sender import get_frame_sender, shutdown_frame_sender

STREAM_INTERVAL_SEC = 1.0 / 12.0

_DRAW_HANDLER = None
_LAST_CAPTURE_AT = 0.0


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


def _stream_redraw_timer():
    sender = get_frame_sender()
    if not sender.is_streaming:
        return None
    for region in _iter_view3d_window_regions():
        region.tag_redraw()
    return STREAM_INTERVAL_SEC


def _capture_draw_callback():
    global _LAST_CAPTURE_AT

    sender = get_frame_sender()
    if not sender.is_streaming:
        return

    status = get_bridge_client().get_status()
    if status.get("state") != "streaming":
        return

    now = time.monotonic()
    if now - _LAST_CAPTURE_AT < STREAM_INTERVAL_SEC:
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
        _LAST_CAPTURE_AT = now
    except Exception as exc:
        print(f"[SutuBridge] 采集视口帧失败: {exc}")


def _ensure_stream_hooks() -> None:
    global _DRAW_HANDLER
    if _DRAW_HANDLER is None:
        _DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            _capture_draw_callback,
            (),
            "WINDOW",
            "POST_PIXEL",
        )
    if not bpy.app.timers.is_registered(_stream_redraw_timer):
        bpy.app.timers.register(_stream_redraw_timer, persistent=True)


def _remove_stream_hooks() -> None:
    global _DRAW_HANDLER
    if _DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLER, "WINDOW")
        except Exception:
            pass
        _DRAW_HANDLER = None
    if bpy.app.timers.is_registered(_stream_redraw_timer):
        bpy.app.timers.unregister(_stream_redraw_timer)


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
        _ensure_stream_hooks()
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
        self.report({"INFO"}, "已停止推流")
        return {"FINISHED"}


def unregister() -> None:
    _remove_stream_hooks()
    shutdown_frame_sender()
