#!/usr/bin/env python3
"""Freeze the source manifest that every downstream artifact is bound to.

The manifest deliberately records *content* identity only — relative POSIX
path, bytes, sha256, normalized file mode — never mtime or absolute path. Two
copies of the same tree at different absolute paths with different file times
therefore produce the same manifest, which is exactly what reproducibility
claims rest on.

Inclusion rule: everything setuptools would ship from the source tree
(pyproject.toml, README.md and all files under src/), plus the release
engineering scripts and tests themselves. Generated directories (build/,
dist/, *.egg-info, __pycache__, .git, vendor wheels, release work dirs) are
excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ReleaseError, sha256_file, write_json_atomic  # noqa: E402

EXCLUDE_DIRS = {
    ".git", "__pycache__", "build", "dist", ".venv", "venv",
    "release-work", "release",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store"}
# Directories whose generated egg-info must never be treated as frozen input.
EGG_INFO_SUFFIX = ".egg-info"


def included_files(root: Path) -> list[Path]:
    selected: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDE_DIRS or part.endswith(EGG_INFO_SUFFIX)
               for part in rel_parts):
            continue
        if path.is_dir() or not path.is_file():
            continue
        if path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
            continue
        selected.append(path)
    return selected


def freeze(root: Path) -> dict:
    root = Path(root).resolve()
    entries = []
    for path in included_files(root):
        rel = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode)
        # Normalize the executable bit away except where genuinely set.
        mode = 0o755 if mode & 0o111 else 0o644
        entries.append({
            "path": rel,
            "size": path.stat().st_size,
            "mode": f"{mode:04o}",
            "sha256": sha256_file(path),
        })
    if not entries:
        raise ReleaseError(f"no source files found under {root}")
    canonical = "".join(
        f"{e['sha256']}  {e['mode']}  {e['path']}\n" for e in entries
    ).encode("utf-8")
    manifest_id = hashlib.sha256(canonical).hexdigest()
    return {
        "schema": "entityforge-source-manifest/1",
        "root": None,  # absolute path intentionally not recorded
        "file_count": len(entries),
        "manifest_id": manifest_id,
        "files": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="source tree root")
    parser.add_argument("--output", type=Path, required=True,
                        help="manifest JSON path (atomic write)")
    args = parser.parse_args()
    manifest = freeze(args.source.resolve())
    write_json_atomic(args.output.resolve(), manifest)
    print(json.dumps({
        "manifest": str(args.output),
        "manifest_id": manifest["manifest_id"],
        "file_count": manifest["file_count"],
    }, indent=2))


if __name__ == "__main__":
    main()
