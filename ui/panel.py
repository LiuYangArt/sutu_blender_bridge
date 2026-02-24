from typing import Optional

import bpy

from ..bridge.client import (
    ADDON_ID,
    get_addon_preferences,
    get_bridge_client,
)
from ..bridge.frame_sender import get_frame_sender


def _apply_bridge_preferences(context: Optional[bpy.types.Context]) -> None:
    prefs = get_addon_preferences(context)
    if prefs is None:
        return

    client = get_bridge_client()
    enabled = bool(client.get_status().get("enabled", True))
    ok = client.configure(
        port=int(getattr(prefs, "port", 30121)),
        enable_connection=enabled,
    )
    if not ok:
        if enabled:
            client.disable_connection()
        return


def _on_bridge_config_updated(self, context: Optional[bpy.types.Context]) -> None:
    _apply_bridge_preferences(context)


def _draw_debug_options(layout, prefs) -> None:
    layout.separator()
    layout.prop(prefs, "auto_install_lz4")
    layout.prop(prefs, "dump_frame_files")
    row = layout.row()
    row.enabled = bool(getattr(prefs, "dump_frame_files", False))
    row.prop(prefs, "dump_max_frames")
    row = layout.row()
    row.enabled = bool(getattr(prefs, "dump_frame_files", False))
    row.prop(prefs, "dump_directory")


class SUTUBridgeAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    port: bpy.props.IntProperty(  # type: ignore
        name="Port",
        description="Sutu Bridge 监听端口",
        default=30121,
        min=1024,
        max=65535,
        update=_on_bridge_config_updated,
    )

    send_render_use_existing_result: bpy.props.BoolProperty(  # type: ignore
        name="Use Existing Render Result",
        description="启用后 Send Render 不触发重渲染，直接发送当前 Render Result",
        default=False,
    )

    auto_install_lz4: bpy.props.BoolProperty(  # type: ignore
        name="Auto Install LZ4",
        description="缺少 lz4 时尝试自动安装；失败会自动退化为原始字节发送",
        default=True,
    )

    dump_frame_files: bpy.props.BoolProperty(  # type: ignore
        name="Dump Frame Files",
        description="导出采集帧与传输字节到文件，便于排查编码/解码问题",
        default=False,
    )

    dump_max_frames: bpy.props.IntProperty(  # type: ignore
        name="Dump Max Frames",
        description="每次推流会话最多导出的帧数",
        default=3,
        min=1,
        max=30,
    )

    dump_directory: bpy.props.StringProperty(  # type: ignore
        name="Dump Directory",
        description="调试文件输出目录，留空时使用系统临时目录",
        default="",
        subtype="DIR_PATH",
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "port")
        _draw_debug_options(layout, self)


class SUTU_PT_bridge_panel(bpy.types.Panel):
    bl_idname = "SUTU_PT_bridge_panel"
    bl_label = "Sutu Bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sutu"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        prefs = get_addon_preferences(context)
        if prefs is None:
            layout.label(text="插件配置未就绪", icon="ERROR")
            return

        layout.prop(prefs, "send_render_use_existing_result")

        status = get_bridge_client().get_status()
        status_row = layout.column(align=True)
        status_row.label(text=f"State: {status.get('state', 'unknown')}")
        transport = status.get("transport")
        if transport:
            status_row.label(text=f"Transport: {transport}")
        if status.get("degraded"):
            status_row.label(text="Degraded: true")

        last_error = status.get("last_error")
        if isinstance(last_error, dict):
            status_row.label(text=f"Error: {last_error.get('code', 'UNKNOWN')}", icon="ERROR")

        row = layout.row(align=True)
        if status.get("enabled", False):
            row.operator("sutu_bridge.connect_toggle", text="Disconnect", icon="UNLINKED")
        else:
            row.operator("sutu_bridge.connect_toggle", text="Connect", icon="LINKED")

        sender = get_frame_sender()
        row = layout.row(align=True)
        if sender.is_streaming:
            row.operator("sutu_bridge.stop_stream", text="Stop Stream", icon="PAUSE")
        else:
            row.operator("sutu_bridge.start_stream", text="Start Stream", icon="PLAY")

        one_shot_row = layout.row(align=True)
        one_shot_row.enabled = status.get("state") == "streaming"
        one_shot_row.operator("sutu_bridge.send_current_frame", text="Send Viewport", icon="IMAGE_DATA")
        one_shot_row.operator("sutu_bridge.send_render_result", text="Send Render", icon="RENDER_STILL")
