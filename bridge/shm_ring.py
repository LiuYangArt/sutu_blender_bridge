from __future__ import annotations

import threading
from dataclasses import dataclass
from multiprocessing import shared_memory


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
        if slot_size <= 0:
            raise ValueError("slot_size 必须大于 0")

        self.layout = SharedMemoryRingLayout(name=name, slot_count=slot_count, slot_size=slot_size)
        self._lock = threading.Lock()
        self._owner = False
        self._next_slot = 0
        self._closed = False

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

    def write_slot(self, slot_index: int, payload: bytes) -> int:
        self._assert_open()
        if slot_index < 0 or slot_index >= self.slot_count:
            raise IndexError(f"slot 越界: {slot_index}")
        payload_len = len(payload)
        if payload_len > self.slot_size:
            raise ValueError(f"payload 太大: {payload_len} > {self.slot_size}")

        offset = slot_index * self.slot_size
        with self._lock:
            self._shm.buf[offset : offset + payload_len] = payload
        return slot_index

    def write_next(self, payload: bytes) -> int:
        self._assert_open()
        with self._lock:
            slot_index = self._next_slot
            self._next_slot = (self._next_slot + 1) % self.slot_count
        return self.write_slot(slot_index, payload)

    def read_slot(self, slot_index: int, byte_size: int) -> bytes:
        self._assert_open()
        if slot_index < 0 or slot_index >= self.slot_count:
            raise IndexError(f"slot 越界: {slot_index}")
        if byte_size < 0 or byte_size > self.slot_size:
            raise ValueError(f"byte_size 非法: {byte_size}")
        offset = slot_index * self.slot_size
        with self._lock:
            return bytes(self._shm.buf[offset : offset + byte_size])

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


def default_ring_name(port: int) -> str:
    return f"sutu_bridge_v2_{int(port)}"
