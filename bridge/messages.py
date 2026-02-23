from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Optional

try:
    import msgpack  # type: ignore
except Exception:  # pragma: no cover - Blender runtime dependency
    from . import msgpack_compat as msgpack

PROTOCOL_MAGIC = "SUTU_BRIDGE_V2"
PROTOCOL_VERSION = 2
MAX_CONTROL_MESSAGE_BYTES = 1024 * 1024
HEARTBEAT_INTERVAL_MS = 1000
HEARTBEAT_TIMEOUT_MS = 5000
MAX_INFLIGHT_FRAMES = 3

BRIDGE_MODE_AUTO = "auto"
BRIDGE_MODE_MANUAL = "manual"

BRIDGE_TRANSPORT_SHM = "shm"
BRIDGE_TRANSPORT_TCP_LZ4 = "tcp_lz4"

MESSAGE_TYPE_HELLO = "hello"
MESSAGE_TYPE_HELLO_ACK = "hello_ack"
MESSAGE_TYPE_START_STREAM = "start_stream"
MESSAGE_TYPE_STOP_STREAM = "stop_stream"
MESSAGE_TYPE_FRAME_META = "frame_meta"
MESSAGE_TYPE_ACK = "ack"
MESSAGE_TYPE_ERROR = "error"
MESSAGE_TYPE_HEARTBEAT = "heartbeat"

DEFAULT_CAPABILITIES = ("shm_ring", "tcp_lz4", "chunked_frame")

E_PORT_INVALID = "E_PORT_INVALID"
E_PORT_IN_USE = "E_PORT_IN_USE"
E_PROTO_MISMATCH = "E_PROTO_MISMATCH"
E_MSG_TOO_LARGE = "E_MSG_TOO_LARGE"
E_SHM_ATTACH_FAIL = "E_SHM_ATTACH_FAIL"
E_LAUNCH_FAILED = "E_LAUNCH_FAILED"
E_SOCKET_IO = "E_SOCKET_IO"
E_HEARTBEAT_TIMEOUT = "E_HEARTBEAT_TIMEOUT"
E_STOP_REQUESTED = "E_STOP_REQUESTED"

ControlMessage = Dict[str, Any]
_CONTROL_MESSAGE_TYPES = (
    MESSAGE_TYPE_HELLO,
    MESSAGE_TYPE_HELLO_ACK,
    MESSAGE_TYPE_START_STREAM,
    MESSAGE_TYPE_STOP_STREAM,
    MESSAGE_TYPE_FRAME_META,
    MESSAGE_TYPE_ACK,
    MESSAGE_TYPE_ERROR,
    MESSAGE_TYPE_HEARTBEAT,
)
_CONTROL_MESSAGE_TYPE_SET = set(_CONTROL_MESSAGE_TYPES)
_CONTROL_MESSAGE_INDEX_TO_TYPE = {idx: name for idx, name in enumerate(_CONTROL_MESSAGE_TYPES)}
_PAYLOAD_FIELDS_BY_TYPE = {
    MESSAGE_TYPE_HELLO: ("magic", "protocolVersion", "capabilities", "clientName", "clientVersion"),
    MESSAGE_TYPE_HELLO_ACK: ("accepted", "serverVersion", "selectedTransport", "reason"),
    MESSAGE_TYPE_START_STREAM: ("streamId",),
    MESSAGE_TYPE_STOP_STREAM: ("reason",),
    MESSAGE_TYPE_FRAME_META: (
        "frameId",
        "width",
        "height",
        "stride",
        "pixelFormat",
        "transport",
        "shmSlot",
        "chunkSize",
        "timestampMs",
    ),
    MESSAGE_TYPE_ACK: ("frameId",),
    MESSAGE_TYPE_ERROR: ("code", "message"),
    MESSAGE_TYPE_HEARTBEAT: ("timestampMs",),
}


def _type_from_compact(value: Any) -> Optional[str]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized if normalized in _CONTROL_MESSAGE_TYPE_SET else None
    if isinstance(value, int):
        return _CONTROL_MESSAGE_INDEX_TO_TYPE.get(int(value))
    return None


def _normalize_transport_value(value: Any) -> Any:
    if isinstance(value, str):
        if value in (BRIDGE_TRANSPORT_SHM, BRIDGE_TRANSPORT_TCP_LZ4):
            return value
        lowered = value.strip().lower()
        if lowered in {"shm", "shared_memory", "shared-memory"}:
            return BRIDGE_TRANSPORT_SHM
        if lowered in {"tcp_lz4", "tcp-lz4", "tcplz4"}:
            return BRIDGE_TRANSPORT_TCP_LZ4
        return value
    if isinstance(value, int):
        if value == 0:
            return BRIDGE_TRANSPORT_SHM
        if value == 1:
            return BRIDGE_TRANSPORT_TCP_LZ4
    return value


def _normalize_payload(message_type: str, payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        normalized = dict(payload)
    elif isinstance(payload, (list, tuple)):
        field_names = _PAYLOAD_FIELDS_BY_TYPE.get(message_type)
        if field_names is None:
            raise BridgeProtocolError(E_PROTO_MISMATCH, "未知消息类型，无法解析 payload")
        normalized = {}
        for idx, field_name in enumerate(field_names):
            if idx < len(payload):
                normalized[field_name] = payload[idx]
    else:
        raise BridgeProtocolError(E_PROTO_MISMATCH, "消息 payload 不是对象")

    if message_type == MESSAGE_TYPE_HELLO_ACK:
        normalized["selectedTransport"] = _normalize_transport_value(normalized.get("selectedTransport"))
    elif message_type == MESSAGE_TYPE_FRAME_META:
        normalized["transport"] = _normalize_transport_value(normalized.get("transport"))
    return normalized


def _normalize_control_message(message: Any) -> ControlMessage:
    if isinstance(message, dict):
        if "type" in message:
            message_type = _type_from_compact(message.get("type"))
            if message_type is None:
                raise BridgeProtocolError(E_PROTO_MISMATCH, "消息类型不受支持")
            payload = _normalize_payload(message_type, message.get("payload"))
            return {"type": message_type, "payload": payload}
        if len(message) == 1:
            variant_key = next(iter(message.keys()))
            message_type = _type_from_compact(variant_key)
            if message_type is not None:
                payload = _normalize_payload(message_type, message.get(variant_key))
                return {"type": message_type, "payload": payload}

    if isinstance(message, (list, tuple)) and len(message) == 2:
        message_type = _type_from_compact(message[0])
        if message_type is not None:
            payload = _normalize_payload(message_type, message[1])
            return {"type": message_type, "payload": payload}

    raise BridgeProtocolError(E_PROTO_MISMATCH, "控制消息结构无效")


class BridgeProtocolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now_millis() -> int:
    return int(time.time() * 1000)


def _require_msgpack():
    return msgpack


def encode_control_message(message: ControlMessage) -> bytes:
    packer = _require_msgpack()
    try:
        payload = packer.packb(message, use_bin_type=True)
    except Exception as exc:
        raise BridgeProtocolError(
            E_PROTO_MISMATCH,
            f"控制消息编码失败: {exc}",
        ) from exc
    if len(payload) > MAX_CONTROL_MESSAGE_BYTES:
        raise BridgeProtocolError(
            E_MSG_TOO_LARGE,
            f"控制消息超出上限: {len(payload)} > {MAX_CONTROL_MESSAGE_BYTES}",
        )
    return payload


def decode_control_message(payload: bytes) -> ControlMessage:
    packer = _require_msgpack()
    try:
        message = packer.unpackb(payload, raw=False)
    except Exception as exc:
        raise BridgeProtocolError(
            E_PROTO_MISMATCH,
            f"控制消息解码失败: {exc}",
        ) from exc
    return _normalize_control_message(message)


def expect_message_type(message: ControlMessage, expected_type: str) -> Dict[str, Any]:
    message_type = message.get("type")
    if message_type != expected_type:
        raise BridgeProtocolError(
            E_PROTO_MISMATCH,
            f"消息类型不匹配，期望 {expected_type}，实际 {message_type}",
        )
    payload = message.get("payload")
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise BridgeProtocolError(E_PROTO_MISMATCH, "消息 payload 不是对象")
    return payload


def build_hello(
    client_name: str,
    client_version: str,
    capabilities: Optional[Iterable[str]] = None,
) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_HELLO,
        "payload": {
            "magic": PROTOCOL_MAGIC,
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": list(capabilities or DEFAULT_CAPABILITIES),
            "clientName": client_name,
            "clientVersion": client_version,
        },
    }


def build_start_stream(stream_id: Optional[str] = None) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_START_STREAM,
        "payload": {
            "streamId": stream_id,
        },
    }


def build_stop_stream(reason: Optional[str] = None) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_STOP_STREAM,
        "payload": {
            "reason": reason,
        },
    }


def build_frame_meta(
    frame_id: int,
    width: int,
    height: int,
    stride: int,
    transport: str,
    timestamp_ms: int,
    shm_slot: Optional[int] = None,
    chunk_size: Optional[int] = None,
    pixel_format: str = "rgba8",
) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_FRAME_META,
        "payload": {
            "frameId": int(frame_id),
            "width": int(width),
            "height": int(height),
            "stride": int(stride),
            "pixelFormat": pixel_format,
            "transport": transport,
            "shmSlot": int(shm_slot) if shm_slot is not None else None,
            "chunkSize": int(chunk_size) if chunk_size is not None else None,
            "timestampMs": int(timestamp_ms),
        },
    }


def build_ack(frame_id: int) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_ACK,
        "payload": {
            "frameId": int(frame_id),
        },
    }


def build_error(code: str, message: str) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_ERROR,
        "payload": {
            "code": code,
            "message": message,
        },
    }


def build_heartbeat(timestamp_ms: Optional[int] = None) -> ControlMessage:
    return {
        "type": MESSAGE_TYPE_HEARTBEAT,
        "payload": {
            "timestampMs": int(timestamp_ms if timestamp_ms is not None else now_millis()),
        },
    }


def parse_hello_ack(message: ControlMessage) -> Dict[str, Any]:
    payload = expect_message_type(message, MESSAGE_TYPE_HELLO_ACK)
    accepted = bool(payload.get("accepted", False))
    selected_transport = payload.get("selectedTransport")
    if accepted and selected_transport not in (BRIDGE_TRANSPORT_SHM, BRIDGE_TRANSPORT_TCP_LZ4):
        raise BridgeProtocolError(
            E_PROTO_MISMATCH,
            f"服务端返回未知传输模式: {selected_transport}",
        )
    return {
        "accepted": accepted,
        "serverVersion": payload.get("serverVersion"),
        "selectedTransport": selected_transport,
        "reason": payload.get("reason"),
    }
