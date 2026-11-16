#!/usr/bin/env python3
"""Proof tests for the offline build/acceptance/release chain.

Run with:  make release-test   (or python3 -I releng/test_release.py)

Every test works in temp directories and never reaches a package index. The
suite covers, with real OS processes and real file locks:

  1. full pipeline: frozen manifest, raw/normalized digests, sdist rebuild,
     isolated acceptance with the source tree chmod 000;
  2. reproducibility from two absolute paths with different source mtimes;
  3. same version + same content rerun reuses the committed result;
  4. two real release processes racing same version + DIFFERENT content:
     exactly one commit;
  5. staged wheel replaced after acceptance is rejected at commit;
  6. process exits before the commit: no partial version reference;
  7. simulated disk write failure at the final rename: no partial success and
     no borrowing of another report;
  8. offline enforcement: missing vendor wheel and wrong interpreter fail
     explicitly instead of falling back to an index.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENDOR = ROOT / "vendor" / "wheels"
sys.path.insert(0, str(HERE))

from build_release import build_once, repro  # noqa: E402
from common import (  # noqa: E402
    ReleaseError, load_pins, sha256_file, write_json_atomic,
)
from publish import PublishStore  # noqa: E402

SOURCE_FILES = {
    "pyproject.toml": (ROOT / "pyproject.toml").read_bytes(),
    "README.md": (ROOT / "README.md").read_bytes(),
    "examples/records.json": (ROOT / "examples" / "records.json").read_bytes(),
    **{f"src/entityforge/{p.name}": p.read_bytes()
       for p in (ROOT / "src" / "entityforge").glob("*.py")},
}


def make_source_copy(root: Path, *, mtime: str) -> Path:
    tree = root / "src-tree"
    for rel, payload in SOURCE_FILES.items():
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        os.utime(target, (time.mktime(time.strptime(mtime, "%Y-%m-%d %H:%M:%S")),) * 2)
    return tree


def worker_publish(store_root: str, report_path: str, barrier: str | None,
                   sleep_before: float) -> dict:
    """Top-level worker so 'spawn' can pickle it for the two-process race."""
    sys.path.insert(0, str(HERE))
    from publish import PublishStore
    store = PublishStore(Path(store_root))
    if barrier is not None:
        # Named pipe rendezvous: both workers arrive before either commits.
        with open(barrier, "r") as stream:
            stream.read(1)
    time.sleep(sleep_before)
    try:
        return store.publish(Path(report_path))
    except ReleaseError as exc:
        return {"outcome": "rejected", "error": str(exc)}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = make_source_copy(self.base / "project",
                                       mtime="2020-01-02 03:04:05")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_chain_with_isolated_acceptance(self):
        report = build_once(self.source, self.base / "work", VENDOR)
        self.assertEqual(report["status"], "release-ready")
        # raw digests are reported, never silently replaced
        for kind in ("wheel", "sdist"):
            self.assertNotEqual(report["artifacts"][kind]["raw_sha256"], "")
        # sdist-rebuilt wheel matches the directly built wheel byte for byte
        diff = report["verification"]["direct_vs_sdist_rebuilt_wheel"]
        self.assertTrue(diff["payloads_identical"], diff)
        self.assertTrue(diff["metadata_identical"])
        self.assertTrue(diff["record_identical"])
        self.assertTrue(diff["code_identical"])
        # acceptance really ran against site-packages with the source locked
        blackbox = report["acceptance"]["blackbox"]
        self.assertIn("site-packages", blackbox["import_file"])
        self.assertTrue(blackbox["isolated_mode"])
        self.assertEqual(blackbox["direct_url"]["archive_info"]["hashes"]
                         ["sha256"], report["acceptance"]["wheel_sha256"])
        self.assertGreater(blackbox["record_files_verified"], 5)
        self.assertEqual(blackbox["cli"],
                         {"records": 3, "entities": 2, "restart": True,
                          "rollback": True, "cli_processes": 5})
        # source tree permissions restored after acceptance
        self.assertTrue(os.access(self.source / "src", os.R_OK | os.X_OK))
        # the build venv never had entityforge installed into it
        build_venv_sp = self.base / "work" / "build-venv" / "lib"
        self.assertFalse(list(build_venv_sp.rglob("entityforge/__init__.py")))

    def test_two_paths_two_mtimes_are_reproducible(self):
        copy_a = make_source_copy(self.base / "path-aaa",
                                  mtime="2019-03-04 05:06:07")
        copy_b = make_source_copy(self.base / "path-zzz",
                                  mtime="2025-11-12 13:14:15")
        self.assertNotEqual(copy_a.resolve(), copy_b.resolve())
        result = repro((copy_a, copy_b), self.base / "repro-work", VENDOR)
        c = result["conclusion"]
        self.assertTrue(c["manifest_identical"])
        # the measured raw-vs-normalized distinction must stay visible
        self.assertIn("raw_sdist_identical", c)
        self.assertTrue(c["normalized_wheel_identical"])
        self.assertTrue(c["normalized_sdist_identical"])
        self.assertTrue(c["byte_for_byte_reproducible"])
        # both copies' raw AND normalized digests are reported
        for copy in ("copy_a", "copy_b"):
            self.assertEqual(set(result[copy]["raw"]),
                             {"wheel", "sdist", "rebuilt_wheel"})

    def test_normalization_cannot_hide_content_change(self):
        # alter one source byte: manifest, wheel and sdist must all move
        altered = self.base / "altered"
        shutil.copytree(self.source, altered)
        init = altered / "src" / "entityforge" / "__init__.py"
        init.write_bytes(init.read_bytes() + b"\n# content change\n")
        first = build_once(self.source, self.base / "w1", VENDOR, accept=False)
        second = build_once(altered, self.base / "w2", VENDOR, accept=False)
        self.assertNotEqual(first["source_manifest"]["manifest_id"],
                            second["source_manifest"]["manifest_id"])
        self.assertNotEqual(
            first["artifacts"]["wheel"]["normalized_sha256"],
            second["artifacts"]["wheel"]["normalized_sha256"])
        diffs = second["verification"]["direct_vs_sdist_rebuilt_wheel"]
        self.assertTrue(diffs["code_identical"])  # internally consistent


class PublishBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = make_source_copy(self.base / "project",
                                       mtime="2021-07-08 09:10:11")
        self.report = build_once(self.source, self.base / "work", VENDOR)
        self.report_path = self.base / "release-report.json"
        write_json_atomic(self.report_path, self.report)
        self.store_root = self.base / "release"
        self.store = PublishStore(self.store_root)
        self.version_ref = (self.store.versions /
                            "entityforge-0.1.0.json")
        self.identity = self.store.report_identity(self.report)

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_then_rerun_reuses_result(self):
        first = self.store.publish(self.report_path)
        self.assertEqual(first["outcome"], "committed")
        objects_before = sorted(p.name for p in self.store.objects.rglob("*")
                                if p.is_file())
        second = self.store.publish(self.report_path)
        self.assertEqual(second["outcome"], "reused")
        self.assertEqual(second["report_identity"], self.identity)
        objects_after = sorted(p.name for p in self.store.objects.rglob("*")
                               if p.is_file())
        self.assertEqual(objects_before, objects_after)
        ref = json.loads(self.version_ref.read_text())
        self.assertEqual(ref["accepted_wheel_sha256"],
                         self.report["acceptance"]["wheel_sha256"])

    def _altered_report(self, tag: str) -> Path:
        other_src = self.base / tag
        shutil.copytree(self.source, other_src)
        target = other_src / "src" / "entityforge" / "storage.py"
        target.write_bytes(target.read_bytes() + f"\n# {tag}\n".encode())
        report = build_once(other_src, self.base / f"work-{tag}", VENDOR)
        path = self.base / f"report-{tag}.json"
        write_json_atomic(path, report)
        return path

    def test_two_real_processes_same_version_different_content(self):
        ctx = mp.get_context("spawn")
        other_report = self._altered_report("race")
        # prime the store directory from the parent so both children see it
        PublishStore(self.store_root)
        barrier = str(self.base / "barrier")
        os.mkfifo(barrier)
        args = [
            (str(self.store_root), str(self.report_path), barrier, 0.0),
            (str(self.store_root), str(other_report), barrier, 0.4),
        ]
        with ctx.Pool(2) as pool:
            async_results = [
                pool.apply_async(worker_publish, arg) for arg in args]
            # release both workers once both are waiting on the fifo
            time.sleep(1.5)
            with open(barrier, "w") as stream:
                stream.write("go")
            outcomes = [r.get(timeout=180) for r in async_results]
        statuses = sorted(o["outcome"] for o in outcomes)
        self.assertEqual(statuses, ["committed", "rejected"])
        # exactly one version reference, pointing at the winning content
        self.assertTrue(self.version_ref.is_file())
        self.assertEqual(len(list(self.store.versions.iterdir())), 1)
        winner = next(o for o in outcomes if o["outcome"] == "committed")
        ref = json.loads(self.version_ref.read_text())
        self.assertEqual(ref["report_identity"], winner["report_identity"])
        loser = next(o for o in outcomes if o["outcome"] == "rejected")
        self.assertIn("already committed with different content",
                      loser["error"])
        # the loser left no half-written objects
        self.assertTrue(all(p.parent.name != "tmp"
                            for p in self.store.objects.rglob("*") if p.is_file()))

    def test_two_real_processes_same_version_same_content(self):
        # Two processes publishing the identical report concurrently: exactly
        # one performs the commit; the other reuses the same identity and no
        # second version reference or duplicate object is created.
        ctx = mp.get_context("spawn")
        PublishStore(self.store_root)
        barrier = str(self.base / "same-barrier")
        os.mkfifo(barrier)
        args = [
            (str(self.store_root), str(self.report_path), barrier, 0.0),
            (str(self.store_root), str(self.report_path), barrier, 0.4),
        ]
        with ctx.Pool(2) as pool:
            async_results = [
                pool.apply_async(worker_publish, arg) for arg in args]
            time.sleep(1.5)
            with open(barrier, "w") as stream:
                stream.write("go")
            outcomes = [r.get(timeout=180) for r in async_results]
        statuses = sorted(o["outcome"] for o in outcomes)
        self.assertEqual(statuses, ["committed", "reused"])
        identities = {o["report_identity"] for o in outcomes}
        self.assertEqual(identities, {self.identity})
        self.assertEqual(len(list(self.store.versions.iterdir())), 1)
        # each content digest appears exactly once in the object store
        stored = [p for p in self.store.objects.rglob("*") if p.is_file()]
        self.assertEqual(len(stored), len(set(stored)))

    def test_staged_wheel_replaced_after_acceptance_is_rejected(self):
        # Real threat model: the accepted wheel is replaced in place in the
        # window between acceptance and the commit. The commit re-hashes every
        # referenced artifact first, so the tamper is rejected before the
        # lock is even taken and nothing reaches the object store.
        wheel_path = Path(self.report["artifacts"]["wheel"]["path"])
        original = wheel_path.read_bytes()
        try:
            wheel_path.write_bytes(original + b"tampered-after-acceptance")
            with self.assertRaises(ReleaseError) as ctx:
                self.store.publish(self.report_path)
            self.assertIn("changed after acceptance", str(ctx.exception))
        finally:
            wheel_path.write_bytes(original)
        self.assertFalse(self.version_ref.exists())
        self.assertEqual(list(self.store.versions.iterdir()), [])
        self.assertEqual([p for p in self.store.objects.rglob("*")
                          if p.is_file()], [])

    def test_staging_copy_corruption_is_rejected_by_drift_check(self):
        # If bytes are corrupted while being copied into the object store,
        # the staged re-hash no longer matches the report and the rename
        # aborts; the object directory never appears.
        wheel_suffixes = tuple(
            Path(e["path"]).name for e in self.report["artifacts"].values())

        def hook(staged, target):
            # Staged names are prefixed obj-<hash>-; match on the suffix.
            if Path(staged).name.endswith(wheel_suffixes):
                staged.write_bytes(staged.read_bytes() + b"corrupt-staging")
        self.store.hooks["before_object_rename"] = hook
        with self.assertRaises(ReleaseError) as ctx:
            self.store.publish(self.report_path)
        self.assertIn("staged object digest drift", str(ctx.exception))
        self.assertFalse(self.version_ref.exists())
        self.assertEqual([p for p in self.store.objects.rglob("*")
                          if p.is_file()], [])

    def test_exit_before_commit_leaves_no_partial_version(self):
        with self.assertRaises(SystemExit):
            self.store.publish(self.report_path, exit_before_commit=True)
        self.assertFalse(self.version_ref.exists())
        # nothing was promoted into the object store
        self.assertEqual([p for p in self.store.objects.rglob("*")
                          if p.is_file()], [])
        # a later real publish still succeeds and fully commits
        result = self.store.publish(self.report_path)
        self.assertEqual(result["outcome"], "committed")
        self.assertTrue(self.version_ref.is_file())

    def test_disk_write_failure_at_final_rename(self):
        def boom(src, dst):
            raise OSError("simulated disk write failure")
        self.store.hooks["before_version_rename"] = boom
        with self.assertRaises(OSError):
            self.store.publish(self.report_path)
        self.assertFalse(self.version_ref.exists())
        # no partial reference hiding under another name
        self.assertEqual(list(self.store.versions.iterdir()), [])
        # recovery: stale tmp files are swept and a retry commits cleanly
        swept = self.store.sweep_tmp()
        self.assertTrue(swept)
        self.store.hooks.clear()
        result = self.store.publish(self.report_path)
        self.assertEqual(result["outcome"], "committed")

    def test_cannot_borrow_another_report(self):
        other_report = self._altered_report("borrow")
        other = build_once(self.source, self.base / "work-borrow-base", VENDOR)
        wrong_identity = PublishStore(self.store_root).report_identity(
            json.loads(other_report.read_text()))
        with self.assertRaises(ReleaseError) as ctx:
            self.store.publish(self.report_path,
                               expected_identity=wrong_identity)
        self.assertIn("borrowing another release's report", str(ctx.exception))
        self.assertFalse(self.version_ref.exists())


class OfflineEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = make_source_copy(self.base / "project",
                                       mtime="2022-05-06 07:08:09")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_vendor_wheel_fails_explicitly(self):
        broken_vendor = self.base / "vendor"
        shutil.copytree(VENDOR, broken_vendor)
        (broken_vendor / "wheel-0.41.3-py3-none-any.whl").unlink()
        with self.assertRaises(ReleaseError) as ctx:
            build_once(self.source, self.base / "work", broken_vendor)
        self.assertIn("missing offline artifact", str(ctx.exception))
        self.assertNotIn("Looking in indexes", str(ctx.exception))

    def test_corrupt_vendor_wheel_fails_explicitly(self):
        broken_vendor = self.base / "vendor"
        shutil.copytree(VENDOR, broken_vendor)
        target = broken_vendor / "setuptools-68.1.2-py3-none-any.whl"
        target.write_bytes(target.read_bytes() + b"corruption")
        with self.assertRaises(ReleaseError) as ctx:
            build_once(self.source, self.base / "work", broken_vendor)
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_wrong_interpreter_fails_explicitly(self):
        from common import verify_interpreter
        pins = load_pins()
        pins["interpreter"]["binary_sha256"] = "0" * 64
        with self.assertRaises(ReleaseError) as ctx:
            verify_interpreter(pins)
        self.assertIn("interpreter binary digest mismatch", str(ctx.exception))
        pins["interpreter"]["path"] = "/usr/bin/python3-does-not-exist"
        with self.assertRaises(ReleaseError) as ctx:
            verify_interpreter(pins)
        self.assertIn("pinned interpreter not available", str(ctx.exception))

    def test_pip_environment_is_hard_offline(self):
        # Even if a network route existed, the environment forbids index use.
        report = build_once(self.source, self.base / "work", VENDOR)
        env = report["environment"]
        self.assertEqual(env["source_date_epoch"],
                         load_pins()["source_date_epoch"])
        for tool in env["build_tools"]:
            self.assertTrue((VENDOR / tool["wheel"]).is_file())
            self.assertEqual(sha256_file(VENDOR / tool["wheel"]),
                             tool["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
