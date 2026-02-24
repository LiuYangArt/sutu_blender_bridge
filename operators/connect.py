from __future__ import annotations

import bpy
from bpy.app.translations import pgettext_iface as _

from ..bridge.client import get_addon_preferences, get_bridge_client


class SUTU_OT_bridge_connect_toggle(bpy.types.Operator):
    bl_idname = "sutu_bridge.connect_toggle"
    bl_label = "Toggle Bridge Connection"
    bl_description = "Connects or disconnects Sutu Bridge"

    def execute(self, context: bpy.types.Context):
        client = get_bridge_client()
        status = client.get_status()
        if status.get("enabled", False):
            client.disable_connection()
            self.report({"INFO"}, _("Sutu Bridge disconnected"))
            return {"FINISHED"}

        prefs = get_addon_preferences(context)
        port = int(getattr(prefs, "port", 30121)) if prefs is not None else 30121
        ok = client.configure(
            port=port,
            enable_connection=True,
        )
        if not ok:
            last_error = client.get_status().get("last_error")
            if isinstance(last_error, dict):
                error_code = str(last_error.get("code") or "UNKNOWN")
                self.report({"ERROR"}, _("Connection setup failed ({code})").format(code=error_code))
            else:
                self.report({"ERROR"}, _("Connection setup failed"))
            return {"CANCELLED"}

        client.request_connect()
        self.report({"INFO"}, _("Sutu Bridge connecting"))
        return {"FINISHED"}
