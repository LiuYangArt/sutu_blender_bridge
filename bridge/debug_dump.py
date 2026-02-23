from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _safe_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


class BridgeDebugDumper:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled_override: Optional[bool] = None
        self._max_frames_override: Optional[int] = None
        self._dump_dir_override: Optional[str] = None

        self._env_enabled = _parse_bool(os.getenv("SUTU_BRIDGE_DUMP"))
        self._env_max_frames = _safe_int(os.getenv("SUTU_BRIDGE_DUMP_MAX_FRAMES"), 3)
        self._env_dump_dir = os.getenv("SUTU_BRIDGE_DUMP_DIR")

        self._session_dir: Optional[Path] = None
        self._dumped_frame_ids: set[int] = set()
        self._announced = False

    def configure(
        self,
        enabled: Optional[bool] = None,
        max_frames: Optional[int] = None,
        dump_dir: Optional[str] = None,
    ) -> None:
        with self._lock:
            if enabled is not None:
                self._enabled_override = bool(enabled)
            if max_frames is not None:
                self._max_frames_override = max(1, int(max_frames))
            if dump_dir is not None:
                cleaned = str(dump_dir).strip()
                self._dump_dir_override = cleaned if cleaned else None

    def start_stream_session(self) -> None:
        with self._lock:
            self._dumped_frame_ids.clear()
            self._session_dir = None
            self._announced = False

    def dump_frame_bytes(
        self,
        frame_id: int,
        stage: str,
        payload: bytes,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        with self._lock:
            if not self._is_enabled_locked():
                return None
            if not self._reserve_frame_locked(frame_id):
                return None
            session_dir = self._ensure_session_dir_locked()
            if session_dir is None:
                return None

        file_base = f"frame_{int(frame_id):06d}_{stage}"
        binary_path = session_dir / f"{file_base}.bin"
        json_path = session_dir / f"{file_base}.json"

        try:
            binary_path.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            meta_payload: Dict[str, Any] = {
                "frameId": int(frame_id),
                "stage": stage,
                "byteLength": len(payload),
                "sha256": digest,
                "previewHex": payload[:32].hex(),
                "writtenAtMs": int(time.time() * 1000),
            }
            if meta:
                meta_payload.update(meta)
            json_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_log(f"{file_base}.bin bytes={len(payload)} sha256={digest[:16]}...")
            return str(binary_path)
        except Exception as exc:
            self._append_log(f"write failed for frame {frame_id} stage={stage}: {exc}")
            return None

    def _is_enabled_locked(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return bool(self._env_enabled)

    def _max_frames_locked(self) -> int:
        if self._max_frames_override is not None:
            return max(1, self._max_frames_override)
        return max(1, self._env_max_frames)

    def _dump_root_locked(self) -> Path:
        if self._dump_dir_override:
            return Path(self._dump_dir_override)
        if self._env_dump_dir:
            return Path(self._env_dump_dir)
        return Path(tempfile.gettempdir()) / "sutu_bridge_dump"

    def _reserve_frame_locked(self, frame_id: int) -> bool:
        normalized = int(frame_id)
        if normalized in self._dumped_frame_ids:
            return True
        if len(self._dumped_frame_ids) >= self._max_frames_locked():
            return False
        self._dumped_frame_ids.add(normalized)
        return True

    def _ensure_session_dir_locked(self) -> Optional[Path]:
        if self._session_dir is not None:
            return self._session_dir
        root = self._dump_root_locked()
        session_name = time.strftime("session_%Y%m%d_%H%M%S")
        session_dir = root / f"{session_name}_{os.getpid()}"
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        self._session_dir = session_dir
        if not self._announced:
            print(f"[SutuBridge][dump] 已启用，输出目录: {session_dir}")
            self._announced = True
            self._append_log(f"session start: {session_dir}")
        return self._session_dir

    def _append_log(self, message: str) -> None:
        session_dir = self._session_dir
        if session_dir is None:
            return
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
        try:
            with (session_dir / "bridge_dump.log").open("a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            pass


_DUMPER: Optional[BridgeDebugDumper] = None


def get_bridge_debug_dumper() -> BridgeDebugDumper:
    global _DUMPER
    if _DUMPER is None:
        _DUMPER = BridgeDebugDumper()
    return _DUMPER

