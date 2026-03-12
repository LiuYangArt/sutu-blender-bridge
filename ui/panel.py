from typing import Optional

import bpy
from bpy.app.translations import pgettext_iface as _

from ..bridge.client import (
    ADDON_ID,
    get_addon_preferences,
    get_bridge_client,
)
from ..bridge.websocket_server import (
    DEFAULT_WEBSOCKET_PORT,
    TRANSPORT_MODE_AUTO,
    TRANSPORT_MODE_NATIVE,
    TRANSPORT_MODE_WEBSOCKET,
    get_websocket_bridge_server,
    is_web_bridge_temporarily_disabled,
    normalize_transport_mode,
)
from ..operators.stream import is_live_stream_active


def _draw_web_bridge_disabled_notice(layout) -> None:
    warning_box = layout.box()
    warning_box.label(text=_("Web Blender Bridge is temporarily disabled."), icon="ERROR")
    warning_box.label(text=_("Use Sutu Desktop with Native mode."))


def _resolve_selected_transport_mode(context: Optional[bpy.types.Context]) -> str:
    prefs = get_addon_preferences(context)
    if prefs is None:
        return TRANSPORT_MODE_NATIVE
    return normalize_transport_mode(getattr(prefs, "transport_mode", TRANSPORT_MODE_AUTO))


def _resolve_panel_status(context: Optional[bpy.types.Context]):
    mode = _resolve_selected_transport_mode(context)
    websocket_status = get_websocket_bridge_server().get_status()
    native_status = get_bridge_client().get_status()

    if mode == TRANSPORT_MODE_NATIVE:
        return native_status
    if mode == TRANSPORT_MODE_WEBSOCKET:
        return websocket_status
    if websocket_status.get("state") == "streaming":
        return websocket_status
    if native_status.get("state") == "streaming":
        return native_status
    if websocket_status.get("enabled"):
        return websocket_status
    return native_status


def _resolve_websocket_port(prefs) -> int:
    return int(getattr(prefs, "websocket_port", DEFAULT_WEBSOCKET_PORT))


def _apply_bridge_preferences(context: Optional[bpy.types.Context]) -> None:
    prefs = get_addon_preferences(context)
    if prefs is None:
        return

    client = get_bridge_client()
    websocket_server = get_websocket_bridge_server()
    mode = _resolve_selected_transport_mode(context)
    websocket_port = _resolve_websocket_port(prefs)

    native_enabled = bool(client.get_status().get("enabled", False))
    websocket_enabled = bool(websocket_server.get_status().get("enabled", False))

    if is_web_bridge_temporarily_disabled(mode):
        client.disable_connection()
        websocket_server.configure(
            port=websocket_port,
            enable_server=False,
        )
        return

    if mode in {TRANSPORT_MODE_NATIVE, TRANSPORT_MODE_AUTO}:
        ok = client.configure(
            port=int(getattr(prefs, "port", 30121)),
            enable_connection=native_enabled,
        )
        if not ok and native_enabled:
            client.disable_connection()
    else:
        client.disable_connection()

    if mode in {TRANSPORT_MODE_WEBSOCKET, TRANSPORT_MODE_AUTO}:
        ok = websocket_server.configure(
            port=websocket_port,
            enable_server=websocket_enabled,
        )
        if not ok and websocket_enabled:
            websocket_server.configure(
                port=websocket_port,
                enable_server=False,
            )
    else:
        websocket_server.configure(
            port=websocket_port,
            enable_server=False,
        )


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

    transport_mode: bpy.props.EnumProperty(  # type: ignore
        name="Transport Mode",
        description="Choose which transport runtime should be exposed to Sutu",
        items=[
            (TRANSPORT_MODE_AUTO, "Auto", "Enable both runtimes and use whichever peer becomes active"),
            (TRANSPORT_MODE_NATIVE, "Native", "Use the existing desktop bridge runtime"),
            (TRANSPORT_MODE_WEBSOCKET, "WebSocket", "Expose a local WebSocket server for Sutu Web"),
        ],
        default=TRANSPORT_MODE_NATIVE,
        update=_on_bridge_config_updated,
    )

    websocket_port: bpy.props.IntProperty(  # type: ignore
        name="WebSocket Port",
        description="Local WebSocket server port for Sutu Web",
        default=DEFAULT_WEBSOCKET_PORT,
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
        mode = _resolve_selected_transport_mode(context)
        layout.prop(self, "transport_mode")
        layout.prop(self, "port")
        websocket_row = layout.row()
        websocket_row.enabled = False
        websocket_row.prop(self, "websocket_port")
        if is_web_bridge_temporarily_disabled(mode):
            _draw_web_bridge_disabled_notice(layout)
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

        selected_mode = _resolve_selected_transport_mode(context)
        web_bridge_disabled = is_web_bridge_temporarily_disabled(selected_mode)
        status = _resolve_panel_status(context)
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

        if web_bridge_disabled:
            _draw_web_bridge_disabled_notice(layout)

        row = layout.row(align=True)
        row.enabled = not web_bridge_disabled
        if status.get("enabled", False):
            row.operator("sutu_bridge.connect_toggle", text=_("Disconnect"), icon="UNLINKED")
        else:
            row.operator("sutu_bridge.connect_toggle", text=_("Connect"), icon="LINKED")

        row = layout.row(align=True)
        row.enabled = not web_bridge_disabled
        if is_live_stream_active():
            row.operator("sutu_bridge.stop_stream", text=_("Stop Stream"), icon="PAUSE")
        else:
            row.operator("sutu_bridge.start_stream", text=_("Start Stream"), icon="PLAY")

        one_shot_row = layout.row(align=True)
        one_shot_row.enabled = (not web_bridge_disabled) and status.get("state") == "streaming"
        one_shot_row.operator("sutu_bridge.send_current_frame", text=_("Send Viewport"), icon="IMAGE_DATA")
        one_shot_row.operator("sutu_bridge.send_render_result", text=_("Send Render"), icon="RENDER_STILL")
