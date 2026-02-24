from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict


ROOT_DIR = Path(__file__).resolve().parents[1]
META_PATH = ROOT_DIR / "addon_meta.json"
MANIFEST_PATH = ROOT_DIR / "blender_manifest.toml"


def load_meta() -> Dict[str, str]:
    with META_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("addon_meta.json must be a JSON object")
    required = ("id", "name", "version", "tagline")
    for key in required:
        if key not in data:
            raise ValueError(f"addon_meta.json missing required key: {key}")
    return {
        "id": str(data["id"]),
        "name": str(data["name"]),
        "version": str(data["version"]),
        "tagline": str(data["tagline"]),
    }


def save_meta(meta: Dict[str, str]) -> None:
    with META_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def parse_semver(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if match is None:
        raise ValueError(f"Invalid semantic version: {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_semver(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def bump_semver(version: str, bump_type: str) -> str:
    major, minor, patch = parse_semver(version)
    if bump_type == "major":
        return format_semver((major + 1, 0, 0))
    if bump_type == "minor":
        return format_semver((major, minor + 1, 0))
    if bump_type == "patch":
        return format_semver((major, minor, patch + 1))
    raise ValueError(f"Unsupported bump type: {bump_type}")


def _replace_manifest_string(text: str, key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    pattern = re.compile(rf'(?m)^{re.escape(key)}\s*=\s*".*"$')
    replacement = f'{key} = "{escaped}"'
    next_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ValueError(f"Unable to update key in blender_manifest.toml: {key}")
    return next_text


def sync_manifest(meta: Dict[str, str]) -> None:
    content = MANIFEST_PATH.read_text(encoding="utf-8")
    content = _replace_manifest_string(content, "id", meta["id"])
    content = _replace_manifest_string(content, "version", meta["version"])
    content = _replace_manifest_string(content, "name", meta["name"])
    content = _replace_manifest_string(content, "tagline", meta["tagline"])
    MANIFEST_PATH.write_text(content, encoding="utf-8", newline="\n")


def handle_print(_args: argparse.Namespace) -> int:
    print(load_meta()["version"])
    return 0


def handle_sync(_args: argparse.Namespace) -> int:
    meta = load_meta()
    sync_manifest(meta)
    print(meta["version"])
    return 0


def handle_bump(args: argparse.Namespace) -> int:
    meta = load_meta()
    meta["version"] = bump_semver(meta["version"], args.bump_type)
    save_meta(meta)
    sync_manifest(meta)
    print(meta["version"])
    return 0


def handle_set(args: argparse.Namespace) -> int:
    parse_semver(args.version)
    meta = load_meta()
    meta["version"] = args.version
    save_meta(meta)
    sync_manifest(meta)
    print(meta["version"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release metadata utility.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    print_parser = subparsers.add_parser("print", help="Print current version.")
    print_parser.set_defaults(func=handle_print)

    sync_parser = subparsers.add_parser("sync", help="Sync blender_manifest.toml from addon_meta.json.")
    sync_parser.set_defaults(func=handle_sync)

    bump_parser = subparsers.add_parser("bump", help="Bump semantic version.")
    bump_parser.add_argument("bump_type", choices=("major", "minor", "patch"))
    bump_parser.set_defaults(func=handle_bump)

    set_parser = subparsers.add_parser("set", help="Set semantic version directly.")
    set_parser.add_argument("version")
    set_parser.set_defaults(func=handle_set)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
