from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from typing import Any, Optional

from ..addon_meta import ADDON_VERSION_STR

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_WEBSOCKET_PORT = 30122
WEBSOCKET_TRANSPORT = "websocket"
TRANSPORT_MODE_AUTO = "auto"
TRANSPORT_MODE_NATIVE = "native"
TRANSPORT_MODE_WEBSOCKET = "websocket"


def normalize_transport_mode(value: Any) -> str:
    normalized = str(value or TRANSPORT_MODE_AUTO).strip().lower()
    if normalized in {TRANSPORT_MODE_AUTO, TRANSPORT_MODE_NATIVE, TRANSPORT_MODE_WEBSOCKET}:
        return normalized
    return TRANSPORT_MODE_AUTO


class WebSocketBridgeServer:
    def __init__(self) -> None:
        self._port = DEFAULT_WEBSOCKET_PORT
        self._enabled = False
        self._state = "disabled"
        self._last_error: Optional[dict[str, str]] = None
        self._session_counter = 0
        self._active_session_id: Optional[int] = None
        self._frame_counter = 0
        self._client_count = 0

        self._lock = threading.Lock()
        self._server_socket: Optional[socket.socket] = None
        self._client_socket: Optional[socket.socket] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._client_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def configure(self, port: int, enable_server: bool) -> bool:
        if not 1024 <= int(port) <= 65535:
            self._set_error("E_PORT_INVALID", f"WebSocket port must be between 1024 and 65535: {port}")
            return False

        should_restart = False
        with self._lock:
            should_restart = self._port != int(port) and self._enabled
            self._port = int(port)
            self._enabled = bool(enable_server)
            if not self._enabled:
                self._state = "disabled"
                self._active_session_id = None
                self._client_count = 0

        if not enable_server:
            self._stop_server()
            self._clear_error()
            return True

        if should_restart:
            self._stop_server()
        self._start_server_if_needed()
        return True

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "enabled": self._enabled,
                "port": self._port,
                "transport": WEBSOCKET_TRANSPORT if self._enabled else None,
                "degraded": False,
                "session_id": self._active_session_id,
                "sessionId": self._active_session_id,
                "inflight_frames": 0,
                "inflightFrames": 0,
                "max_inflight_frames": 1,
                "maxInflightFrames": 1,
                "last_error": dict(self._last_error) if self._last_error else None,
                "lastError": dict(self._last_error) if self._last_error else None,
                "client_count": self._client_count,
                "clientCount": self._client_count,
                "websocket_url": f"ws://127.0.0.1:{self._port}",
                "websocketUrl": f"ws://127.0.0.1:{self._port}",
            }

    def has_client(self) -> bool:
        with self._lock:
            return self._client_socket is not None and self._client_count > 0

    def send_png_frame(
        self,
        width: int,
        height: int,
        png_bytes: bytes,
        timestamp_ms: Optional[int] = None,
    ) -> Optional[int]:
        with self._lock:
            client = self._client_socket
            if client is None or not self._enabled:
                return None
            self._frame_counter += 1
            frame_id = self._frame_counter

        payload = {
            "type": "frame",
            "frameId": frame_id,
            "width": int(width),
            "height": int(height),
            "timestampMs": int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
            "mimeType": "image/png",
            "imageData": base64.b64encode(png_bytes).decode("ascii"),
        }
        try:
            self._send_text_frame(client, json.dumps(payload, separators=(",", ":")))
        except Exception as exc:
            self._set_error("E_WS_SEND", f"Failed to send websocket frame: {exc}")
            self._detach_client(client)
            return None
        return frame_id

    def shutdown(self) -> None:
        self.configure(port=self._port, enable_server=False)

    def _start_server_if_needed(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._server_main,
            name="SutuBridgeWebSocketServer",
            daemon=True,
        )
        self._worker_thread.start()

    def _stop_server(self) -> None:
        self._stop_event.set()
        self._close_client_socket()
        self._close_server_socket()
        if self._client_thread and self._client_thread.is_alive():
            self._client_thread.join(timeout=1.0)
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        self._client_thread = None
        self._worker_thread = None
        self._stop_event.clear()

    def _server_main(self) -> None:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", self._port))
            server.listen(1)
            server.settimeout(0.5)
            with self._lock:
                self._server_socket = server
                self._state = "listening"
            self._clear_error()
        except OSError as exc:
            self._set_error("E_WS_LISTEN", f"Failed to listen on websocket port {self._port}: {exc}")
            with self._lock:
                self._state = "recovering" if self._enabled else "disabled"
            return

        try:
            while not self._stop_event.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                try:
                    self._perform_handshake(client)
                    self._attach_client(client)
                except Exception as exc:
                    self._set_error("E_WS_HANDSHAKE", f"WebSocket handshake failed: {exc}")
                    try:
                        client.close()
                    except Exception:
                        pass
        finally:
            self._close_client_socket()
            self._close_server_socket()
            with self._lock:
                self._state = "disabled" if not self._enabled else "listening"

    def _perform_handshake(self, client: socket.socket) -> None:
        client.settimeout(2.0)
        request = self._recv_until(client, b"\r\n\r\n", 65536).decode("utf-8", errors="ignore")
        header_lines = request.split("\r\n")
        request_line = header_lines[0] if header_lines else ""
        if "Upgrade: websocket" not in request and "upgrade: websocket" not in request.lower():
            raise RuntimeError("missing websocket upgrade header")
        if not request_line.startswith("GET "):
            raise RuntimeError("invalid websocket request line")

        headers: dict[str, str] = {}
        for line in header_lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        sec_key = headers.get("sec-websocket-key")
        if not sec_key:
            raise RuntimeError("missing Sec-WebSocket-Key")

        accept_value = base64.b64encode(
            hashlib.sha1(f"{sec_key}{GUID}".encode("utf-8")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_value}\r\n\r\n"
        )
        client.sendall(response.encode("utf-8"))
        self._send_text_frame(
            client,
            json.dumps(
                {
                    "type": "hello_ack",
                    "accepted": True,
                    "serverVersion": ADDON_VERSION_STR,
                    "selectedTransport": WEBSOCKET_TRANSPORT,
                },
                separators=(",", ":"),
            ),
        )

    def _attach_client(self, client: socket.socket) -> None:
        self._close_client_socket()
        with self._lock:
            self._session_counter += 1
            self._active_session_id = self._session_counter
            self._client_socket = client
            self._client_count = 1
            self._state = "streaming"
        client.settimeout(0.5)
        self._client_thread = threading.Thread(
            target=self._client_reader_main,
            args=(client,),
            name="SutuBridgeWebSocketClient",
            daemon=True,
        )
        self._client_thread.start()

    def _client_reader_main(self, client: socket.socket) -> None:
        try:
            while not self._stop_event.is_set():
                opcode, payload = self._recv_frame(client)
                if opcode is None:
                    continue
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_control_frame(client, 0xA, payload)
                    continue
        except Exception:
            pass
        finally:
            self._detach_client(client)

    def _detach_client(self, client: socket.socket) -> None:
        with self._lock:
            active = self._client_socket
            if active is not client:
                return
            self._client_socket = None
            self._client_count = 0
            self._active_session_id = None
            self._state = "listening" if self._enabled else "disabled"
        try:
            client.close()
        except Exception:
            pass

    def _recv_until(self, client: socket.socket, marker: bytes, max_bytes: int) -> bytes:
        buffer = bytearray()
        while marker not in buffer:
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError("peer closed during handshake")
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise RuntimeError("websocket handshake too large")
        return bytes(buffer)

    def _recv_frame(self, client: socket.socket) -> tuple[Optional[int], bytes]:
        try:
            header = self._recv_exact(client, 2)
        except socket.timeout:
            return None, b""

        first, second = header[0], header[1]
        opcode = first & 0x0F
        masked = (second & 0x80) != 0
        payload_len = second & 0x7F

        if payload_len == 126:
            payload_len = struct.unpack(">H", self._recv_exact(client, 2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack(">Q", self._recv_exact(client, 8))[0]

        mask_key = self._recv_exact(client, 4) if masked else b""
        payload = self._recv_exact(client, payload_len) if payload_len > 0 else b""
        if masked:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, client: socket.socket, size: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < size:
            chunk = client.recv(size - len(buffer))
            if not chunk:
                raise RuntimeError("websocket peer closed")
            buffer.extend(chunk)
        return bytes(buffer)

    def _send_text_frame(self, client: socket.socket, text: str) -> None:
        self._send_control_frame(client, 0x1, text.encode("utf-8"))

    def _send_control_frame(self, client: socket.socket, opcode: int, payload: bytes) -> None:
        payload_len = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if payload_len < 126:
            header.append(payload_len)
        elif payload_len <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack(">H", payload_len))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", payload_len))
        client.sendall(bytes(header) + payload)

    def _close_server_socket(self) -> None:
        with self._lock:
            server = self._server_socket
            self._server_socket = None
        if server is None:
            return
        try:
            server.close()
        except Exception:
            pass

    def _close_client_socket(self) -> None:
        with self._lock:
            client = self._client_socket
            self._client_socket = None
            self._client_count = 0
            self._active_session_id = None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            pass

    def _set_error(self, code: str, message: str) -> None:
        with self._lock:
            self._last_error = {"code": str(code), "message": str(message)}
        print(f"[SutuBridge][{code}] {message}")

    def _clear_error(self) -> None:
        with self._lock:
            self._last_error = None


_WEBSOCKET_SERVER: Optional[WebSocketBridgeServer] = None


def get_websocket_bridge_server() -> WebSocketBridgeServer:
    global _WEBSOCKET_SERVER
    if _WEBSOCKET_SERVER is None:
        _WEBSOCKET_SERVER = WebSocketBridgeServer()
    return _WEBSOCKET_SERVER


def shutdown_websocket_bridge_server() -> None:
    global _WEBSOCKET_SERVER
    if _WEBSOCKET_SERVER is None:
        return
    _WEBSOCKET_SERVER.shutdown()
    _WEBSOCKET_SERVER = None


def unregister() -> None:
    shutdown_websocket_bridge_server()
