from __future__ import annotations

import bpy
from bpy.app.translations import pgettext_iface as _

from ..bridge.client import get_addon_preferences, get_bridge_client
from ..bridge.websocket_server import (
    DEFAULT_WEBSOCKET_PORT,
    TRANSPORT_MODE_AUTO,
    TRANSPORT_MODE_NATIVE,
    TRANSPORT_MODE_WEBSOCKET,
    get_websocket_bridge_server,
    is_web_bridge_temporarily_disabled,
    normalize_transport_mode,
)


def _resolve_websocket_port(prefs) -> int:
    if prefs is None:
        return DEFAULT_WEBSOCKET_PORT
    return int(getattr(prefs, "websocket_port", DEFAULT_WEBSOCKET_PORT))


def _disable_bridge_connections(client, websocket_server, prefs) -> None:
    client.disable_connection()
    websocket_server.configure(
        port=_resolve_websocket_port(prefs),
        enable_server=False,
    )


class SUTU_OT_bridge_connect_toggle(bpy.types.Operator):
    bl_idname = "sutu_bridge.connect_toggle"
    bl_label = "Toggle Bridge Connection"
    bl_description = "Connects or disconnects Sutu Bridge"

    def execute(self, context: bpy.types.Context):
        client = get_bridge_client()
        websocket_server = get_websocket_bridge_server()
        prefs = get_addon_preferences(context)
        mode = normalize_transport_mode(getattr(prefs, "transport_mode", TRANSPORT_MODE_AUTO))

        native_enabled = bool(client.get_status().get("enabled", False))
        websocket_enabled = bool(websocket_server.get_status().get("enabled", False))
        if native_enabled or websocket_enabled:
            _disable_bridge_connections(client, websocket_server, prefs)
            self.report({"INFO"}, _("Sutu Bridge disconnected"))
            return {"FINISHED"}

        if is_web_bridge_temporarily_disabled(mode):
            _disable_bridge_connections(client, websocket_server, prefs)
            self.report({"ERROR"}, _("Web Blender Bridge is temporarily disabled."))
            return {"CANCELLED"}

        port = int(getattr(prefs, "port", 30121)) if prefs is not None else 30121
        websocket_port = _resolve_websocket_port(prefs)

        ok_native = True
        ok_websocket = True

        if mode in {TRANSPORT_MODE_NATIVE, TRANSPORT_MODE_AUTO}:
            ok_native = client.configure(
                port=port,
                enable_connection=True,
            )
            if ok_native:
                client.request_connect()
        else:
            client.disable_connection()

        if mode in {TRANSPORT_MODE_WEBSOCKET, TRANSPORT_MODE_AUTO}:
            ok_websocket = websocket_server.configure(
                port=websocket_port,
                enable_server=True,
            )
        else:
            websocket_server.configure(port=websocket_port, enable_server=False)

        if not ok_native or not ok_websocket:
            client.disable_connection()
            websocket_server.configure(port=websocket_port, enable_server=False)
            last_error = client.get_status().get("last_error")
            if not last_error:
                last_error = websocket_server.get_status().get("last_error")
            if isinstance(last_error, dict):
                error_code = str(last_error.get("code") or "UNKNOWN")
                self.report({"ERROR"}, _("Connection setup failed ({code})").format(code=error_code))
            else:
                self.report({"ERROR"}, _("Connection setup failed"))
            return {"CANCELLED"}

        self.report({"INFO"}, _("Sutu Bridge connecting"))
        return {"FINISHED"}
