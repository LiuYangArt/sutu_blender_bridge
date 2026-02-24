from __future__ import annotations

import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import bpy

from .debug_dump import get_bridge_debug_dumper
from .framing import FrameDecoder, FrameError, encode_frame
from .messages import (
    BRIDGE_TRANSPORT_SHM,
    BRIDGE_TRANSPORT_TCP_LZ4,
    E_HEARTBEAT_TIMEOUT,
    E_PORT_INVALID,
    E_PROTO_MISMATCH,
    E_SOCKET_IO,
    E_STOP_REQUESTED,
    HEARTBEAT_INTERVAL_MS,
    HEARTBEAT_TIMEOUT_MS,
    MAX_CONTROL_MESSAGE_BYTES,
    MAX_INFLIGHT_FRAMES,
    MESSAGE_TYPE_ACK,
    MESSAGE_TYPE_ERROR,
    MESSAGE_TYPE_HEARTBEAT,
    BridgeProtocolError,
    build_heartbeat,
    build_hello,
    decode_control_message,
    encode_control_message,
    expect_message_type,
    parse_hello_ack,
)

_PACKAGE_NAME = __package__ or "sutu_blender_bridge.bridge"
if _PACKAGE_NAME.endswith(".bridge"):
    ADDON_ID = _PACKAGE_NAME.rsplit(".", 1)[0]
else:
    ADDON_ID = _PACKAGE_NAME
ADDON_SHORT_ID = ADDON_ID.rsplit(".", 1)[-1]
RECONNECT_BACKOFF_SEC = (0.5, 1.0, 2.0, 5.0)
MAX_BINARY_FRAME_BYTES = 128 * 1024 * 1024


@dataclass
class BridgeClientConfig:
    port: int = 30121
    enable_connection: bool = False


class BridgeClientError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BridgeClient:
    def __init__(self) -> None:
        self._config = BridgeClientConfig()
        self._state = "disabled"
        self._transport: Optional[str] = None
        self._degraded = False
        self._session_counter = 0
        self._active_session_id: Optional[int] = None
        self._last_error: Optional[Dict[str, str]] = None
        self._target_stream_width: Optional[int] = None
        self._target_stream_height: Optional[int] = None

        self._socket_lock = threading.Lock()
        self._socket: Optional[socket.socket] = None
        self._state_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._send_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=512)

        self._inflight_frame_ids: deque[int] = deque()
        self._max_inflight_frames = MAX_INFLIGHT_FRAMES

        self._client_name = "sutu_blender_bridge"
        self._client_version = "0.2.0"
        self._capabilities = ["shm_ring", "tcp_lz4", "chunked_frame"]
        self._debug_dumper = get_bridge_debug_dumper()

    @property
    def port(self) -> int:
        with self._state_lock:
            return self._config.port

    @property
    def selected_transport(self) -> Optional[str]:
        with self._state_lock:
            return self._transport

    def get_status(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "state": self._state,
                "enabled": self._config.enable_connection,
                "port": self._config.port,
                "transport": self._transport,
                "degraded": self._degraded,
                "session_id": self._active_session_id,
                "inflight_frames": len(self._inflight_frame_ids),
                "max_inflight_frames": self._max_inflight_frames,
                "target_stream_width": self._target_stream_width,
                "target_stream_height": self._target_stream_height,
                "last_error": dict(self._last_error) if self._last_error else None,
            }

    def get_stream_target_size_hint(self) -> Tuple[Optional[int], Optional[int]]:
        with self._state_lock:
            return self._target_stream_width, self._target_stream_height

    def configure(self, port: int, enable_connection: bool) -> bool:
        if not self._validate_port(port):
            return False

        with self._state_lock:
            self._config.port = int(port)
            self._config.enable_connection = bool(enable_connection)
            if not enable_connection:
                self._state = "disabled"
                self._transport = None
                self._degraded = False
                self._active_session_id = None
                self._inflight_frame_ids.clear()

        if not enable_connection:
            self._stop_worker()
            return True

        self._ensure_worker()
        with self._state_lock:
            if self._state == "disabled":
                self._state = "listening"
        self.request_connect()
        return True

    def request_connect(self) -> None:
        with self._state_lock:
            self._config.enable_connection = True
            if self._state == "disabled":
                self._state = "listening"
        self._ensure_worker()

    def disable_connection(self) -> None:
        with self._state_lock:
            self._config.enable_connection = False
            self._state = "disabled"
            self._transport = None
            self._degraded = False
            self._active_session_id = None
            self._inflight_frame_ids.clear()
            self._target_stream_width = None
            self._target_stream_height = None
            self._last_error = None
        self._stop_worker()

    def shutdown(self) -> None:
        self.disable_connection()
        self._clear_send_queue()

    def enqueue_control_message(self, message: Dict[str, Any], frame_id: Optional[int] = None) -> None:
        payload = encode_control_message(message)
        framed = encode_frame(payload, MAX_CONTROL_MESSAGE_BYTES)
        self._enqueue_frame_bytes(framed)
        if frame_id is not None:
            self._register_inflight_frame(frame_id)

    def enqueue_binary_chunk(self, payload: bytes, frame_id: Optional[int] = None) -> None:
        framed = encode_frame(payload, MAX_BINARY_FRAME_BYTES)
        if frame_id is not None and frame_id > 0:
            self._debug_dumper.dump_frame_bytes(
                frame_id=frame_id,
                stage="tcp_framed",
                payload=framed,
                meta={
                    "payloadBytes": int(len(payload)),
                    "framedBytes": int(len(framed)),
                },
            )
        self._enqueue_frame_bytes(framed)

    def _enqueue_frame_bytes(self, framed: bytes) -> None:
        try:
            self._send_queue.put_nowait(framed)
            return
        except queue.Full:
            pass

        try:
            self._send_queue.get_nowait()
        except queue.Empty:
            pass

        try:
            self._send_queue.put_nowait(framed)
        except queue.Full:
            self._set_error(E_SOCKET_IO, "发送队列已满，当前消息被丢弃")

    def _register_inflight_frame(self, frame_id: int) -> None:
        with self._state_lock:
            if len(self._inflight_frame_ids) >= self._max_inflight_frames:
                dropped = self._inflight_frame_ids.popleft()
                print(f"[SutuBridge] drop stale inflight frame {dropped}")
            self._inflight_frame_ids.append(int(frame_id))

    def _ack_inflight_frame(self, frame_id: int) -> None:
        with self._state_lock:
            try:
                self._inflight_frame_ids.remove(int(frame_id))
            except ValueError:
                pass

    def _ensure_worker(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_main,
            name="SutuBridgeClientWorker",
            daemon=True,
        )
        self._worker_thread.start()

    def _stop_worker(self) -> None:
        self._stop_event.set()
        self._close_socket()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        self._worker_thread = None
        self._stop_event.clear()

    def _worker_main(self) -> None:
        backoff_index = 0
        while not self._stop_event.is_set():
            fast_reconnect = False
            config = self._snapshot_config()
            if not config.enable_connection:
                self._set_state("disabled")
                if self._wait_stop(0.1):
                    break
                continue

            self._set_state("listening")

            try:
                self._run_session(config)
                backoff_index = 0
            except BridgeClientError as exc:
                if exc.code == E_STOP_REQUESTED:
                    break
                if self._stop_event.is_set():
                    self._clear_error()
                    break
                if not self._snapshot_config().enable_connection:
                    self._clear_error()
                    continue
                if self._is_expected_peer_close_error(exc):
                    # Sutu may proactively close socket after one-shot flow.
                    # Treat this as a recoverable reconnect signal instead of user-facing error.
                    fast_reconnect = True
                    self._clear_error()
                else:
                    self._set_error(exc.code, str(exc))
            except (BridgeProtocolError, FrameError) as exc:
                code = getattr(exc, "code", E_PROTO_MISMATCH)
                self._set_error(code, str(exc))
            except Exception as exc:
                self._set_error(E_SOCKET_IO, f"bridge 未知错误: {exc}")
            finally:
                self._close_socket()
                # Drop stale frames/messages from previous session before reconnect.
                self._clear_send_queue()

            if self._stop_event.is_set():
                break
            if not self._snapshot_config().enable_connection:
                continue

            if fast_reconnect:
                backoff_index = 0
                continue

            self._set_state("recovering")
            delay = RECONNECT_BACKOFF_SEC[min(backoff_index, len(RECONNECT_BACKOFF_SEC) - 1)]
            backoff_index = min(backoff_index + 1, len(RECONNECT_BACKOFF_SEC) - 1)
            if self._wait_stop(delay):
                break

        self._set_state("disabled")

    def _run_session(self, config: BridgeClientConfig) -> None:
        self._set_state("handshaking")
        decoder = FrameDecoder(MAX_CONTROL_MESSAGE_BYTES)
        try:
            sock = socket.create_connection(("127.0.0.1", config.port), timeout=2.0)
        except TimeoutError as exc:
            raise BridgeClientError(
                E_SOCKET_IO,
                f"连接 Sutu 超时，请确认 Sutu 已启动并监听端口 {config.port}",
            ) from exc
        except OSError as exc:
            raise BridgeClientError(E_SOCKET_IO, f"连接 Sutu 失败: {exc}") from exc
        sock.settimeout(0.05)
        self._set_socket(sock)
        self._clear_error()

        hello = build_hello(
            client_name=self._client_name,
            client_version=self._client_version,
            capabilities=self._capabilities,
        )
        self._send_control_now(sock, hello)

        ack_message = self._read_control_message_until(sock, decoder, timeout_s=HEARTBEAT_TIMEOUT_MS / 1000.0)
        ack_payload = parse_hello_ack(ack_message)
        if not ack_payload.get("accepted", False):
            reason = ack_payload.get("reason") or "服务端拒绝握手"
            raise BridgeClientError(E_PROTO_MISMATCH, reason)

        selected_transport = ack_payload.get("selectedTransport")
        if selected_transport not in (BRIDGE_TRANSPORT_SHM, BRIDGE_TRANSPORT_TCP_LZ4):
            raise BridgeClientError(E_PROTO_MISMATCH, f"未知传输模式: {selected_transport}")

        degraded = False
        with self._state_lock:
            self._session_counter += 1
            self._active_session_id = self._session_counter
            self._transport = selected_transport
            self._degraded = degraded
            self._state = "streaming"
            self._inflight_frame_ids.clear()
            self._target_stream_width = None
            self._target_stream_height = None

        last_peer_heartbeat = time.monotonic()
        last_sent_heartbeat = 0.0
        while not self._stop_event.is_set():
            if not self._snapshot_config().enable_connection:
                raise BridgeClientError(E_STOP_REQUESTED, "连接已禁用")

            self._flush_send_queue(sock)
            now = time.monotonic()
            if now - last_sent_heartbeat >= HEARTBEAT_INTERVAL_MS / 1000.0:
                self._send_control_now(sock, build_heartbeat())
                last_sent_heartbeat = now

            last_peer_heartbeat = self._try_read_incoming(sock, decoder, last_peer_heartbeat)
            if (time.monotonic() - last_peer_heartbeat) > HEARTBEAT_TIMEOUT_MS / 1000.0:
                raise BridgeClientError(E_HEARTBEAT_TIMEOUT, "心跳超时")

            time.sleep(0.01)

    def _try_read_incoming(
        self,
        sock: socket.socket,
        decoder: FrameDecoder,
        last_peer_heartbeat: float,
    ) -> float:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return last_peer_heartbeat
        except OSError as exc:
            raise BridgeClientError(E_SOCKET_IO, f"读取 socket 失败: {exc}") from exc

        if not chunk:
            raise BridgeClientError(E_SOCKET_IO, "socket 已被对端关闭")

        frames = decoder.push_bytes(chunk)
        for payload in frames:
            try:
                message = decode_control_message(payload)
            except BridgeProtocolError:
                continue
            message_type = message.get("type")
            if message_type == MESSAGE_TYPE_HEARTBEAT:
                last_peer_heartbeat = time.monotonic()
                heartbeat_payload = expect_message_type(message, MESSAGE_TYPE_HEARTBEAT)
                self._update_stream_target_hint(
                    heartbeat_payload.get("targetWidth"),
                    heartbeat_payload.get("targetHeight"),
                )
                continue
            if message_type == MESSAGE_TYPE_ACK:
                ack_payload = expect_message_type(message, MESSAGE_TYPE_ACK)
                frame_id = int(ack_payload.get("frameId", 0))
                if frame_id > 0:
                    self._ack_inflight_frame(frame_id)
                continue
            if message_type == MESSAGE_TYPE_ERROR:
                error_payload = expect_message_type(message, MESSAGE_TYPE_ERROR)
                code = str(error_payload.get("code") or E_PROTO_MISMATCH)
                text = str(error_payload.get("message") or "服务端错误")
                self._set_error(code, text)
                raise BridgeClientError(code, f"Sutu runtime error: {text}")
        return last_peer_heartbeat

    def _read_control_message_until(
        self,
        sock: socket.socket,
        decoder: FrameDecoder,
        timeout_s: float,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                raise BridgeClientError(E_STOP_REQUESTED, "停止请求")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                raise BridgeClientError(E_SOCKET_IO, f"读取握手消息失败: {exc}") from exc
            if not chunk:
                raise BridgeClientError(E_SOCKET_IO, "握手阶段连接被关闭")
            frames = decoder.push_bytes(chunk)
            for payload in frames:
                message = decode_control_message(payload)
                return message
        raise BridgeClientError(E_PROTO_MISMATCH, "握手超时")

    def _send_control_now(self, sock: socket.socket, message: Dict[str, Any]) -> None:
        payload = encode_control_message(message)
        framed = encode_frame(payload, MAX_CONTROL_MESSAGE_BYTES)
        self._send_raw(sock, framed)

    def _send_raw(self, sock: socket.socket, framed: bytes) -> None:
        try:
            sock.sendall(framed)
        except OSError as exc:
            raise BridgeClientError(E_SOCKET_IO, f"发送消息失败: {exc}") from exc

    def _flush_send_queue(self, sock: socket.socket) -> None:
        while True:
            try:
                framed = self._send_queue.get_nowait()
            except queue.Empty:
                return
            self._send_raw(sock, framed)

    def _snapshot_config(self) -> BridgeClientConfig:
        with self._state_lock:
            return BridgeClientConfig(
                port=self._config.port,
                enable_connection=self._config.enable_connection,
            )

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
            if state != "streaming":
                self._transport = None
                self._degraded = False
                self._active_session_id = None
                self._inflight_frame_ids.clear()
                self._target_stream_width = None
                self._target_stream_height = None

    def _update_stream_target_hint(self, target_width: Any, target_height: Any) -> None:
        next_width = _normalize_optional_positive_int(target_width)
        next_height = _normalize_optional_positive_int(target_height)

        with self._state_lock:
            if (self._target_stream_width, self._target_stream_height) == (next_width, next_height):
                return
            self._target_stream_width = next_width
            self._target_stream_height = next_height

    def _set_error(self, code: str, message: str) -> None:
        with self._state_lock:
            self._last_error = {"code": str(code), "message": str(message)}
        print(f"[SutuBridge][{code}] {message}")

    def _clear_error(self) -> None:
        with self._state_lock:
            self._last_error = None

    def _set_socket(self, sock: socket.socket) -> None:
        with self._socket_lock:
            self._socket = sock

    def _close_socket(self) -> None:
        with self._socket_lock:
            sock = self._socket
            self._socket = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    def _wait_stop(self, seconds: float) -> bool:
        end_time = time.monotonic() + seconds
        while time.monotonic() < end_time:
            if self._stop_event.is_set():
                return True
            time.sleep(0.05)
        return self._stop_event.is_set()

    def _clear_send_queue(self) -> None:
        while True:
            try:
                self._send_queue.get_nowait()
            except queue.Empty:
                break

    def _validate_port(self, port: int) -> bool:
        if 1024 <= int(port) <= 65535:
            return True
        self._set_error(E_PORT_INVALID, f"端口必须在 1024-65535 范围内，当前为 {port}")
        return False

    def _is_expected_peer_close_error(self, exc: BridgeClientError) -> bool:
        if exc.code != E_SOCKET_IO:
            return False
        text = str(exc)
        if "socket 已被对端关闭" in text:
            return True
        lowered = text.lower()
        if "winerror 10054" in lowered:
            return True
        if "forcibly closed by the remote host" in lowered:
            return True
        return False


def _normalize_optional_positive_int(value: Any) -> Optional[int]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = int(value)
    if parsed <= 0:
        return None
    return parsed


_BRIDGE_CLIENT: Optional[BridgeClient] = None


def get_bridge_client() -> BridgeClient:
    global _BRIDGE_CLIENT
    if _BRIDGE_CLIENT is None:
        _BRIDGE_CLIENT = BridgeClient()
    return _BRIDGE_CLIENT


def shutdown_bridge_client() -> None:
    global _BRIDGE_CLIENT
    if _BRIDGE_CLIENT is None:
        return
    _BRIDGE_CLIENT.shutdown()
    _BRIDGE_CLIENT = None


def get_addon_preferences(context: Optional[bpy.types.Context] = None):
    ctx = context or bpy.context
    if ctx is None:
        return None
    addons = getattr(getattr(ctx, "preferences", None), "addons", None)
    if addons is None:
        return None
    for key in (ADDON_ID, ADDON_SHORT_ID):
        addon = addons.get(key)
        if addon is not None:
            return addon.preferences

    try:
        for key, addon in addons.items():
            if key == ADDON_SHORT_ID or key.endswith(f".{ADDON_SHORT_ID}"):
                return addon.preferences
    except Exception:
        pass
    return None


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_startup_overrides() -> Dict[str, Any]:
    result: Dict[str, Any] = {}

    env_port = os.getenv("SUTU_BRIDGE_PORT")
    if env_port and env_port.isdigit():
        result["port"] = int(env_port)

    env_enable = _parse_bool(os.getenv("SUTU_BRIDGE_ENABLE"))
    if env_enable is not None:
        result["enable_connection"] = env_enable

    extra_args = []
    if "--" in sys.argv:
        extra_args = sys.argv[sys.argv.index("--") + 1 :]
    i = 0
    while i < len(extra_args):
        token = extra_args[i]
        if token == "--sutu-bridge-port" and i + 1 < len(extra_args):
            if extra_args[i + 1].isdigit():
                result["port"] = int(extra_args[i + 1])
            i += 2
            continue
        if token == "--sutu-bridge-enable":
            result["enable_connection"] = True
            i += 1
            continue
        if token == "--sutu-bridge-disable":
            result["enable_connection"] = False
            i += 1
            continue
        if token == "--sutu-bridge-connect-now":
            result["connect_now"] = True
            i += 1
            continue
        i += 1
    return result


def register() -> None:
    client = get_bridge_client()
    prefs = get_addon_preferences()

    port = 30121
    # Keep startup behavior deterministic: never auto-connect unless explicitly overridden.
    enable_connection = False

    if prefs is not None:
        port = int(getattr(prefs, "port", port))

    overrides = _parse_startup_overrides()
    port = int(overrides.get("port", port))
    enable_connection = bool(overrides.get("enable_connection", enable_connection))

    if not client.configure(port=port, enable_connection=enable_connection):
        return

    if bool(overrides.get("connect_now", False)):
        client.request_connect()


def unregister() -> None:
    shutdown_bridge_client()
