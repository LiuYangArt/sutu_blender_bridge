from __future__ import annotations

import struct
import threading
from dataclasses import dataclass
from multiprocessing import shared_memory

SHM_HEADER_BYTES = 24
_U32_MAX = 0xFFFFFFFF


@dataclass(frozen=True)
class SharedMemoryRingLayout:
    name: str
    slot_count: int
    slot_size: int

    @property
    def total_size(self) -> int:
        return self.slot_count * self.slot_size


class SharedMemoryRing:
    def __init__(
        self,
        name: str,
        slot_count: int,
        slot_size: int,
        create: bool = True,
    ):
        if slot_count <= 0:
            raise ValueError("slot_count 必须大于 0")
        if slot_size <= SHM_HEADER_BYTES:
            raise ValueError(f"slot_size 必须大于 header 大小({SHM_HEADER_BYTES})")

        self.layout = SharedMemoryRingLayout(name=name, slot_count=slot_count, slot_size=slot_size)
        self._lock = threading.Lock()
        self._owner = False
        self._next_slot = 0
        self._closed = False
        self._slot_seq = [0] * slot_count

        try:
            self._shm = shared_memory.SharedMemory(
                name=name,
                create=create,
                size=self.layout.total_size,
            )
            self._owner = create
        except FileExistsError:
            self._shm = shared_memory.SharedMemory(name=name, create=False)
            self._owner = False
            if self._shm.size < self.layout.total_size:
                current_size = self._shm.size
                self._shm.close()
                raise ValueError(
                    f"已有共享内存容量不足: {current_size} < {self.layout.total_size}"
                )

    @property
    def name(self) -> str:
        return self.layout.name

    @property
    def slot_count(self) -> int:
        return self.layout.slot_count

    @property
    def slot_size(self) -> int:
        return self.layout.slot_size

    @property
    def is_owner(self) -> bool:
        return self._owner

    @property
    def payload_capacity(self) -> int:
        return self.slot_size - SHM_HEADER_BYTES

    def write_slot(
        self,
        slot_index: int,
        payload: bytes,
        frame_id: int,
        timestamp_ms: int,
    ) -> int:
        self._assert_open()
        if slot_index < 0 or slot_index >= self.slot_count:
            raise IndexError(f"slot 越界: {slot_index}")
        payload_len = len(payload)
        if payload_len > self.payload_capacity:
            raise ValueError(f"payload 太大: {payload_len} > {self.payload_capacity}")

        offset = slot_index * self.slot_size
        with self._lock:
            committed_seq = self._slot_seq[slot_index] & _U32_MAX
            if committed_seq & 1:
                committed_seq = (committed_seq + 1) & _U32_MAX
            writing_seq = (committed_seq + 1) & _U32_MAX
            next_committed_seq = (committed_seq + 2) & _U32_MAX

            self._write_u32(offset, writing_seq)
            self._write_u32(offset + 4, payload_len)
            self._write_u64(offset + 8, int(frame_id))
            self._write_u64(offset + 16, int(timestamp_ms))
            self._shm.buf[offset + SHM_HEADER_BYTES : offset + SHM_HEADER_BYTES + payload_len] = payload
            self._write_u32(offset, next_committed_seq)
            self._slot_seq[slot_index] = next_committed_seq
        return slot_index

    def write_next(self, payload: bytes, frame_id: int, timestamp_ms: int) -> int:
        self._assert_open()
        with self._lock:
            slot_index = self._next_slot
            self._next_slot = (self._next_slot + 1) % self.slot_count
        return self.write_slot(slot_index, payload, frame_id, timestamp_ms)

    def read_slot(self, slot_index: int, byte_size: int) -> bytes:
        self._assert_open()
        if slot_index < 0 or slot_index >= self.slot_count:
            raise IndexError(f"slot 越界: {slot_index}")
        if byte_size < 0 or byte_size > self.payload_capacity:
            raise ValueError(f"byte_size 非法: {byte_size}")
        offset = slot_index * self.slot_size + SHM_HEADER_BYTES
        with self._lock:
            return bytes(self._shm.buf[offset : offset + byte_size])

    def _write_u32(self, offset: int, value: int) -> None:
        self._shm.buf[offset : offset + 4] = struct.pack("<I", int(value) & _U32_MAX)

    def _write_u64(self, offset: int, value: int) -> None:
        self._shm.buf[offset : offset + 8] = struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)

    def close(self, unlink: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._shm.close()
        finally:
            if unlink and self._owner:
                try:
                    self._shm.unlink()
                except FileNotFoundError:
                    pass

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("共享内存环已关闭")


def default_ring_name(port: int, slot_size: int) -> str:
    return f"sutu_bridge_v2_{int(port)}_{int(slot_size)}"
