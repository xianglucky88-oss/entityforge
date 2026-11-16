"""Shared helpers for the offline build/acceptance/release chain.

Everything here is standard-library only and safe to run under ``python -I``:
no imports of third-party code, no relative imports, no writes outside the
paths explicitly passed by callers.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PINS_PATH = Path(__file__).resolve().parent / "pins.json"


class ReleaseError(RuntimeError):
    """Any condition that must fail the release loudly rather than limp on."""


def load_pins() -> dict:
    with PINS_PATH.open(encoding="utf-8") as stream:
        return json.load(stream)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON via a same-directory temp file + fsync + rename.

    Callers that inject disk-write failures do so by making the rename or the
    temp-file write raise; in both cases the destination path never appears.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True,
                      ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def read_json(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def clean_env() -> dict:
    """Minimal environment: no PYTHONPATH, no proxy, no inherited index config.

    A clean env is what makes ``PIP_NO_INDEX=1`` airtight: even a planted
    pip.conf cannot be picked up via PIP_CONFIG_FILE, and no proxy can turn a
    blocked index call into a silent success.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "SOURCE_DATE_EPOCH": str(load_pins()["source_date_epoch"]),
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        # Debian's patched distutils trips setuptools' _distutils_hack assert
        # unless stdlib distutils is selected explicitly.
        "SETUPTOOLS_USE_DISTUTILS": "stdlib",
    }
    return env


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else clean_env(),
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise ReleaseError(
            f"command failed ({completed.returncode}): {' '.join(map(str, cmd))}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    return completed


def verify_interpreter(pins: dict | None = None) -> Path:
    """Fail loudly when the pinned interpreter is missing, moved or altered."""
    pins = pins or load_pins()
    spec = pins["interpreter"]
    python = Path(spec["path"])
    if not python.is_file():
        raise ReleaseError(
            f"pinned interpreter not available: {python} "
            f"(need CPython {spec['version_exact']}, {spec['platform_machine']})"
        )
    completed = subprocess.run(
        [str(python), "-I", "-c",
         "import sys, platform; print(sys.version.split()[0]); print(platform.machine())"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise ReleaseError(f"pinned interpreter cannot execute: {completed.stderr}")
    version, machine = completed.stdout.split()
    if version != spec["version_exact"]:
        raise ReleaseError(
            f"interpreter version mismatch: found {version}, "
            f"pinned {spec['version_exact']}; re-pin before rebuilding"
        )
    if machine != spec["platform_machine"]:
        raise ReleaseError(
            f"platform mismatch: found {machine}, pinned {spec['platform_machine']}"
        )
    actual_hash = sha256_file(python)
    if actual_hash != spec["binary_sha256"]:
        raise ReleaseError(
            f"interpreter binary digest mismatch for {python}:\n"
            f"  found {actual_hash}\n  pinned {spec['binary_sha256']}\n"
            f"refusing to build with an unverified interpreter"
        )
    return python


def verify_vendor(pins: dict | None = None, vendor_dir: Path | None = None) -> list[Path]:
    """Verify every pinned build-tool wheel is present locally with exact hash.

    No index is ever consulted here: a missing or altered wheel is a hard
    failure, which is the offline contract.
    """
    pins = pins or load_pins()
    vendor_dir = Path(vendor_dir) if vendor_dir else (
        Path(__file__).resolve().parents[1] / "vendor" / "wheels")
    found: list[Path] = []
    problems: list[str] = []
    for tool in pins["build_tools"]:
        wheel_path = vendor_dir / tool["wheel"]
        if not wheel_path.is_file():
            problems.append(f"missing offline artifact: {wheel_path}")
            continue
        actual = sha256_file(wheel_path)
        if actual != tool["sha256"]:
            problems.append(
                f"hash mismatch for {tool['wheel']}:\n"
                f"  found {actual}\n  pinned {tool['sha256']}")
        found.append(wheel_path)
    if problems:
        raise ReleaseError(
            "offline dependency preparation invalid; run vendor/fetch-vendor.sh "
            "on a connected preparation host, never let the build reach an index:\n"
            + "\n".join(f"  - {p}" for p in problems))
    return found


def fresh_dir(path: Path) -> Path:
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def atomic_replace(src: Path, dst: Path) -> None:
    """os.replace wrapper so disk-failure injection has one choke point."""
    os.replace(Path(src), Path(dst))


@contextlib.contextmanager
def chmod_guarded(path: Path):
    """Make a source tree unreadable AND un-traversable for acceptance.

    Only directories that belong to the frozen source are locked; generated
    build trees (build/, *.egg-info, venvs) are skipped so their ownership or
    mode can never abort the guard. Permissions are restored in reverse order
    even when the acceptance body raises.
    """
    path = Path(path)
    skip_names = {"build", "dist", "__pycache__", "build-venv",
                  "acceptance-venv"}
    targets = [path]
    for current, dirnames, _ in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d not in skip_names and not d.endswith(".egg-info")]
        targets.extend(Path(current) / d for d in dirnames)
    # Lock deepest directories first: once a parent is 000 its children
    # become unreachable, so a top-down order would make the later chmods
    # fail with EACCES. Restore top-down for the symmetric reason.
    original = [(p, p.stat().st_mode) for p in targets]
    lock_order = list(reversed(original))
    locked: list[Path] = []
    try:
        try:
            for p, _ in lock_order:
                os.chmod(p, 0o000)
                locked.append(p)
        except PermissionError:
            for p, mode in original:  # top-down rollback
                with contextlib.suppress(OSError):
                    os.chmod(p, mode)
            raise
        yield
    finally:
        for p, mode in original:  # top-down restore keeps paths reachable
            with contextlib.suppress(FileNotFoundError, PermissionError):
                os.chmod(p, mode)


def under_isolated_python(code: str) -> int:
    """Run code with -I in *this* interpreter for in-process checks."""
    return subprocess.run([sys.executable, "-I", "-c", code],
                          env=clean_env(), capture_output=True, text=True)
