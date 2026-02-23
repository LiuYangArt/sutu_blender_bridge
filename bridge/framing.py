from __future__ import annotations

from typing import List

from .messages import E_MSG_TOO_LARGE, E_PROTO_MISMATCH, MAX_CONTROL_MESSAGE_BYTES


class FrameError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FrameDecoder:
    def __init__(self, max_payload_len: int = MAX_CONTROL_MESSAGE_BYTES):
        self.max_payload_len = max_payload_len
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def push_bytes(self, chunk: bytes) -> List[bytes]:
        if not chunk:
            return []
        self._buffer.extend(chunk)
        frames: List[bytes] = []

        while True:
            if len(self._buffer) < 4:
                break
            payload_len = int.from_bytes(self._buffer[0:4], byteorder="big", signed=False)
            if payload_len > self.max_payload_len:
                raise FrameError(
                    E_MSG_TOO_LARGE,
                    f"分帧消息超出上限: {payload_len} > {self.max_payload_len}",
                )

            frame_len = 4 + payload_len
            if len(self._buffer) < frame_len:
                break

            payload = bytes(self._buffer[4:frame_len])
            del self._buffer[:frame_len]
            frames.append(payload)

        return frames


def encode_frame(payload: bytes, max_payload_len: int = MAX_CONTROL_MESSAGE_BYTES) -> bytes:
    if payload is None:
        raise FrameError(E_PROTO_MISMATCH, "payload 不能为空")
    payload_len = len(payload)
    if payload_len > max_payload_len:
        raise FrameError(
            E_MSG_TOO_LARGE,
            f"payload 超出上限: {payload_len} > {max_payload_len}",
        )
    return payload_len.to_bytes(4, byteorder="big", signed=False) + payload
