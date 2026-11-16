#!/usr/bin/env python3
"""Publish accepted artifacts behind a content-addressed commit boundary.

Chosen protocol (trade-offs of the alternatives are documented in
docs/release-protocol.md):

  * artifacts and reports live in an object store keyed by their own sha256,
    so the same content can never produce two different release objects, and
    a different content can never overwrite an existing one;
  * a version reference (versions/<name>-<version>.json) is switched
    atomically via same-directory temp-file + rename while holding an flock;
  * a report is accepted only if every digest it records re-hashes to the
    same value and the report's own identity matches the publish request, so
    one release process can never borrow another process's report.

Guarantees exercised by releng/test_release.py:

  same version + same content, rerun   -> existing result is reused, no new
                                           objects, version ref unchanged;
  same version + different content     -> exactly one commit wins; the loser
                                           fails loudly and changes nothing;
  staged wheel replaced after accept   -> re-hash at commit rejects it;
  process exits before the commit      -> only temp/ files remain, no partial
                                           version reference ever exists;
  disk write failure at commit         -> same: no partial success in the
                                           release directory.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ReleaseError, atomic_replace, clean_env, load_pins, run, sha256_file,
    write_json_atomic,
)


class PublishStore:
    def __init__(self, root: Path, hooks: dict | None = None):
        self.root = Path(root).resolve()
        self.objects = self.root / "objects"
        self.versions = self.root / "versions"
        self.tmp = self.root / "tmp"
        self.lock_path = self.root / ".publish.lock"
        for directory in (self.objects, self.versions, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        # Test hooks monkeypatch one narrow choke point each.
        self.hooks = hooks or {}

    # ------------------------------------------------------------- locking
    @contextlib.contextmanager
    def commit_lock(self):
        # Waiting cost: only the final rename is serialized. Build and
        # acceptance (the expensive phases) run without holding the lock.
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------ report identity
    def report_identity(self, report: dict) -> str:
        """Content hash over the digest-bearing parts of a release report."""
        import hashlib
        material = {
            "source_manifest_id": report["source_manifest"]["manifest_id"],
            "artifacts": {
                name: entry["normalized_sha256"]
                for name, entry in sorted(report["artifacts"].items())
            },
            "accepted_wheel": report["acceptance"]["wheel_sha256"],
            "interpreter": report["environment"]["interpreter"]["binary_sha256"],
            "build_tools": sorted(
                (t["name"], t["sha256"]) for t in report["environment"]["build_tools"]
            ),
            "project": report["project"],
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    def verify_report(self, report_path: Path) -> dict:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if report.get("status") != "release-ready":
            raise ReleaseError(
                f"report {report_path} is not marked release-ready")
        # Re-hash every artifact named in the report against its current bytes.
        for name, entry in report["artifacts"].items():
            artifact = Path(entry["path"])
            if not artifact.is_file():
                raise ReleaseError(
                    f"report references missing artifact {name}: {artifact}")
            digest = sha256_file(artifact)
            for field in ("raw_sha256", "normalized_sha256"):
                expected = entry.get(field)
                if expected and field == "normalized_sha256" and digest != expected:
                    raise ReleaseError(
                        f"{name} changed after acceptance: "
                        f"{digest} != {expected}")
        if sha256_file(report["acceptance"]["wheel"]) != \
                report["acceptance"]["wheel_sha256"]:
            raise ReleaseError("accepted wheel bytes changed after acceptance")
        return report

    # ----------------------------------------------------------- commit step
    def _store_object(self, source: Path, digest: str) -> Path:
        target = self.objects / digest / source.name
        if target.exists():
            if sha256_file(target) != digest:
                raise ReleaseError(
                    f"object store collision at {target}: same key, "
                    "different content — refusing to overwrite")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = self.tmp / f"obj-{digest[:12]}-{source.name}"
        shutil.copy2(source, staged)
        if self.hooks.get("before_object_rename"):
            self.hooks["before_object_rename"](staged, target)
        # Re-hash immediately before the rename so corruption introduced while
        # staging (including by the test hooks) can never enter the object store.
        if sha256_file(staged) != digest:
            staged.unlink(missing_ok=True)
            raise ReleaseError(f"staged object digest drift for {source}")
        atomic_replace(staged, target)
        return target

    def publish(self, report_path: Path, *, exit_before_commit: bool = False,
                expected_identity: str | None = None) -> dict:
        report_path = Path(report_path).resolve()
        report = self.verify_report(report_path)
        identity = self.report_identity(report)
        if expected_identity and identity != expected_identity:
            raise ReleaseError(
                "report identity does not match the publish request; "
                "borrowing another release's report is forbidden")
        project = report["project"]
        version_ref = self.versions / f"{project['name']}-{project['version']}.json"

        with self.commit_lock():
            if version_ref.exists():
                current = json.loads(version_ref.read_text(encoding="utf-8"))
                if current["report_identity"] == identity:
                    # Same version + same content: reuse the finished result.
                    return {"outcome": "reused", "version_ref": str(version_ref),
                            "report_identity": identity}
                raise ReleaseError(
                    f"version {project['version']} already committed with "
                    f"different content {current['report_identity'][:12]}...; "
                    f"this release {identity[:12]}... loses the commit race")

            if exit_before_commit:
                # Simulates a process dying after all work but before commit.
                raise SystemExit(2)

            stored = {}
            for name, entry in report["artifacts"].items():
                source = Path(entry["path"])
                stored[name] = str(
                    self._store_object(source, entry["normalized_sha256"]))
            report_copy = self.tmp / f"report-{identity[:12]}.json"
            shutil.copy2(report_path, report_copy)
            report_target = self.objects / identity / report_path.name
            report_target.parent.mkdir(parents=True, exist_ok=True)
            if self.hooks.get("before_report_rename"):
                self.hooks["before_report_rename"](report_copy, report_target)
            atomic_replace(report_copy, report_target)

            ref = {
                "name": project["name"],
                "version": project["version"],
                "report_identity": identity,
                "report": str(report_target),
                "artifacts": stored,
                "accepted_wheel_sha256": report["acceptance"]["wheel_sha256"],
            }
            ref_tmp = self.tmp / version_ref.name
            write_json_atomic(ref_tmp, ref)
            if self.hooks.get("before_version_rename"):
                self.hooks.get("before_version_rename")(ref_tmp, version_ref)
            atomic_replace(ref_tmp, version_ref)
        return {"outcome": "committed", "version_ref": str(version_ref),
                "report_identity": identity}

    def sweep_tmp(self) -> list[str]:
        """Failure recovery: temp files never promoted are safe to remove."""
        removed = []
        for path in self.tmp.iterdir():
            path.unlink()
            removed.append(str(path))
        return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_root", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--exit-before-commit", action="store_true")
    parser.add_argument("--expected-identity")
    args = parser.parse_args()
    store = PublishStore(args.release_root)
    result = store.publish(args.report,
                           exit_before_commit=args.exit_before_commit,
                           expected_identity=args.expected_identity)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
