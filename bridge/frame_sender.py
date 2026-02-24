from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import lz4.frame as lz4_frame  # type: ignore
except Exception:  # pragma: no cover - Blender runtime dependency
    lz4_frame = None

from .debug_dump import get_bridge_debug_dumper
from .messages import (
    BRIDGE_TRANSPORT_SHM,
    BRIDGE_TRANSPORT_TCP_LZ4,
    build_frame_meta,
    build_start_stream,
    build_stop_stream,
    now_millis,
)
from .shm_ring import SHM_HEADER_BYTES, SharedMemoryRing, default_ring_name

DEFAULT_RING_SLOTS = 4
RETIRED_RING_TTL_SEC = 2.0


@dataclass
class _RetiredRing:
    ring: SharedMemoryRing
    expires_at: float
    name: str


class FrameSender:
    def __init__(self, client=None, ring_slots: int = DEFAULT_RING_SLOTS):
        from .client import get_bridge_client

        self._client = client or get_bridge_client()
        self._ring_slots = DEFAULT_RING_SLOTS
        self._lock = threading.Lock()
        self._frame_id = 0
        self._streaming = False
        self._shm_ring: Optional[SharedMemoryRing] = None
        self._last_ring_name: Optional[str] = None
        self._last_ring_size: Optional[int] = None
        self._retired_rings: list[_RetiredRing] = []
        self._warned_missing_lz4 = False
        self._tried_auto_install_lz4 = False
        self._auto_install_lz4 = True
        self._debug_dumper = get_bridge_debug_dumper()

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def start_stream(self, stream_id: Optional[str] = None) -> None:
        self._sync_runtime_preferences()
        self._debug_dumper.start_stream_session()
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
        self._sync_runtime_preferences()
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
        self._debug_dumper.dump_frame_bytes(
            frame_id=frame_id,
            stage="rgba_raw",
            payload=payload,
            meta={
                "width": int(width),
                "height": int(height),
                "stride": int(stride),
                "requiredBytes": int(required_len),
                "timestampMs": int(ts_ms),
                "transport": transport,
            },
        )
        if transport == BRIDGE_TRANSPORT_SHM:
            slot = self._write_frame_to_shm(
                payload=payload,
                required_len=required_len,
                frame_id=frame_id,
                timestamp_ms=ts_ms,
            )
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
        self._debug_dumper.dump_frame_bytes(
            frame_id=frame_id,
            stage="tcp_chunk",
            payload=compressed,
            meta={
                "width": int(width),
                "height": int(height),
                "stride": int(stride),
                "rawBytes": int(len(payload)),
                "chunkBytes": int(len(compressed)),
                "lz4Available": bool(lz4_frame is not None),
                "compressionRatio": round(float(len(compressed)) / float(len(payload)), 6) if payload else 0.0,
            },
        )
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
        self._client.enqueue_binary_chunk(compressed, frame_id=frame_id)
        return frame_id

    def _write_frame_to_shm(
        self,
        payload: bytes,
        required_len: int,
        frame_id: int,
        timestamp_ms: int,
    ) -> int:
        self._cleanup_retired_rings()
        slot_size = SHM_HEADER_BYTES + required_len
        ring_name = default_ring_name(self._client.port, slot_size)
        if (
            self._shm_ring is None
            or self._last_ring_name != ring_name
            or self._last_ring_size != slot_size
        ):
            self._retire_active_ring()
            self._shm_ring = SharedMemoryRing(
                name=ring_name,
                slot_count=self._ring_slots,
                slot_size=slot_size,
                create=True,
            )
            self._last_ring_name = ring_name
            self._last_ring_size = slot_size
            print(
                f"[SutuBridge] shm ring ready name={ring_name} slot_count={self._ring_slots} slot_size={slot_size}"
            )
        return self._shm_ring.write_next(payload, frame_id=frame_id, timestamp_ms=timestamp_ms)

    def _close_shm_ring(self) -> None:
        if self._shm_ring is not None:
            try:
                self._shm_ring.close(unlink=True)
            except Exception as exc:
                print(f"[SutuBridge] close shm ring failed: {exc}")
            self._shm_ring = None
            self._last_ring_name = None
            self._last_ring_size = None

        if self._retired_rings:
            for retired in self._retired_rings:
                try:
                    retired.ring.close(unlink=True)
                except Exception as exc:
                    print(f"[SutuBridge] close retired shm ring failed name={retired.name}: {exc}")
            self._retired_rings.clear()

    def _retire_active_ring(self) -> None:
        if self._shm_ring is None:
            return
        retired_name = self._last_ring_name or self._shm_ring.name
        # Keep old ring alive for a short grace period so peer can still attach
        # to in-flight frame_meta that was queued before resize/stride change.
        expires_at = time.monotonic() + RETIRED_RING_TTL_SEC
        self._retired_rings.append(
            _RetiredRing(ring=self._shm_ring, expires_at=expires_at, name=retired_name)
        )
        print(f"[SutuBridge] shm ring retired name={retired_name}, keep_alive={RETIRED_RING_TTL_SEC:.1f}s")
        self._shm_ring = None
        self._last_ring_name = None
        self._last_ring_size = None

    def _cleanup_retired_rings(self) -> None:
        if not self._retired_rings:
            return
        now = time.monotonic()
        keep: list[_RetiredRing] = []
        for retired in self._retired_rings:
            if now < retired.expires_at:
                keep.append(retired)
                continue
            try:
                retired.ring.close(unlink=True)
            except Exception as exc:
                print(f"[SutuBridge] cleanup retired shm ring failed name={retired.name}: {exc}")
        self._retired_rings = keep

    def _compress_tcp_payload(self, payload: bytes) -> bytes:
        self._maybe_auto_install_lz4()
        if lz4_frame is None:
            if not self._warned_missing_lz4:
                print("[SutuBridge] lz4 未安装，tcp_lz4 将退化为原始字节发送")
                self._warned_missing_lz4 = True
            return payload
        return lz4_frame.compress(payload)

    def _sync_runtime_preferences(self) -> None:
        try:
            from .client import get_addon_preferences

            prefs = get_addon_preferences()
        except Exception:
            prefs = None

        dump_enabled = False
        dump_max_frames = 3
        dump_dir = ""
        auto_install = True
        if prefs is not None:
            dump_enabled = bool(getattr(prefs, "dump_frame_files", False))
            dump_max_frames = max(1, int(getattr(prefs, "dump_max_frames", 3)))
            dump_dir_text = str(getattr(prefs, "dump_directory", "")).strip()
            dump_dir = dump_dir_text
            auto_install = bool(getattr(prefs, "auto_install_lz4", True))

        self._auto_install_lz4 = auto_install
        self._debug_dumper.configure(
            enabled=dump_enabled,
            max_frames=dump_max_frames,
            dump_dir=dump_dir,
        )

    def _maybe_auto_install_lz4(self) -> None:
        global lz4_frame
        if lz4_frame is not None:
            return
        if not self._auto_install_lz4:
            return
        if self._tried_auto_install_lz4:
            return
        self._tried_auto_install_lz4 = True

        print("[SutuBridge] 检测到 lz4 缺失，尝试自动安装...")
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "lz4",
            "--disable-pip-version-check",
            "--no-input",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except Exception as exc:
            print(f"[SutuBridge] 自动安装 lz4 失败: {exc}")
            return

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            summary = stderr.splitlines()[-1] if stderr else f"exit={result.returncode}"
            print(f"[SutuBridge] 自动安装 lz4 失败: {summary}")
            return

        try:
            import lz4.frame as installed_lz4_frame  # type: ignore

            lz4_frame = installed_lz4_frame
            self._warned_missing_lz4 = False
            print("[SutuBridge] lz4 自动安装成功，已启用 tcp_lz4 压缩")
        except Exception as exc:
            print(f"[SutuBridge] lz4 安装后加载失败: {exc}")


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
