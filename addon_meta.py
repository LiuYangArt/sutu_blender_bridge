from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple


_META_PATH = Path(__file__).with_name("addon_meta.json")


def _load_meta() -> Dict[str, Any]:
    with _META_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("addon_meta.json must be a JSON object")
    return data


def _parse_version(version: str) -> Tuple[int, int, int]:
    parts = str(version).split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid version format: {version!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


_META = _load_meta()

ADDON_ID = str(_META["id"])
ADDON_NAME = str(_META["name"])
ADDON_VERSION_STR = str(_META["version"])
ADDON_VERSION = _parse_version(ADDON_VERSION_STR)
ADDON_TAGLINE = str(_META["tagline"])
