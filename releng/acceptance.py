#!/usr/bin/env python3
"""Isolated installation acceptance.

The acceptance object is the *published-bytes* wheel, never the source tree:

  1. build a fresh --without-pip venv with the pinned interpreter;
  2. bootstrap pip/setuptools only from vendor wheels, PIP_NO_INDEX=1;
  3. install the wheel by explicit file path so pip writes PEP 610
     direct_url.json binding the installed distribution to that exact wheel
     and its sha256;
  4. make the source tree unreadable AND un-traversable (chmod 000 on every
     directory), strip PYTHONPATH/PYTHONHOME, run from an unrelated cwd under
     python -I, and exercise the installed package purely as a black box;
  5. verify import provenance, direct_url binding, INSTALLER and RECORD.

Nothing here imports entityforge from the interpreter running this script:
every entityforge call happens in the acceptance venv subprocess.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap import bootstrap  # noqa: E402
from common import (  # noqa: E402
    ReleaseError, chmod_guarded, clean_env, load_pins, run, sha256_file,
    write_json_atomic,
)

BLACKBOX_SCRIPT = r"""
import csv, json, os, subprocess, sys, tempfile
from pathlib import Path

venv_bin = Path(sys.executable).parent
entry = venv_bin / "entityforge"
report = {}

# --- provenance: where did the installed package come from? -----------------
import importlib.metadata as md
dist = md.distribution("entityforge")
dist_info = Path(dist._path)
pkg_file = Path(dist.locate_file("entityforge/__init__.py")).resolve()
report["import_file"] = str(pkg_file)
if "site-packages" not in str(pkg_file):
    raise SystemExit(f"entityforge imported from outside site-packages: {pkg_file}")
if any(p in sys.path for p in ("", os.getcwd())):
    # -I removes cwd from sys.path; assert the invariant explicitly.
    raise SystemExit(f"cwd leaked into sys.path: {sys.path}")
report["sys_path"] = list(sys.path)
report["isolated_mode"] = sys.flags.isolated == 1
if not report["isolated_mode"]:
    raise SystemExit("acceptance must run under python -I")

installer = (dist_info / "INSTALLER").read_text(encoding="utf-8").strip()
if installer != "pip":
    raise SystemExit(f"unexpected INSTALLER {installer!r}")
report["installer"] = installer

direct = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
expected_wheel_url = os.environ["EXPECTED_WHEEL_URL"]
expected_wheel_sha = os.environ["EXPECTED_WHEEL_SHA"]
if direct["url"] != expected_wheel_url:
    raise SystemExit(
        f"direct_url does not bind the accepted wheel:\n"
        f"  {direct['url']} != {expected_wheel_url}")
bound_hash = direct["archive_info"]["hashes"]["sha256"]
if bound_hash != expected_wheel_sha:
    raise SystemExit(f"direct_url hash {bound_hash} != {expected_wheel_sha}")
report["direct_url"] = direct

# --- RECORD: every installed file must match the wheel's RECORD -------------
import base64, hashlib
site_packages = dist_info.parent
record_rows = list(csv.reader((dist_info / "RECORD").read_text().splitlines()))
checked = 0
for row in record_rows:
    if not row or not row[1]:
        continue
    name, hash_field, size_field = row
    target = (site_packages / name).resolve()
    if not target.is_file():
        raise SystemExit(f"RECORD member not installed: {name}")
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(target.read_bytes()).digest()).rstrip(b"=").decode()
    if digest != hash_field.split("=", 1)[1]:
        raise SystemExit(f"installed file diverges from RECORD: {name}")
    checked += 1
report["record_files_verified"] = checked

# --- black-box behavior through the installed console script ----------------
env = dict(os.environ)
assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
with tempfile.TemporaryDirectory() as work:
    work = Path(work)
    records = work / "records.json"
    records.write_text(json.dumps([
        {"source": "crm", "id": "1", "fields": {
            "name": "Alice", "email": "a@x.test", "city": "Shanghai", "phone": None}},
        {"source": "web", "id": "2", "fields": {
            "name": "Alice", "email": "a@x.test", "city": "SH", "phone": None}},
        {"source": "web", "id": "3", "fields": {
            "name": "Bob", "email": "b@x.test", "city": "Beijing", "phone": None}},
    ]), encoding="utf-8")
    db = work / "master.sqlite"

    def cli(*args):
        completed = subprocess.run(
            [str(entry), "--db", str(db), *map(str, args)],
            env=env, capture_output=True, text=True, cwd=str(work))
        if completed.returncode != 0:
            raise SystemExit(f"CLI failed: {completed.stderr}")
        return json.loads(completed.stdout)

    imported = cli("ingest", records)
    resolved = cli("resolve")
    if len(resolved["entities"]) != 2 or len(resolved["records"]) != 3:
        raise SystemExit(f"unexpected resolution: {resolved}")
    if cli("show") != resolved:
        raise SystemExit("state did not survive a fresh CLI process")
    rolled_back = cli("rollback", imported["revision"])
    if rolled_back["entities"]:
        raise SystemExit("rollback did not restore the pre-resolution revision")
    if len(cli("history")) != 4:
        raise SystemExit("audit history incomplete")
    report["cli"] = {"records": 3, "entities": 2, "restart": True,
                     "rollback": True, "cli_processes": 5}

# --- black-box behavior through the documented Python API -------------------
from entityforge import EntityService
from entityforge.matching import Normalizer, PrioritySurvivor
from entityforge.storage import RevisionConflict  # noqa: F401
with tempfile.TemporaryDirectory() as work:
    svc = EntityService(str(Path(work) / "api.sqlite"),
                        normalizer=Normalizer({"sh": "Shanghai"}),
                        survivor=PrioritySurvivor({"crm": 0, "web": 1}))
    try:
        revision = svc.ingest([
            {"source": "crm", "id": "1", "fields": {
                "name": "Alice", "city": "SH"}},
            {"source": "web", "id": "2", "fields": {
                "name": "Alice", "city": "Shanghai"}},
        ])
        result = svc.resolve(expected=revision)
        if len(result["entities"]) != 1:
            raise SystemExit("API resolution did not merge the pair")
    finally:
        svc.close()
report["api"] = {"entities": 1}
print("ACCEPTANCE_JSON=" + json.dumps(report, sort_keys=True))
"""


def accept_wheel(wheel: Path, source_root: Path, work_dir: Path,
                 vendor_dir: Path) -> dict:
    pins = load_pins()
    wheel = Path(wheel).resolve()
    source_root = Path(source_root).resolve()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    wheel_sha = sha256_file(wheel)

    # The acceptance sandbox (venv + black-box script + cwd) must never live
    # inside the guarded source tree: the guard makes the tree un-traversable,
    # which would prevent the subprocess interpreter from even starting.
    try:
        work_dir.relative_to(source_root)
    except ValueError:
        sandbox = work_dir
        sandbox_owned = False
    else:
        sandbox = Path(tempfile.mkdtemp(prefix="entityforge-accept-"))
        sandbox_owned = True
    venv_dir = sandbox / "acceptance-venv"
    try:
        return _accept_in_sandbox(
            wheel, wheel_sha, source_root, vendor_dir, sandbox, venv_dir, pins)
    finally:
        if sandbox_owned:
            shutil.rmtree(sandbox, ignore_errors=True)


def _accept_in_sandbox(wheel, wheel_sha, source_root, vendor_dir, sandbox,
                       venv_dir, pins) -> dict:
    bootstrap_info = bootstrap(venv_dir, Path(vendor_dir).resolve())
    venv_python = venv_dir / "bin" / "python"

    # Explicit wheel path => PEP 610 direct_url.json with the exact wheel hash.
    env = clean_env()
    run([venv_python, "-I", "-m", "pip", "install",
         "--no-index", "--find-links", Path(vendor_dir).resolve(),
         "--no-deps", wheel], env=env)

    # Belt-and-braces: prove no pre-existing entityforge could have been reused.
    pre_check = run([venv_python, "-I", "-m", "pip", "show", "entityforge"],
                    env=env)
    if "Location:" not in pre_check.stdout:
        raise ReleaseError("entityforge missing after isolated install")

    script = sandbox / "blackbox.py"
    script.write_text(BLACKBOX_SCRIPT, encoding="utf-8")
    # The cwd must also live outside the guarded source tree.
    run_cwd = Path(tempfile.mkdtemp(prefix="entityforge-accept-cwd-",
                                    dir=sandbox))
    try:
        # The source tree is made inaccessible for the whole black-box run. A
        # planted sys.path entry or any relative import attempt would then
        # fail, proving the accepted bytes come solely from site-packages.
        with chmod_guarded(source_root):
            if os.access(source_root, os.R_OK | os.X_OK):
                raise ReleaseError("source guard failed: tree still accessible")
            completed = subprocess.run(
                [str(venv_python), "-I", str(script)],
                cwd=str(run_cwd),
                env={**env,
                     "EXPECTED_WHEEL_URL": wheel.as_uri(),
                     "EXPECTED_WHEEL_SHA": wheel_sha},
                capture_output=True, text=True)
    finally:
        shutil.rmtree(run_cwd, ignore_errors=True)
    if completed.returncode != 0:
        raise ReleaseError(
            "isolated acceptance failed with source tree inaccessible:\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}")

    marker = "ACCEPTANCE_JSON="
    lines = [line for line in completed.stdout.splitlines()
             if line.startswith(marker)]
    if len(lines) != 1:
        raise ReleaseError(f"acceptance marker missing in:\n{completed.stdout}")
    blackbox = json.loads(lines[0][len(marker):])
    if blackbox["direct_url"]["archive_info"]["hashes"]["sha256"] != wheel_sha:
        raise ReleaseError("accepted wheel hash diverged from binding")
    return {
        "wheel": str(wheel),
        "wheel_sha256": wheel_sha,
        "source_root_guarded": str(source_root),
        "venv": str(venv_dir),
        "bootstrap": bootstrap_info,
        "blackbox": blackbox,
        "status": "accepted",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--vendor", type=Path,
                        default=Path(__file__).resolve().parents[1] / "vendor" / "wheels")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = accept_wheel(args.wheel, args.source_root, args.work_dir,
                          args.vendor)
    write_json_atomic(args.report, report)
    print(json.dumps({"status": "accepted",
                      "wheel_sha256": report["wheel_sha256"],
                      "report": str(args.report)}, indent=2))


if __name__ == "__main__":
    main()
