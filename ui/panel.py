from __future__ import annotations

from typing import Optional

import bpy

from ..bridge.client import (
    ADDON_ID,
    BRIDGE_MODE_AUTO,
    BRIDGE_MODE_MANUAL,
    get_addon_preferences,
    get_bridge_client,
)
from ..bridge.frame_sender import get_frame_sender


def _apply_bridge_preferences(context: Optional[bpy.types.Context], connect_now: bool) -> None:
    prefs = get_addon_preferences(context)
    if prefs is None:
        return

    client = get_bridge_client()
    ok = client.configure(
        link_mode=str(getattr(prefs, "link_mode", BRIDGE_MODE_MANUAL)),
        port=int(getattr(prefs, "port", 30121)),
        enable_connection=bool(getattr(prefs, "enable_connection", False)),
    )
    if not ok:
        return
    if bool(getattr(prefs, "enable_connection", False)) and connect_now:
        client.request_connect()


def _on_bridge_config_updated(self, context: Optional[bpy.types.Context]) -> None:
    connect_now = bool(
        getattr(self, "enable_connection", False) and getattr(self, "link_mode", "") == BRIDGE_MODE_AUTO
    )
    _apply_bridge_preferences(context, connect_now=connect_now)


def _on_bridge_enable_updated(self, context: Optional[bpy.types.Context]) -> None:
    enabled = bool(getattr(self, "enable_connection", False))
    _apply_bridge_preferences(context, connect_now=enabled)
    if not enabled:
        get_bridge_client().disable_connection()


class SUTUBridgeAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    link_mode: bpy.props.EnumProperty(  # type: ignore
        name="Link Mode",
        description="选择自动连接或手动连接",
        items=(
            (BRIDGE_MODE_AUTO, "Auto", "自动模式：启用后自动尝试连接"),
            (BRIDGE_MODE_MANUAL, "Manual", "手动模式：点击 Connect 触发连接"),
        ),
        default=BRIDGE_MODE_MANUAL,
        update=_on_bridge_config_updated,
    )

    port: bpy.props.IntProperty(  # type: ignore
        name="Port",
        description="Sutu Bridge 监听端口",
        default=30121,
        min=1024,
        max=65535,
        update=_on_bridge_config_updated,
    )

    enable_connection: bpy.props.BoolProperty(  # type: ignore
        name="Enable Connection",
        description="启用连接后客户端会保持监听/重连",
        default=False,
        update=_on_bridge_enable_updated,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "link_mode")
        layout.prop(self, "port")
        layout.prop(self, "enable_connection")


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

        layout.prop(prefs, "link_mode")
        layout.prop(prefs, "port")
        layout.prop(prefs, "enable_connection")

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
