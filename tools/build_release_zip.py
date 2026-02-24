from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT_DIR / "dist"
META_PATH = ROOT_DIR / "addon_meta.json"

EXCLUDE_PREFIXES = (
    ".github/",
    ".vscode/",
    "__pycache__/",
    "tools/",
)

EXCLUDE_FILES = {
    "AGENTS.md",
    "release.bat",
    "sutu_blender_bridge.code-workspace",
}


def load_meta() -> dict[str, str]:
    with META_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return {
        "id": str(data["id"]),
        "version": str(data["version"]),
    }


def list_git_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    files = []
    for raw in result.stdout.splitlines():
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        if path in EXCLUDE_FILES:
            continue
        if any(path.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
            continue
        files.append(path)
    return files


def build_zip() -> Path:
    meta = load_meta()
    archive_name = f"{meta['id']}-{meta['version']}.zip"
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = DIST_DIR / archive_name
    if archive_path.exists():
        archive_path.unlink()

    files = list_git_files()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files:
            zf.write(ROOT_DIR / rel_path, arcname=rel_path)
    return archive_path


def main() -> int:
    archive_path = build_zip()
    print(archive_path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
