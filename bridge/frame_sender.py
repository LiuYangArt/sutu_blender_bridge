from __future__ import annotations

import threading
from typing import Optional

try:
    import lz4.frame as lz4_frame  # type: ignore
except Exception:  # pragma: no cover - Blender runtime dependency
    lz4_frame = None

from .messages import (
    BRIDGE_TRANSPORT_SHM,
    BRIDGE_TRANSPORT_TCP_LZ4,
    build_frame_meta,
    build_start_stream,
    build_stop_stream,
    now_millis,
)
from .shm_ring import SharedMemoryRing, default_ring_name

DEFAULT_RING_SLOTS = 3


class FrameSender:
    def __init__(self, client=None, ring_slots: int = DEFAULT_RING_SLOTS):
        from .client import get_bridge_client

        self._client = client or get_bridge_client()
        self._ring_slots = max(1, int(ring_slots))
        self._lock = threading.Lock()
        self._frame_id = 0
        self._streaming = False
        self._shm_ring: Optional[SharedMemoryRing] = None
        self._last_ring_name: Optional[str] = None
        self._last_ring_size: Optional[int] = None
        self._warned_missing_lz4 = False

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def start_stream(self, stream_id: Optional[str] = None) -> None:
        self._client.enqueue_control_message(build_start_stream(stream_id))
        self._streaming = True

    def stop_stream(self, reason: Optional[str] = None) -> None:
        if self._streaming:
            self._client.enqueue_control_message(build_stop_stream(reason))
        self._streaming = False
        self._close_shm_ring()

    def shutdown(self) -> None:
        self.stop_stream("addon_shutdown")

    def send_rgba_frame(
        self,
        width: int,
        height: int,
        pixels: bytes,
        stride: Optional[int] = None,
        timestamp_ms: Optional[int] = None,
    ) -> Optional[int]:
        if not self._streaming:
            return None
        if width <= 0 or height <= 0:
            return None

        stride = int(stride if stride is not None else width * 4)
        required_len = stride * height
        if len(pixels) < required_len:
            raise ValueError(f"像素数据长度不足: {len(pixels)} < {required_len}")

        payload = pixels[:required_len]
        ts_ms = int(timestamp_ms if timestamp_ms is not None else now_millis())

        with self._lock:
            self._frame_id += 1
            frame_id = self._frame_id

        transport = self._client.selected_transport or BRIDGE_TRANSPORT_TCP_LZ4
        if transport == BRIDGE_TRANSPORT_SHM:
            slot = self._write_frame_to_shm(payload, required_len)
            self._client.enqueue_control_message(
                build_frame_meta(
                    frame_id=frame_id,
                    width=width,
                    height=height,
                    stride=stride,
                    transport=BRIDGE_TRANSPORT_SHM,
                    shm_slot=slot,
                    chunk_size=None,
                    timestamp_ms=ts_ms,
                )
            )
            return frame_id

        compressed = self._compress_tcp_payload(payload)
        self._client.enqueue_control_message(
            build_frame_meta(
                frame_id=frame_id,
                width=width,
                height=height,
                stride=stride,
                transport=BRIDGE_TRANSPORT_TCP_LZ4,
                shm_slot=None,
                chunk_size=len(compressed),
                timestamp_ms=ts_ms,
            )
        )
        self._client.enqueue_binary_chunk(compressed)
        return frame_id

    def _write_frame_to_shm(self, payload: bytes, required_len: int) -> int:
        ring_name = default_ring_name(self._client.port)
        if (
            self._shm_ring is None
            or self._last_ring_name != ring_name
            or self._last_ring_size != required_len
        ):
            self._close_shm_ring()
            self._shm_ring = SharedMemoryRing(
                name=ring_name,
                slot_count=self._ring_slots,
                slot_size=required_len,
                create=True,
            )
            self._last_ring_name = ring_name
            self._last_ring_size = required_len
        return self._shm_ring.write_next(payload)

    def _close_shm_ring(self) -> None:
        if self._shm_ring is None:
            return
        self._shm_ring.close(unlink=True)
        self._shm_ring = None
        self._last_ring_name = None
        self._last_ring_size = None

    def _compress_tcp_payload(self, payload: bytes) -> bytes:
        if lz4_frame is None:
            if not self._warned_missing_lz4:
                print("[SutuBridge] lz4 未安装，tcp_lz4 将退化为原始字节发送")
                self._warned_missing_lz4 = True
            return payload
        return lz4_frame.compress(payload)


_FRAME_SENDER: Optional[FrameSender] = None


def get_frame_sender() -> FrameSender:
    global _FRAME_SENDER
    if _FRAME_SENDER is None:
        _FRAME_SENDER = FrameSender()
    return _FRAME_SENDER


def shutdown_frame_sender() -> None:
    global _FRAME_SENDER
    if _FRAME_SENDER is None:
        return
    _FRAME_SENDER.shutdown()
    _FRAME_SENDER = None
