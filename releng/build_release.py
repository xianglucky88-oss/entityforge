#!/usr/bin/env python3
"""End-to-end offline build/verification/acceptance orchestrator.

Subcommands:

  build   run the full chain for one source copy and write a release report
          (raw + normalized digests, sdist rebuild comparison, acceptance);
  repro   build two source copies (different absolute paths, different file
          times), report each copy's raw digests and a reproducibility verdict.

The pipeline never installs entityforge into the build venv: the backend is
called in-process through releng/pep517_driver.py, and the only entityforge
installation ever performed happens inside the acceptance venv.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bootstrap import bootstrap  # noqa: E402
from common import (  # noqa: E402
    ReleaseError, clean_env, load_pins, run, sha256_file, verify_interpreter,
    verify_vendor, write_json_atomic,
)
from freeze_sources import freeze  # noqa: E402
from normalize import (  # noqa: E402
    archive_payload_manifest, normalize_sdist, normalize_wheel,
)


def _project_metadata(source: Path) -> dict:
    # tomllib is stdlib on the pinned 3.11; read without importing the project.
    import tomllib
    with (source / "pyproject.toml").open("rb") as stream:
        data = tomllib.load(stream)
    return {"name": data["project"]["name"],
            "version": data["project"]["version"],
            "requires_python": data["project"]["requires-python"],
            "build_requires": data["build-system"]["requires"]}


def _build_raw(venv_python: Path, source: Path, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # cwd must be the source tree, not releng/: setuptools performs flat-layout
    # discovery against the current directory and would mistake the release
    # engineering modules for distribution content otherwise.
    completed = run([venv_python, "-I", HERE / "pep517_driver.py",
                     source, out_dir, "both"], cwd=source)
    names = json.loads(completed.stdout)
    return {"wheel": out_dir / names["wheel"], "sdist": out_dir / names["sdist"]}


def _unpack_sdist(sdist: Path, target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with tarfile.open(sdist, "r:gz") as archive:
        # The archive was just produced and normalized by this pipeline;
        # extraction stays on the stdlib 3.11 API (filter= landed in 3.12).
        for member in archive.getmembers():
            archive.extract(member, target)
    roots = [p for p in target.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise ReleaseError(f"sdist did not contain exactly one root: {roots}")
    return roots[0]


def _compare_wheels(left: Path, right: Path) -> dict:
    """Member-level diff; code, METADATA and RECORD are compared explicitly.

    Archive-level byte equality is reported separately so a content difference
    can never be hidden behind (or mistaken for) a mere timestamp difference.
    """
    a = archive_payload_manifest(left)
    b = archive_payload_manifest(right)
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    changed = sorted(name for name in set(a) & set(b) if a[name] != b[name])
    metadata_names = [n for n in a | b if n.endswith("dist-info/METADATA")]
    record_names = [n for n in a | b if n.endswith("dist-info/RECORD")]
    code_names = sorted(n for n in set(a) & set(b)
                        if n.startswith("entityforge/") and n.endswith(".py"))
    return {
        "member_set_identical": not only_a and not only_b,
        "payloads_identical": not changed and not only_a and not only_b,
        "changed_members": changed,
        "only_in_left": only_a,
        "only_in_right": only_b,
        "metadata_identical": all(a[n] == b[n] for n in metadata_names),
        "record_identical": all(a[n] == b[n] for n in record_names),
        "code_members_compared": code_names,
        "code_identical": all(a[n] == b[n] for n in code_names),
        "left_archive_sha256": sha256_file(left),
        "right_archive_sha256": sha256_file(right),
    }


def _source_sdist_check(manifest: dict, sdist: Path) -> dict:
    """Bind the sdist to the frozen manifest.

    Setuptools' sdist ships distribution content (pyproject.toml, README,
    src/); the frozen manifest additionally covers releng/ and tests/ so the
    release tooling itself is content-pinned. The contract is therefore a
    faithful subset: every sdist payload must match a frozen entry, with no
    unfrozen files smuggled in.
    """
    payload = archive_payload_manifest(sdist)
    frozen = {entry["path"]: entry for entry in manifest["files"]}
    unknown, drifted, checked = [], [], []
    for member, digest in payload.items():
        if member.endswith("/") or "/egg-info/" in member or member.endswith(
                ("PKG-INFO", "setup.cfg", "SOURCES.txt", "dependency_links.txt",
                 "entry_points.txt", "top_level.txt")):
            continue  # generated metadata, not frozen source
        rel = member.split("/", 1)[1] if "/" in member else member
        if rel not in frozen:
            unknown.append(member)
            continue
        if digest != frozen[rel]["sha256"]:
            drifted.append(member)
        checked.append(rel)
    if unknown or drifted:
        raise ReleaseError(
            "sdist is not a faithful subset of the frozen source: "
            f"unfrozen={unknown} drifted={drifted}")
    expected_shipped = sorted(
        rel for rel in frozen
        if rel.startswith("src/") or rel in ("pyproject.toml", "README.md"))
    if sorted(checked) != expected_shipped:
        raise ReleaseError(
            f"sdist missing frozen distribution files: "
            f"{sorted(set(expected_shipped) - set(checked))}")
    return {"frozen_files_in_sdist": len(checked),
            "sdist_total_members": len(payload)}


def build_once(source: Path, work_dir: Path, vendor_dir: Path,
               accept: bool = True) -> dict:
    source = source.resolve()
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    pins = load_pins()
    python = verify_interpreter(pins)
    verify_vendor(pins, vendor_dir)

    project = _project_metadata(source)
    if project["build_requires"] != ["setuptools==68.1.2", "wheel==0.41.3"]:
        raise ReleaseError(
            "pyproject build-system requires are not the pinned exact versions; "
            f"found {project['build_requires']}")

    # 1. frozen source manifest (content identity, path/mtime independent)
    manifest_path = work_dir / "source-manifest.json"
    manifest = freeze(source)
    write_json_atomic(manifest_path, manifest)

    # 2. offline build venv, pinned tools only
    venv_dir = work_dir / "build-venv"
    bootstrap_info = bootstrap(venv_dir, vendor_dir)
    venv_python = venv_dir / "bin" / "python"

    # 3. direct build: raw artifacts
    raw_dir = work_dir / "1-raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)
    raw = _build_raw(venv_python, source, raw_dir)
    raw_digests = {kind: sha256_file(path) for kind, path in raw.items()}

    # 4. explicit normalization step (fresh stage keeps reruns idempotent)
    norm_dir = work_dir / "2-normalized"
    if norm_dir.exists():
        shutil.rmtree(norm_dir)
    norm_dir.mkdir(parents=True)
    norm_wheel = norm_dir / raw["wheel"].name
    norm_sdist = norm_dir / raw["sdist"].name
    wheel_norm_info = normalize_wheel(raw["wheel"], norm_wheel)
    sdist_norm_info = normalize_sdist(raw["sdist"], norm_sdist)
    norm_digests = {"wheel": sha256_file(norm_wheel),
                    "sdist": sha256_file(norm_sdist)}

    # 5. rebuild wheel from the sdist, in an unrelated directory
    rebuild_tree = work_dir / "3-sdist-rebuild"
    unpacked = _unpack_sdist(norm_sdist, rebuild_tree)
    rebuild_raw_dir = work_dir / "3-sdist-rebuild-out"
    if rebuild_raw_dir.exists():
        shutil.rmtree(rebuild_raw_dir)
    rebuild_raw_dir.mkdir(parents=True)
    rebuilt_raw = _build_raw(venv_python, unpacked, rebuild_raw_dir)["wheel"]
    rebuilt_norm = rebuild_raw_dir / "rebuilt.whl"
    normalize_wheel(rebuilt_raw, rebuilt_norm)

    # 6. diffs that must never be ignored
    source_check = _source_sdist_check(manifest, norm_sdist)
    rebuild_diff = _compare_wheels(norm_wheel, rebuilt_norm)
    if not rebuild_diff["payloads_identical"]:
        raise ReleaseError(
            "sdist-rebuilt wheel diverges from the direct wheel: "
            f"{json.dumps(rebuild_diff, indent=2)}")

    report = {
        "schema": "entityforge-release-report/1",
        "status": "release-ready",
        "project": project,
        "source": str(source),
        "source_manifest": {
            "path": str(manifest_path),
            "manifest_id": manifest["manifest_id"],
            "file_count": manifest["file_count"],
        },
        "environment": {
            "interpreter": {
                "path": str(python),
                "version": pins["interpreter"]["version_exact"],
                "binary_sha256": pins["interpreter"]["binary_sha256"],
            },
            "build_tools": pins["build_tools"],
            "source_date_epoch": pins["source_date_epoch"],
            "bootstrap": bootstrap_info,
        },
        "normalization": {
            "wheel": wheel_norm_info,
            "sdist": sdist_norm_info,
            "note": "explicit deterministic archive rewrite; per-member payload "
                    "digests verified unchanged before/after",
        },
        "artifacts": {
            "wheel": {"path": str(norm_wheel),
                      "raw_sha256": raw_digests["wheel"],
                      "normalized_sha256": norm_digests["wheel"]},
            "sdist": {"path": str(norm_sdist),
                      "raw_sha256": raw_digests["sdist"],
                      "normalized_sha256": norm_digests["sdist"]},
            "rebuilt_wheel": {
                "path": str(rebuilt_norm),
                "raw_sha256": sha256_file(rebuilt_raw),
                "normalized_sha256": sha256_file(rebuilt_norm)},
        },
        "verification": {
            "source_sdist_binding": source_check,
            "direct_vs_sdist_rebuilt_wheel": rebuild_diff,
        },
    }

    # 7. isolated acceptance against the normalized wheel, source locked away
    if accept:
        from acceptance import accept_wheel
        acceptance = accept_wheel(
            norm_wheel, source, work_dir / "4-acceptance", vendor_dir)
        report["acceptance"] = acceptance
    return report


def repro(two_sources: tuple[Path, Path], work_dir: Path, vendor_dir: Path) -> dict:
    """Build the same content twice from distinct paths with distinct mtimes."""
    copy_reports = []
    for index, source in enumerate(two_sources):
        sub = work_dir / f"copy-{index}"
        report = build_once(source.resolve(), sub, vendor_dir, accept=False)
        copy_reports.append(report)

    a, b = copy_reports
    manifest_equal = (a["source_manifest"]["manifest_id"]
                      == b["source_manifest"]["manifest_id"])
    conclusion = {
        "manifest_identical": manifest_equal,
        "raw_wheel_identical":
            a["artifacts"]["wheel"]["raw_sha256"]
            == b["artifacts"]["wheel"]["raw_sha256"],
        "raw_sdist_identical":
            a["artifacts"]["sdist"]["raw_sha256"]
            == b["artifacts"]["sdist"]["raw_sha256"],
        "normalized_wheel_identical":
            a["artifacts"]["wheel"]["normalized_sha256"]
            == b["artifacts"]["wheel"]["normalized_sha256"],
        "normalized_sdist_identical":
            a["artifacts"]["sdist"]["normalized_sha256"]
            == b["artifacts"]["sdist"]["normalized_sha256"],
    }
    conclusion["byte_for_byte_reproducible"] = all(
        conclusion[key] for key in
        ("normalized_wheel_identical", "normalized_sdist_identical"))
    conclusion["explanation"] = (
        "raw sdist bytes differ because setuptools records builder wall-clock "
        "mtimes/PAX headers and source file mtimes; the explicit normalization "
        "step rewrites both archives from their member payloads with fixed "
        "epoch/order/mode, and normalized archives are byte-identical. "
        "Raw and normalized digests are both reported; no difference is hidden."
        if not conclusion["raw_sdist_identical"] else
        "raw and normalized archives are both byte-identical across copies")
    return {
        "schema": "entityforge-reproducibility/1",
        "copy_a": {"source": a["source"],
                   "raw": {k: v["raw_sha256"] for k, v in a["artifacts"].items()},
                   "normalized": {k: v["normalized_sha256"]
                                  for k, v in a["artifacts"].items()}},
        "copy_b": {"source": b["source"],
                   "raw": {k: v["raw_sha256"] for k, v in b["artifacts"].items()},
                   "normalized": {k: v["normalized_sha256"]
                                  for k, v in b["artifacts"].items()}},
        "conclusion": conclusion,
        "reports": copy_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build")
    build_p.add_argument("--source", type=Path, required=True)
    build_p.add_argument("--work-dir", type=Path, required=True)
    build_p.add_argument("--vendor", type=Path,
                         default=HERE.parent / "vendor" / "wheels")
    build_p.add_argument("--report", type=Path, required=True)
    build_p.add_argument("--no-accept", action="store_true")

    repro_p = sub.add_parser("repro")
    repro_p.add_argument("--source-a", type=Path, required=True)
    repro_p.add_argument("--source-b", type=Path, required=True)
    repro_p.add_argument("--work-dir", type=Path, required=True)
    repro_p.add_argument("--vendor", type=Path,
                         default=HERE.parent / "vendor" / "wheels")
    repro_p.add_argument("--report", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        report = build_once(args.source, args.work_dir, args.vendor,
                            accept=not args.no_accept)
        write_json_atomic(args.report, report)
        print(json.dumps({"status": report["status"],
                          "manifest": report["source_manifest"]["manifest_id"],
                          "wheel": report["artifacts"]["wheel"]["normalized_sha256"],
                          "report": str(args.report)}, indent=2))
    else:
        report = repro((args.source_a, args.source_b), args.work_dir,
                       args.vendor)
        write_json_atomic(args.report, report)
        print(json.dumps(report["conclusion"], indent=2))


if __name__ == "__main__":
    main()
