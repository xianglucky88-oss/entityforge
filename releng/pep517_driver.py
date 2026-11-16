"""Minimal PEP 517 build driver invoked INSIDE the bootstrapped build venv.

The venv already contains the pinned setuptools/wheel, so the backend hook is
called directly: no build isolation subprocess, no index access, no extra
dependency resolution. ``SOURCE_DATE_EPOCH`` is inherited from the clean
environment and clamps all wheel zip entry times.

Generated directories left by earlier attempts are removed first so a stale
egg-info/build tree can never influence a fresh build.

Usage: python -I pep517_driver.py <source-dir> <out-dir> {wheel|sdist|both}
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
from pathlib import Path


def _clean_generated(source_dir: Path) -> None:
    for stale in (source_dir / "build",):
        if stale.is_dir():
            shutil.rmtree(stale)
    for egg_info in source_dir.glob("src/*.egg-info"):
        shutil.rmtree(egg_info)


def main() -> None:
    source_dir = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    what = sys.argv[3]
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean_generated(source_dir)

    # Backend resolution is deliberately the same import a PEP 517 frontend
    # would perform for build-backend = "setuptools.build_meta".
    sys.path.insert(0, str(source_dir))
    from setuptools import build_meta as backend

    result = {}
    # distutils logs build chatter to stdout; keep the driver's stdout as a
    # single JSON line so the orchestrator can parse it deterministically.
    chatter = io.StringIO()
    with contextlib.redirect_stdout(chatter), contextlib.redirect_stderr(chatter):
        if what in ("wheel", "both"):
            result["wheel"] = backend.build_wheel(str(out_dir), {})
        if what in ("sdist", "both"):
            result["sdist"] = backend.build_sdist(str(out_dir), {})
    sys.stderr.write(chatter.getvalue())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
