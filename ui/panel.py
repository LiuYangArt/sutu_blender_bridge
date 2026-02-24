from typing import Optional

import bpy
from bpy.app.translations import pgettext_iface as _

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
    # Preserve current runtime toggle; default to disabled when status is unavailable.
    enabled = bool(client.get_status().get("enabled", False))
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


def _localize_status_state(state: object) -> str:
    mapping = {
        "disabled": _("Disabled"),
        "idle": _("Idle"),
        "listening": _("Listening"),
        "connecting": _("Connecting"),
        "handshaking": _("Handshaking"),
        "recovering": _("Recovering"),
        "streaming": _("Streaming"),
        "error": _("Error"),
        "unknown": _("Unknown"),
    }
    key = str(state or "unknown")
    return mapping.get(key, key)


class SUTUBridgeAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    port: bpy.props.IntProperty(  # type: ignore
        name="Port",
        description="Sutu Bridge listening port",
        default=30121,
        min=1024,
        max=65535,
        update=_on_bridge_config_updated,
    )

    send_render_use_existing_result: bpy.props.BoolProperty(  # type: ignore
        name="Use Existing Render Result",
        description="When enabled, Send Render skips re-rendering and sends the current Render Result",
        default=False,
    )

    auto_install_lz4: bpy.props.BoolProperty(  # type: ignore
        name="Auto Install LZ4",
        description="Try auto-installing lz4 when missing; falls back to raw bytes if installation fails",
        default=True,
    )

    dump_frame_files: bpy.props.BoolProperty(  # type: ignore
        name="Dump Frame Files",
        description="Dump captured frames and transmitted bytes to files for debugging encode/decode issues",
        default=False,
    )

    dump_max_frames: bpy.props.IntProperty(  # type: ignore
        name="Dump Max Frames",
        description="Maximum number of frames to dump per streaming session",
        default=3,
        min=1,
        max=30,
    )

    dump_directory: bpy.props.StringProperty(  # type: ignore
        name="Dump Directory",
        description="Output directory for debug files; uses system temp directory when empty",
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
            layout.label(text=_("Addon preferences are not ready"), icon="ERROR")
            return

        layout.prop(prefs, "send_render_use_existing_result")

        status = get_bridge_client().get_status()
        state_text = _localize_status_state(status.get("state", "unknown"))
        status_row = layout.column(align=True)
        status_row.label(text=f"{_('State')}: {state_text}")
        transport = status.get("transport")
        if transport:
            status_row.label(text=f"{_('Transport')}: {transport}")
        if status.get("degraded"):
            status_row.label(text=f"{_('Degraded')}: {_('Yes')}")

        last_error = status.get("last_error")
        if isinstance(last_error, dict):
            status_row.label(text=f"{_('Error')}: {last_error.get('code', 'UNKNOWN')}", icon="ERROR")

        row = layout.row(align=True)
        if status.get("enabled", False):
            row.operator("sutu_bridge.connect_toggle", text=_("Disconnect"), icon="UNLINKED")
        else:
            row.operator("sutu_bridge.connect_toggle", text=_("Connect"), icon="LINKED")

        sender = get_frame_sender()
        row = layout.row(align=True)
        if sender.is_streaming:
            row.operator("sutu_bridge.stop_stream", text=_("Stop Stream"), icon="PAUSE")
        else:
            row.operator("sutu_bridge.start_stream", text=_("Start Stream"), icon="PLAY")

        one_shot_row = layout.row(align=True)
        one_shot_row.enabled = status.get("state") == "streaming"
        one_shot_row.operator("sutu_bridge.send_current_frame", text=_("Send Viewport"), icon="IMAGE_DATA")
        one_shot_row.operator("sutu_bridge.send_render_result", text=_("Send Render"), icon="RENDER_STILL")
