#!/usr/bin/env python3
"""Package the suzuku-monitor-agy Antigravity plugin into a distributable release zip."""

from __future__ import annotations

import json
import stat
import sys
import time
import zipfile
from pathlib import Path

IGNORED_DIR_NAMES = {"__pycache__", "node_modules", ".git"}
IGNORED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
IGNORED_SUFFIXES = (".pyc", ".pyo", ".pyd")


def _should_skip(rel_parts: tuple[str, ...], file_name: str) -> bool:
    if any(part in IGNORED_DIR_NAMES for part in rel_parts[:-1]):
        return True
    if file_name in IGNORED_FILE_NAMES:
        return True
    if file_name.endswith(IGNORED_SUFFIXES):
        return True
    return False


def build_release_zip(repo_root: Path | None = None) -> Path:
    root = (repo_root or Path(__file__).resolve().parent.parent).resolve()
    plugin_dir = root / "plugin"
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"plugin.json not found at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    name = str(manifest.get("name") or "suzuku-monitor").strip()
    version = str(manifest.get("version") or "0.0.0").strip()

    dist_dir = root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    archive_stem = name if name.endswith("-agy") else f"{name}-agy"
    out_zip = dist_dir / f"{archive_stem}-v{version}.zip"

    files = sorted(p for p in plugin_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file_path in files:
            rel = file_path.relative_to(plugin_dir)
            if _should_skip(rel.parts, file_path.name):
                continue

            arcname = (Path(name) / rel).as_posix()
            st = file_path.stat()
            mtime = time.localtime(st.st_mtime)
            date_time = (
                max(1980, mtime.tm_year),
                mtime.tm_mon,
                mtime.tm_mday,
                mtime.tm_hour,
                mtime.tm_min,
                mtime.tm_sec,
            )

            zinfo = zipfile.ZipInfo(filename=arcname, date_time=date_time)
            zinfo.create_system = 3  # UNIX permissions preserved in unzip
            is_exec = (rel.parts[0] == "scripts" and file_path.suffix == ".py") or bool(
                st.st_mode & stat.S_IXUSR
            )
            mode = 0o755 if is_exec else 0o644
            zinfo.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(zinfo, file_path.read_bytes())

    return out_zip


def main() -> int:
    out_zip = build_release_zip()
    print(f"Created {out_zip} ({out_zip.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
