import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from entityforge import BatchConflict, EntityService, RevisionConflict
from entityforge.matching import EvidenceMatcher, Normalizer, PrioritySurvivor

PROJECT = Path(__file__).resolve().parents[1]


def upsert(source, key, name="Alice", email=None, city="Shanghai", phone=None):
    return {"op": "upsert", "source": source, "id": key, "fields": {
        "name": name, "email": email, "city": city, "phone": phone}}


def delete(source, key):
    return {"op": "delete", "source": source, "id": key}


def record_key(source, external):
    return json.dumps([source, external], ensure_ascii=False, separators=(",", ":"))


def apply_ops(records, operations):
    """Mirror of batch semantics on a plain {key: row} dict, for references."""
    for raw in operations:
        key = record_key(raw["source"], raw["id"])
        if raw["op"] == "delete":
            records.pop(key, None)
        else:
            records[key] = {"source": raw["source"], "id": raw["id"], "fields": raw["fields"]}


def full_reference(records, **kwargs):
    """Independent full-resolution reference: fresh database, plain ingest + resolve."""
    tmp = tempfile.TemporaryDirectory()
    service = EntityService(Path(tmp.name) / "reference.sqlite", **kwargs)
    try:
        if records:
            service.ingest([{"source": r["source"], "id": r["id"], "fields": r["fields"]}
                            for r in records.values()])
        return service.resolve()
    finally:
        service.close()
        tmp.cleanup()


class RecordingMatcher:
    """Records every compared pair (identified by email or name) for locality proofs."""

    def __init__(self, inner=None):
        self.inner = inner or EvidenceMatcher()
        self.calls = []

    def compare(self, left, right):
        self.calls.append((left.get("email") or left.get("name"),
                           right.get("email") or right.get("name")))
        return self.inner.compare(left, right)


def run_script(source, *args):
    """Run a python snippet as a real subprocess against the project sources."""
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "script.py"
        script.write_text(source, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              env=env, capture_output=True, text=True)


def run_cli(db, *args):
    env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
    return subprocess.run([sys.executable, "-m", "entityforge.cli", "--db", str(db),
                           *map(str, args)], env=env, capture_output=True, text=True)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "master.sqlite"
        self.service = EntityService(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def assert_same_resolution(self, snapshot, records, **kwargs):
        reference = full_reference(records, **kwargs)
        self.assertEqual(snapshot["entities"], reference["entities"])
        self.assertEqual(snapshot["evidence"], reference["evidence"])

    # --- idempotent batch receipts -------------------------------------

    def test_replay_returns_original_revision_without_writes(self):
        ops = [upsert("crm", "1", email="a@x.test"), upsert("crm", "2", email="a@x.test")]
        first = self.service.apply_batch("b1", ops, expected=0)
        self.assertEqual(first["status"], "committed")
        self.assertEqual(first["revision"], 1)
        history_len = len(self.service.store.history())
        replay = self.service.apply_batch("b1", ops, expected=0)
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual({k: v for k, v in replay.items() if k != "status"},
                         {k: v for k, v in first.items() if k != "status"})
        # No new audit revision, HEAD untouched.
        self.assertEqual(self.service.snapshot()["revision"], 1)
        self.assertEqual(len(self.service.store.history()), history_len)

    def test_completed_batch_is_recognized_before_stale_expected(self):
        self.service.apply_batch("b1", [upsert("crm", "1")], expected=0)
        self.service.apply_batch("b2", [upsert("crm", "2")], expected=1)
        # The retry of b1 carries its original, now stale expected revision.
        replay = self.service.apply_batch("b1", [upsert("crm", "1")], expected=0)
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(replay["revision"], 1)
        # A genuinely new batch with a stale expected still conflicts.
        with self.assertRaises(RevisionConflict):
            self.service.apply_batch("b3", [upsert("crm", "3")], expected=0)

    def test_same_batch_id_with_different_content_is_rejected(self):
        self.service.apply_batch("b1", [upsert("crm", "1")], expected=0)
        with self.assertRaises(BatchConflict):
            self.service.apply_batch("b1", [upsert("crm", "2")])
        with self.assertRaises(BatchConflict):
            self.service.apply_batch("b1", [upsert("crm", "1"), delete("crm", "9")])
        with self.assertRaises(BatchConflict):
            self.service.apply_batch("b1", [delete("crm", "1")])
        self.assertEqual(self.service.snapshot()["revision"], 1)

    def test_invalid_operation_aborts_batch_and_keeps_id_reusable(self):
        with self.assertRaises(ValueError):
            self.service.apply_batch("b1", [upsert("crm", "1"), {"op": "upsert", "source": "crm"}])
        with self.assertRaises(ValueError):
            self.service.apply_batch("b2", [{"op": "merge", "source": "crm", "id": "1"}])
        with self.assertRaises(ValueError):
            self.service.apply_batch(
                "b3", [{"op": "upsert", "source": "crm", "id": "1", "fields": {"n": 5}}])
        self.assertEqual(self.service.snapshot()["revision"], 0)
        self.assertIsNone(self.service.store.get_batch("b1"))
        receipt = self.service.apply_batch("b1", [upsert("crm", "1")], expected=0)
        self.assertEqual(receipt["status"], "committed")

    def test_two_instances_competing_with_same_expected(self):
        other = EntityService(self.path)
        self.addCleanup(other.close)
        self.service.apply_batch("b1", [upsert("crm", "1")], expected=0)
        with self.assertRaises(RevisionConflict):
            other.apply_batch("b2", [upsert("crm", "2")], expected=0)
        # The losing instance retrying its own committed batch gets the replay.
        replay = other.apply_batch("b1", [upsert("crm", "1")], expected=0)
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(replay["revision"], 1)

    # --- incremental equivalence with the independent full path ---------

    def test_incremental_batches_match_independent_full_resolution(self):
        kwargs = {"normalizer": Normalizer({"sh": "Shanghai"}),
                  "survivor": PrioritySurvivor({"crm": 0, "web": 1})}
        service = EntityService(Path(self.tmp.name) / "main.sqlite", **kwargs)
        self.addCleanup(service.close)
        records = {}
        batches = [
            [upsert("crm", "1", "Alice Zhang", email="a@x.test", city="SH"),
             upsert("web", "2", "Alice Zhan", city="Shanghai"),
             upsert("crm", "3", "Bob", phone="111", city="Beijing")],
            [upsert("web", "4", "Bob", phone="111", city="Beijing"),
             upsert("crm", "5", "Carol", email="c@x.test", city="Shanghai")],
            [upsert("crm", "3", "Bob", phone="222", city="Beijing")],          # contact change
            [delete("web", "2")],                                              # deletion
            [upsert("crm", "5", "Carol", email="a@x.test", city="Shanghai")],  # joins Alice cluster
            [upsert("web", "6", "Alice Zhang", city="SH")],                    # alias fuzzy candidate
            [delete("crm", "3")],                                              # delete a singleton
        ]
        for i, ops in enumerate(batches):
            receipt = service.apply_batch(f"b{i}", ops, expected=service.snapshot()["revision"])
            self.assertFalse(receipt["fallback"], f"batch {i} unexpectedly fell back")
            apply_ops(records, ops)
            self.assert_same_resolution(service.snapshot(), records, **kwargs)

    def test_delete_splits_component(self):
        self.service.apply_batch("b1", [
            upsert("crm", "1", "Ann", email="a@x.test", city=""),
            upsert("crm", "2", "Bob", email="a@x.test", phone="9", city=""),
            upsert("crm", "3", "Cid", phone="9", city="")])
        self.assertEqual(len(self.service.snapshot()["entities"]), 1)
        receipt = self.service.apply_batch("b2", [delete("crm", "2")])
        self.assertFalse(receipt["fallback"])
        snapshot = self.service.snapshot()
        self.assertEqual(len(snapshot["entities"]), 2)
        records = {}
        apply_ops(records, [upsert("crm", "1", "Ann", email="a@x.test", city=""),
                            upsert("crm", "3", "Cid", phone="9", city="")])
        self.assert_same_resolution(snapshot, records)

    def test_contact_change_moves_record_between_clusters(self):
        self.service.apply_batch("b1", [
            upsert("crm", "1", "Ann", email="x@1.test", city=""),
            upsert("crm", "2", "Amy", email="x@1.test", city=""),
            upsert("crm", "3", "Bob", email="z@1.test", city=""),
            upsert("crm", "4", "Ben", email="z@1.test", city="")])
        receipt = self.service.apply_batch(
            "b2", [upsert("crm", "2", "Amy", email="z@1.test", city="")])
        self.assertFalse(receipt["fallback"])
        entities = self.service.snapshot()["entities"]
        self.assertEqual(sorted(len(e["members"]) for e in entities.values()), [1, 3])
        records = {}
        apply_ops(records, [upsert("crm", "1", "Ann", email="x@1.test", city=""),
                            upsert("crm", "2", "Amy", email="z@1.test", city=""),
                            upsert("crm", "3", "Bob", email="z@1.test", city=""),
                            upsert("crm", "4", "Ben", email="z@1.test", city="")])
        self.assert_same_resolution(self.service.snapshot(), records)

    def test_alias_driven_block_change_splits_and_rejoins(self):
        kwargs = {"normalizer": Normalizer({"sh": "Shanghai"})}
        service = EntityService(Path(self.tmp.name) / "alias.sqlite", **kwargs)
        self.addCleanup(service.close)
        service.apply_batch("b1", [upsert("crm", "1", "Alice Zhang", city="SH"),
                                   upsert("crm", "2", "Alice Zhan", city="Shanghai")])
        self.assertEqual(len(service.snapshot()["entities"]), 1)
        service.apply_batch("b2", [upsert("crm", "1", "Alice Zhang", city="Beijing")])
        self.assertEqual(len(service.snapshot()["entities"]), 2)
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice Zhang", city="Beijing"),
                            upsert("crm", "2", "Alice Zhan", city="Shanghai")])
        self.assert_same_resolution(service.snapshot(), records, **kwargs)
        service.apply_batch("b3", [upsert("crm", "1", "Alice Zhang", city="SH")])
        self.assertEqual(len(service.snapshot()["entities"]), 1)
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice Zhang", city="SH"),
                            upsert("crm", "2", "Alice Zhan", city="Shanghai")])
        self.assert_same_resolution(service.snapshot(), records, **kwargs)

    def test_complete_link_conflict_survives_member_update(self):
        ops = [upsert("crm", "1", "Alice", email="a@x.test"),
               upsert("crm", "2", "Alice"),
               upsert("crm", "3", "Alice", email="c@x.test")]
        self.service.apply_batch("b1", ops)
        self.assertEqual(sorted(len(e["members"]) for e in
                                self.service.snapshot()["entities"].values()), [1, 2])
        receipt = self.service.apply_batch("b2", [upsert("crm", "2", "Alice", phone="555")])
        self.assertFalse(receipt["fallback"])
        snapshot = self.service.snapshot()
        # A~B and B~C weakly match, A and C conflict: B's update must not
        # transitively merge all three.
        self.assertEqual(sorted(len(e["members"]) for e in snapshot["entities"].values()), [1, 2])
        self.assertTrue(any(e.get("blocked_transitive") for e in snapshot["evidence"]))
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice", email="a@x.test"),
                            upsert("crm", "2", "Alice", phone="555"),
                            upsert("crm", "3", "Alice", email="c@x.test")])
        self.assert_same_resolution(snapshot, records)

    def test_delete_unblocks_merge_through_unaffected_block(self):
        # u~w match exactly; u~v fuzzy through a city block that contains no
        # changed record; w~v have conflicting phones, so complete-link blocks
        # {u,w}⋈{v}. Deleting w must let u and v merge: the closure has to grow
        # from w's blocks through u's component into u's *other* blocks.
        ops = [upsert("crm", "u", "Ursula", email="u@x.test", city="Shanghai"),
               upsert("crm", "w", "Wendy", email="u@x.test", phone="1", city=""),
               upsert("crm", "v", "Ursula", phone="2", city="Shanghai")]
        self.service.apply_batch("b1", ops)
        entities = self.service.snapshot()["entities"]
        self.assertEqual(sorted(len(e["members"]) for e in entities.values()), [1, 2])
        self.assertTrue(any(e.get("blocked_transitive") for e in self.service.snapshot()["evidence"]))
        receipt = self.service.apply_batch("b2", [delete("crm", "w")])
        self.assertFalse(receipt["fallback"])
        entities = self.service.snapshot()["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(len(next(iter(entities.values()))["members"]), 2)
        records = {}
        apply_ops(records, [upsert("crm", "u", "Ursula", email="u@x.test", city="Shanghai"),
                            upsert("crm", "v", "Ursula", phone="2", city="Shanghai")])
        self.assert_same_resolution(self.service.snapshot(), records)

    def test_noop_batch_keeps_entities_and_evidence(self):
        ops = [upsert("crm", "1", email="a@x.test"), upsert("crm", "2", email="a@x.test")]
        self.service.apply_batch("b1", ops)
        before = self.service.snapshot()
        receipt = self.service.apply_batch("b2", [upsert("crm", "1", email="a@x.test"),
                                                  delete("crm", "9")])
        self.assertFalse(receipt["fallback"])
        self.assertEqual(receipt["changed_records"], 0)
        self.assertEqual(receipt["matcher_calls"], 0)
        after = self.service.snapshot()
        self.assertEqual(after["entities"], before["entities"])
        self.assertEqual(after["evidence"], before["evidence"])

    def test_unrelated_candidate_blocks_are_not_rematched(self):
        matcher = RecordingMatcher()
        service = EntityService(Path(self.tmp.name) / "local.sqlite", matcher=matcher)
        self.addCleanup(service.close)
        ops = [upsert("crm", f"a{i}", name=f"Alice{i}", city="Qingdao") for i in range(4)]
        ops += [upsert("crm", f"b{i}", name=f"Bob{i}", city="Beijing") for i in range(4)]
        service.apply_batch("setup", ops)
        matcher.calls.clear()
        receipt = service.apply_batch(
            "b1", [upsert("crm", "a0", name="Alice0 Changed", city="Qingdao")])
        self.assertFalse(receipt["fallback"])
        self.assertEqual(receipt["matcher_calls"], len(matcher.calls))
        self.assertTrue(matcher.calls)
        involved = {str(name).casefold() for pair in matcher.calls for name in pair}
        self.assertTrue(any(name.startswith("alice") for name in involved))
        self.assertTrue(all(not name.startswith("bob") for name in involved),
                        f"unrelated block was rematched: {involved}")
        # A full resolve re-invokes the matcher for the unrelated block.
        matcher.calls.clear()
        service.resolve()
        self.assertTrue(any(str(name).casefold().startswith("bob")
                            for pair in matcher.calls for name in pair))

    def test_input_order_permutations_converge(self):
        ops = [upsert("crm", "1", "Alice", email="a@x.test"),
               upsert("web", "2", "Alice", email="a@x.test"),
               upsert("crm", "3", "Bob", phone="1"),
               delete("web", "9"),
               upsert("web", "4", "Cid", city="Beijing")]
        services = []
        for i in range(4):
            service = EntityService(Path(self.tmp.name) / f"perm{i}.sqlite")
            self.addCleanup(service.close)
            services.append(service)
        services[0].apply_batch("b", ops)
        services[1].apply_batch("b", list(reversed(ops)))
        self.assertEqual(services[0].snapshot()["entities"], services[1].snapshot()["entities"])
        self.assertEqual(services[0].snapshot()["evidence"], services[1].snapshot()["evidence"])
        first, second = ops[:2] + [ops[4]], [ops[2], ops[3]]
        services[2].apply_batch("p", first)
        services[2].apply_batch("q", second)
        services[3].apply_batch("q", second)
        services[3].apply_batch("p", first)
        self.assertEqual(services[2].snapshot()["entities"], services[3].snapshot()["entities"])
        self.assertEqual(services[2].snapshot()["evidence"], services[3].snapshot()["evidence"])
        records = {}
        apply_ops(records, ops)
        self.assert_same_resolution(services[2].snapshot(), records)

    # --- explicit full fallback ----------------------------------------

    def test_rules_change_forces_reported_full_fallback(self):
        self.service.apply_batch("b1", [upsert("crm", "1", "Alice"),
                                        upsert("web", "2", "Alicia")])
        self.service.close()
        other = EntityService(self.path, matcher=EvidenceMatcher(0.5))
        self.addCleanup(other.close)
        receipt = other.apply_batch("b2", [upsert("crm", "3", "Bob")])
        self.assertTrue(receipt["fallback"])
        self.assertEqual(receipt["fallback_reason"], "rules_changed")
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice"), upsert("web", "2", "Alicia"),
                            upsert("crm", "3", "Bob")])
        self.assert_same_resolution(other.snapshot(), records, matcher=EvidenceMatcher(0.5))
        # Steady state again: the next batch under the new rules is incremental.
        followup = other.apply_batch("b3", [upsert("crm", "4", "Cid")])
        self.assertFalse(followup["fallback"])

    def test_ingest_invalidation_reports_fallback_once(self):
        self.service.ingest([{"source": "crm", "id": "1",
                              "fields": {"name": "Alice", "city": "Shanghai"}},
                             {"source": "crm", "id": "2",
                              "fields": {"name": "Alice", "city": "Shanghai"}}])
        receipt = self.service.apply_batch("b1", [upsert("crm", "3", "Bob", city="Beijing")])
        self.assertTrue(receipt["fallback"])
        self.assertEqual(receipt["fallback_reason"], "entities_not_resolved")
        followup = self.service.apply_batch("b2", [upsert("crm", "4", "Cid", city="Beijing")])
        self.assertFalse(followup["fallback"])
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice"), upsert("crm", "2", "Alice"),
                            upsert("crm", "3", "Bob", city="Beijing"),
                            upsert("crm", "4", "Cid", city="Beijing")])
        self.assert_same_resolution(self.service.snapshot(), records)

    def test_manual_merge_then_batch_falls_back_to_full(self):
        self.service.apply_batch("b1", [upsert("crm", "1", "Alice"),
                                        upsert("crm", "2", "Bob", city="Beijing")])
        entities = self.service.snapshot()["entities"]
        self.service.merge(list(entities))
        receipt = self.service.apply_batch("b3", [upsert("crm", "3", "Cid", city="Beijing")])
        self.assertTrue(receipt["fallback"])
        self.assertEqual(receipt["fallback_reason"], "entities_not_resolved")
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice"),
                            upsert("crm", "2", "Bob", city="Beijing"),
                            upsert("crm", "3", "Cid", city="Beijing")])
        # The manual merge is not an automatic-resolution fixpoint; the batch
        # result must equal full resolution of the post-batch records.
        self.assert_same_resolution(self.service.snapshot(), records)

    def test_rollback_rebuilds_index_and_batches_continue(self):
        self.service.apply_batch("b1", [upsert("crm", "1", "Alice", email="a@x.test")])
        self.service.apply_batch("b2", [upsert("crm", "2", "Alice", email="a@x.test")])
        self.service.rollback(1)
        receipt = self.service.apply_batch("b3", [upsert("crm", "3", "Alice", email="a@x.test")])
        self.assertFalse(receipt["fallback"])
        entities = self.service.snapshot()["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(len(next(iter(entities.values()))["members"]), 2)
        records = {}
        apply_ops(records, [upsert("crm", "1", "Alice", email="a@x.test"),
                            upsert("crm", "3", "Alice", email="a@x.test")])
        self.assert_same_resolution(self.service.snapshot(), records)

    # --- crash safety with real subprocesses ----------------------------

    def test_crash_after_writes_before_commit_leaves_no_partial_state(self):
        self.service.apply_batch("setup", [upsert("crm", "0", "Seed", city="Beijing")], expected=0)
        before = self.service.snapshot()
        self.service.close()
        script = """
import os, sys
from entityforge import EntityService
from entityforge.storage import SQLiteStore

class CrashStore(SQLiteStore):
    def _before_commit(self):
        os._exit(1)  # die after revision+index+receipt writes, before commit

service = EntityService(sys.argv[1])
service.store.close()
service.store = CrashStore(sys.argv[1])
service.apply_batch("b1", [
    {"op": "upsert", "source": "crm", "id": "1",
     "fields": {"name": "Alice", "city": "Shanghai"}},
], expected=1)
"""
        result = run_script(script, self.path)
        self.assertNotEqual(result.returncode, 0)
        reopened = EntityService(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.snapshot(), before)
        self.assertIsNone(reopened.store.get_batch("b1"))
        self.assertEqual(reopened.store.index_revision(), before["revision"])
        receipt = reopened.apply_batch("b1", [upsert("crm", "1")], expected=1)
        self.assertEqual(receipt["status"], "committed")

    def test_crash_during_resolution_leaves_no_partial_state(self):
        before = self.service.snapshot()
        self.service.close()
        script = """
import os, sys
from entityforge import EntityService

class KillMatcher:
    def compare(self, left, right):
        os._exit(1)  # die mid-transaction while clustering

service = EntityService(sys.argv[1], matcher=KillMatcher())
service.apply_batch("b1", [
    {"op": "upsert", "source": "crm", "id": "1",
     "fields": {"name": "Alice", "city": "Shanghai"}},
    {"op": "upsert", "source": "crm", "id": "2",
     "fields": {"name": "Alice", "city": "Shanghai"}},
], expected=0)
"""
        result = run_script(script, self.path)
        self.assertNotEqual(result.returncode, 0)
        reopened = EntityService(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.snapshot(), before)
        self.assertIsNone(reopened.store.get_batch("b1"))
        receipt = reopened.apply_batch("b1", [upsert("crm", "1"), upsert("crm", "2")], expected=0)
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(len(reopened.snapshot()["entities"]), 1)

    def test_lost_receipt_replays_after_restart(self):
        script = """
import sys
from entityforge import EntityService
service = EntityService(sys.argv[1])
service.apply_batch("b1", [
    {"op": "upsert", "source": "crm", "id": "1",
     "fields": {"name": "Alice", "city": "Shanghai"}},
], expected=0)
service.close()  # committed, but the receipt never reached the caller
"""
        result = run_script(script, self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        service = EntityService(self.path)
        self.addCleanup(service.close)
        history_len = len(service.store.history())
        same_ops = [{"op": "upsert", "source": "crm", "id": "1",
                     "fields": {"name": "Alice", "city": "Shanghai"}}]
        receipt = service.apply_batch("b1", same_ops, expected=0)
        self.assertEqual(receipt["status"], "replayed")
        self.assertEqual(receipt["revision"], 1)
        self.assertEqual(len(service.store.history()), history_len)

    def test_concurrent_processes_with_same_expected(self):
        self.service.apply_batch("setup", [upsert("crm", "0", "Seed")], expected=0)
        self.service.close()
        ops_files = []
        for i, key in enumerate(("1", "2")):
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, dir=self.tmp.name)
            json.dump([upsert("crm", key)], handle)
            handle.close()
            ops_files.append(handle.name)
        # Different batch ids racing on the same expected revision: exactly one wins.
        procs = [subprocess.Popen(
            [sys.executable, "-m", "entityforge.cli", "--db", str(self.path),
             "batch", ops_files[i], "--batch-id", f"c{i}", "--expected", "1"],
            env=dict(os.environ, PYTHONPATH=str(PROJECT / "src")),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
        outcomes = [p.communicate() + (p.returncode,) for p in procs]
        winners = [o for o in outcomes if o[2] == 0]
        losers = [o for o in outcomes if o[2] == 2]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(losers), 1, outcomes)
        self.assertIn("revision_conflict", losers[0][1])
        service = EntityService(self.path)
        self.addCleanup(service.close)
        self.assertEqual(service.snapshot()["revision"], 2)
        # Same batch id and content racing: both succeed, one is the replay.
        procs = [subprocess.Popen(
            [sys.executable, "-m", "entityforge.cli", "--db", str(self.path),
             "batch", ops_files[0], "--batch-id", "shared", "--expected", "2"],
            env=dict(os.environ, PYTHONPATH=str(PROJECT / "src")),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        receipts = []
        for p in procs:
            stdout, stderr = p.communicate()
            self.assertEqual(p.returncode, 0, stderr)
            receipts.append(json.loads(stdout))
        statuses = sorted(r["status"] for r in receipts)
        self.assertEqual(statuses, ["committed", "replayed"])
        self.assertEqual({r["revision"] for r in receipts}, {3})
        self.assertEqual(service.snapshot()["revision"], 3)

    # --- CLI surface -----------------------------------------------------

    def test_cli_batch_receipts_and_errors(self):
        def write_ops(payload, name):
            path = Path(self.tmp.name) / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            return path

        committed = run_cli(self.path, "batch", write_ops([upsert("crm", "1")], "a.json"),
                            "--batch-id", "b1", "--expected", "0")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        receipt = json.loads(committed.stdout)
        self.assertEqual((receipt["status"], receipt["revision"]), ("committed", 1))
        self.assertFalse(receipt["fallback"])
        replay = run_cli(self.path, "batch", write_ops([upsert("crm", "1")], "b.json"),
                         "--batch-id", "b1", "--expected", "0")
        self.assertEqual(json.loads(replay.stdout)["status"], "replayed")
        conflict = run_cli(self.path, "batch", write_ops([upsert("crm", "2")], "c.json"),
                           "--batch-id", "b1")
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("batch_conflict", conflict.stderr)
        stale = run_cli(self.path, "batch", write_ops([upsert("crm", "2")], "d.json"),
                        "--batch-id", "b2", "--expected", "0")
        self.assertEqual(stale.returncode, 2)
        self.assertIn("revision_conflict", stale.stderr)
        # Payload form carries batch id and expected inside the JSON document.
        payload = run_cli(self.path, "batch", write_ops(
            {"batch_id": "b3", "operations": [upsert("crm", "3")]}, "e.json"))
        self.assertEqual(payload.returncode, 0, payload.stderr)
        self.assertEqual(json.loads(payload.stdout)["status"], "committed")
        # A legacy ingest invalidates entities; the next batch reports the fallback.
        legacy = Path(self.tmp.name) / "legacy.json"
        legacy.write_text(json.dumps(
            [{"source": "crm", "id": "9", "fields": {"name": "Zed", "city": "Shanghai"}}]),
            encoding="utf-8")
        self.assertEqual(run_cli(self.path, "ingest", legacy).returncode, 0)
        fell_back = run_cli(self.path, "batch", write_ops([upsert("crm", "4")], "f.json"),
                            "--batch-id", "b4")
        receipt = json.loads(fell_back.stdout)
        self.assertTrue(receipt["fallback"])
        self.assertEqual(receipt["fallback_reason"], "entities_not_resolved")


if __name__ == "__main__":
    unittest.main()
