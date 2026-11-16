#!/usr/bin/env python3
"""Create two content-identical source copies with distinct absolute paths
and distinct file mtimes, then run the reproducibility pipeline on them.

Usage: python -I releng/repro_helper.py <work-dir>
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_release import repro  # noqa: E402
from common import write_json_atomic  # noqa: E402

ROOT = HERE.parent
VENDOR = ROOT / "vendor" / "wheels"

COPY_SPECS = (
    ("copy-alpha", "2019-03-04 05:06:07"),
    ("copy-omega", "2025-11-12 13:14:15"),
)
# Everything that constitutes the shippable source tree (kept in sync with
# releng/freeze_sources.py's inclusion rules).
TRACKED_DIRS = ("src", "examples", "releng", "tests")
TRACKED_FILES = ("pyproject.toml", "README.md", "Makefile")


def _materialize(dst: Path, mtime: str) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in TRACKED_FILES:
        shutil.copy2(ROOT / name, dst / name)
    for name in TRACKED_DIRS:
        shutil.copytree(ROOT / name, dst / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    stamp = time.mktime(time.strptime(mtime, "%Y-%m-%d %H:%M:%S"))
    for path in dst.rglob("*"):
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    return dst


def main() -> None:
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    copies = tuple(
        _materialize(work / dirname, mtime)
        for dirname, mtime in COPY_SPECS)
    report = repro(copies, work / "pipeline", VENDOR)
    report_path = work / "reproducibility-report.json"
    write_json_atomic(report_path, report)
    import json
    print(json.dumps({"report": str(report_path),
                      "copies": [str(c) for c in copies],
                      **report["conclusion"]}, indent=2))


if __name__ == "__main__":
    main()
