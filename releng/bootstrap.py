#!/usr/bin/env python3
"""Create an offline build/acceptance virtual environment from vendor wheels.

Steps, all local:
  1. verify the pinned interpreter (exact version + binary digest)
  2. verify every vendored wheel against releng/pins.json
  3. python -m venv --without-pip <venv>   (stdlib only; no ensurepip needed)
  4. install pip by executing the pip wheel file directly (zip-app entry)
  5. install the pinned setuptools/wheel into the venv with --no-index

The created venv can never reach a package index: every pip invocation sets
PIP_NO_INDEX and only --find-links paths inside the repository are used.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ReleaseError, clean_env, load_pins, run, sha256_file, verify_interpreter,
    verify_vendor,
)


def _pip_self_install_args(vendor_dir: Path, pip_version: str) -> list[str]:
    return ["install", "--no-index", "--find-links", str(vendor_dir),
            f"pip=={pip_version}"]


def bootstrap(venv_path: Path, vendor_dir: Path) -> dict:
    pins = load_pins()
    python = verify_interpreter(pins)
    wheels = verify_vendor(pins, vendor_dir)
    wheel_by_name = {
        name: next(w for w in wheels if w.name.startswith(name + "-"))
        for name in ("pip", "setuptools", "wheel")
    }

    venv_path = Path(venv_path).resolve()
    # Fresh venv: a reused target could hide a previously installed entityforge.
    import shutil
    if venv_path.exists():
        shutil.rmtree(venv_path)
    run([python, "-m", "venv", "--without-pip", venv_path])
    venv_python = venv_path / "bin" / "python"
    if not venv_python.is_file():
        raise ReleaseError(f"venv creation produced no interpreter at {venv_python}")

    env = clean_env()
    # Under -I the script directory is stripped from sys.path, so a wheel is
    # not importable until it is inserted explicitly; then run pip's in-wheel
    # __main__ as a module. No index is configured anywhere in the environment.
    pip_version = next(t["version"] for t in pins["build_tools"]
                       if t["name"] == "pip")
    runner = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(wheel_by_name['pip'])!r}); "
        f"sys.argv=['pip']+{[str(a) for a in _pip_self_install_args(vendor_dir, pip_version)]!r}; "
        "runpy.run_module('pip', run_name='__main__')"
    )
    run([venv_python, "-I", "-c", runner], env=env)
    run([venv_python, "-I", "-m", "pip", "install",
         "--no-index", "--find-links", vendor_dir,
         "setuptools=={0}".format(next(t["version"] for t in pins["build_tools"]
                                       if t["name"] == "setuptools")),
         "wheel=={0}".format(next(t["version"] for t in pins["build_tools"]
                                  if t["name"] == "wheel"))],
        env=env)

    # Prove the resulting toolchain, still fully offline.
    probe = run([venv_python, "-I", "-c",
                 "import pip, setuptools, wheel, sys; "
                 "print(sys.version.split()[0], pip.__version__, "
                 "setuptools.__version__, wheel.__version__)"], env=env)
    versions = probe.stdout.strip().split()
    expected = [pins["interpreter"]["version_exact"]] + [
        t["version"] for t in pins["build_tools"]]
    if versions != expected:
        raise ReleaseError(
            f"bootstrapped toolchain versions {versions} != pinned {expected}")
    return {
        "venv": str(venv_path),
        "interpreter": str(venv_python),
        "versions": dict(zip(("python", "pip", "setuptools", "wheel"), versions)),
        "vendor_wheels": [
            {"file": w.name, "sha256": sha256_file(w)} for w in wheels
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venv", type=Path, help="venv directory to create")
    parser.add_argument("--vendor", type=Path,
                        default=Path(__file__).resolve().parents[1] / "vendor" / "wheels")
    args = parser.parse_args()
    result = bootstrap(args.venv.resolve(), args.vendor.resolve())
    import json
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
