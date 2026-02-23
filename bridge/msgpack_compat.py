from __future__ import annotations

import struct
from typing import Any


def packb(obj: Any, use_bin_type: bool = True) -> bytes:
    if not use_bin_type:
        raise ValueError("仅支持 use_bin_type=True")
    return _encode(obj)


def unpackb(payload: bytes, raw: bool = False) -> Any:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload 必须是 bytes-like")
    reader = _Reader(bytes(payload))
    value = _decode(reader, raw=raw)
    if reader.offset != len(reader.data):
        raise ValueError("解码后仍有未消费数据")
    return value


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, length: int) -> bytes:
        end = self.offset + length
        if end > len(self.data):
            raise ValueError("MessagePack 数据截断")
        part = self.data[self.offset : end]
        self.offset = end
        return part


def _encode(obj: Any) -> bytes:
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        return _encode_int(obj)
    if isinstance(obj, float):
        return b"\xcb" + struct.pack(">d", obj)
    if isinstance(obj, str):
        return _encode_str(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return _encode_bin(bytes(obj))
    if isinstance(obj, (list, tuple)):
        return _encode_array(obj)
    if isinstance(obj, dict):
        return _encode_map(obj)
    raise TypeError(f"不支持的类型: {type(obj)!r}")


def _encode_int(value: int) -> bytes:
    if 0 <= value <= 0x7F:
        return struct.pack("B", value)
    if -32 <= value < 0:
        return struct.pack("B", value & 0xFF)
    if 0 <= value <= 0xFF:
        return b"\xcc" + struct.pack(">B", value)
    if 0 <= value <= 0xFFFF:
        return b"\xcd" + struct.pack(">H", value)
    if 0 <= value <= 0xFFFFFFFF:
        return b"\xce" + struct.pack(">I", value)
    if 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        return b"\xcf" + struct.pack(">Q", value)
    if -0x80 <= value < 0:
        return b"\xd0" + struct.pack(">b", value)
    if -0x8000 <= value < -0x80:
        return b"\xd1" + struct.pack(">h", value)
    if -0x80000000 <= value < -0x8000:
        return b"\xd2" + struct.pack(">i", value)
    if -0x8000000000000000 <= value < -0x80000000:
        return b"\xd3" + struct.pack(">q", value)
    raise OverflowError("整数超出 MessagePack int64/uint64 范围")


def _encode_str(value: str) -> bytes:
    encoded = value.encode("utf-8")
    length = len(encoded)
    if length <= 31:
        return struct.pack("B", 0xA0 | length) + encoded
    if length <= 0xFF:
        return b"\xd9" + struct.pack(">B", length) + encoded
    if length <= 0xFFFF:
        return b"\xda" + struct.pack(">H", length) + encoded
    if length <= 0xFFFFFFFF:
        return b"\xdb" + struct.pack(">I", length) + encoded
    raise OverflowError("字符串过长")


def _encode_bin(value: bytes) -> bytes:
    length = len(value)
    if length <= 0xFF:
        return b"\xc4" + struct.pack(">B", length) + value
    if length <= 0xFFFF:
        return b"\xc5" + struct.pack(">H", length) + value
    if length <= 0xFFFFFFFF:
        return b"\xc6" + struct.pack(">I", length) + value
    raise OverflowError("二进制数据过长")


def _encode_array(values: Any) -> bytes:
    items = b"".join(_encode(v) for v in values)
    length = len(values)
    if length <= 15:
        return struct.pack("B", 0x90 | length) + items
    if length <= 0xFFFF:
        return b"\xdc" + struct.pack(">H", length) + items
    if length <= 0xFFFFFFFF:
        return b"\xdd" + struct.pack(">I", length) + items
    raise OverflowError("数组过长")


def _encode_map(values: dict[Any, Any]) -> bytes:
    body_parts = []
    for key, value in values.items():
        body_parts.append(_encode(key))
        body_parts.append(_encode(value))
    body = b"".join(body_parts)
    length = len(values)
    if length <= 15:
        return struct.pack("B", 0x80 | length) + body
    if length <= 0xFFFF:
        return b"\xde" + struct.pack(">H", length) + body
    if length <= 0xFFFFFFFF:
        return b"\xdf" + struct.pack(">I", length) + body
    raise OverflowError("对象字段过多")


def _decode(reader: _Reader, raw: bool) -> Any:
    marker = reader.read(1)[0]

    if marker <= 0x7F:
        return marker
    if marker >= 0xE0:
        return marker - 0x100
    if 0xA0 <= marker <= 0xBF:
        return _decode_str(reader.read(marker & 0x1F), raw=raw)
    if 0x90 <= marker <= 0x9F:
        return [_decode(reader, raw=raw) for _ in range(marker & 0x0F)]
    if 0x80 <= marker <= 0x8F:
        return _decode_map_items(reader, marker & 0x0F, raw=raw)

    if marker == 0xC0:
        return None
    if marker == 0xC2:
        return False
    if marker == 0xC3:
        return True
    if marker == 0xC4:
        return reader.read(struct.unpack(">B", reader.read(1))[0])
    if marker == 0xC5:
        return reader.read(struct.unpack(">H", reader.read(2))[0])
    if marker == 0xC6:
        return reader.read(struct.unpack(">I", reader.read(4))[0])
    if marker == 0xCA:
        return struct.unpack(">f", reader.read(4))[0]
    if marker == 0xCB:
        return struct.unpack(">d", reader.read(8))[0]
    if marker == 0xCC:
        return struct.unpack(">B", reader.read(1))[0]
    if marker == 0xCD:
        return struct.unpack(">H", reader.read(2))[0]
    if marker == 0xCE:
        return struct.unpack(">I", reader.read(4))[0]
    if marker == 0xCF:
        return struct.unpack(">Q", reader.read(8))[0]
    if marker == 0xD0:
        return struct.unpack(">b", reader.read(1))[0]
    if marker == 0xD1:
        return struct.unpack(">h", reader.read(2))[0]
    if marker == 0xD2:
        return struct.unpack(">i", reader.read(4))[0]
    if marker == 0xD3:
        return struct.unpack(">q", reader.read(8))[0]
    if marker == 0xD9:
        return _decode_str(reader.read(struct.unpack(">B", reader.read(1))[0]), raw=raw)
    if marker == 0xDA:
        return _decode_str(reader.read(struct.unpack(">H", reader.read(2))[0]), raw=raw)
    if marker == 0xDB:
        return _decode_str(reader.read(struct.unpack(">I", reader.read(4))[0]), raw=raw)
    if marker == 0xDC:
        length = struct.unpack(">H", reader.read(2))[0]
        return [_decode(reader, raw=raw) for _ in range(length)]
    if marker == 0xDD:
        length = struct.unpack(">I", reader.read(4))[0]
        return [_decode(reader, raw=raw) for _ in range(length)]
    if marker == 0xDE:
        return _decode_map_items(reader, struct.unpack(">H", reader.read(2))[0], raw=raw)
    if marker == 0xDF:
        return _decode_map_items(reader, struct.unpack(">I", reader.read(4))[0], raw=raw)

    raise ValueError(f"不支持的 MessagePack 标记: 0x{marker:02x}")


def _decode_map_items(reader: _Reader, length: int, raw: bool) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for _ in range(length):
        key = _decode(reader, raw=raw)
        value = _decode(reader, raw=raw)
        result[key] = value
    return result


def _decode_str(data: bytes, raw: bool) -> Any:
    if raw:
        return data
    return data.decode("utf-8")
