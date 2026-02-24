from __future__ import annotations

import bpy

from ..bridge.client import get_addon_preferences, get_bridge_client


class SUTU_OT_bridge_connect_toggle(bpy.types.Operator):
    bl_idname = "sutu_bridge.connect_toggle"
    bl_label = "Toggle Bridge Connection"
    bl_description = "连接或断开 Sutu Bridge"

    def execute(self, context: bpy.types.Context):
        client = get_bridge_client()
        status = client.get_status()
        if status.get("enabled", False):
            client.disable_connection()
            self.report({"INFO"}, "Sutu Bridge 已断开")
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
                self.report({"ERROR"}, str(last_error.get("message") or "连接配置失败"))
            else:
                self.report({"ERROR"}, "连接配置失败")
            return {"CANCELLED"}

        client.request_connect()
        self.report({"INFO"}, "Sutu Bridge 正在连接")
        return {"FINISHED"}
