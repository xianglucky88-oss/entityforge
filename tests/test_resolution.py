import tempfile
import unittest
from pathlib import Path
from entityforge import EntityService
from entityforge.matching import Normalizer, PrioritySurvivor
from entityforge.storage import RevisionConflict


def record(source, key, name="Alice", email=None, city="Shanghai", phone=None):
    return {"source": source, "id": key, "fields": {
        "name": name, "email": email, "city": city, "phone": phone}}


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "master.sqlite"
        self.service = EntityService(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def test_normalized_identity_and_provenance(self):
        self.service.survivor = PrioritySurvivor({"crm": 0, "web": 1})
        self.service.ingest([record("web", "1", "ＡＬＩＣＥ", " Alice@x.test "),
                             record("crm", "2", "Alice", "alice@x.test")])
        result = self.service.resolve()
        self.assertEqual(len(result["entities"]), 1)
        entity = next(iter(result["entities"].values()))
        self.assertEqual(entity["fields"]["name"], "Alice")
        self.assertIn("crm", entity["origins"]["name"])
        self.assertTrue(result["evidence"][0]["matched"])

    def test_fuzzy_name_and_alias(self):
        self.service.normalizer = Normalizer({"sh": "Shanghai"})
        self.service.ingest([record("a", "1", "Alice Zhang", city="SH"),
                             record("b", "2", "Alice Zhan", city="Shanghai")])
        self.assertEqual(len(self.service.resolve()["entities"]), 1)

    def test_identifier_conflicts_block_transitive_join(self):
        self.service.ingest([record("a", "1", email="a@x.test"), record("b", "2"),
                             record("c", "3", email="c@x.test")])
        result = self.service.resolve()
        self.assertEqual(sorted(len(e["members"]) for e in result["entities"].values()), [1, 2])
        self.assertTrue(any(e.get("blocked_transitive") for e in result["evidence"]))

    def test_empty_values_do_not_match(self):
        self.service.ingest([record("a", "1", "", city=""), record("b", "2", "", city="")])
        self.assertEqual(len(self.service.resolve()["entities"]), 2)

    def test_manual_split_merge_and_rollback(self):
        self.service.ingest([record("a", "1"), record("b", "2")])
        resolved = self.service.resolve()
        eid, entity = next(iter(resolved["entities"].items()))
        split = self.service.split(eid, [[m] for m in entity["members"]])
        self.assertEqual(len(split["entities"]), 2)
        self.service.merge(list(split["entities"]))
        self.assertEqual(len(self.service.snapshot()["entities"]), 1)
        restored = self.service.rollback(split["revision"])
        self.assertEqual(len(restored["entities"]), 2)
        self.assertGreater(restored["revision"], split["revision"])

    def test_failed_mutation_is_atomic(self):
        self.service.ingest([record("a", "1"), record("b", "2")])
        snapshot = self.service.resolve()
        eid = next(iter(snapshot["entities"]))
        with self.assertRaises(ValueError):
            self.service.split(eid, [["missing"], ["missing"]])
        self.assertEqual(self.service.snapshot(), snapshot)

    def test_concurrent_revision_guard(self):
        other = EntityService(self.path)
        self.addCleanup(other.close)
        revision = other.snapshot()["revision"]
        self.service.ingest([record("a", "1")], expected=revision)
        with self.assertRaises(RevisionConflict):
            other.ingest([record("b", "1")], expected=revision)

    def test_upsert_invalidates_masters_and_survives_restart(self):
        self.service.ingest([record("a", "1")])
        self.service.resolve()
        self.service.ingest([record("a", "1", "Bob")])
        other = EntityService(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.snapshot()["entities"], {})
        self.assertEqual(len(other.snapshot()["records"]), 1)

    def test_bad_input_batch_does_not_partially_ingest(self):
        with self.assertRaises(ValueError):
            self.service.ingest([record("a", "1"), {"source": "bad"}])
        self.assertEqual(self.service.snapshot()["revision"], 0)

    def test_alias_cycles_rejected(self):
        with self.assertRaises(ValueError):
            Normalizer({"a": "b", "b": "a"}).normalize({"name": "a"})


if __name__ == "__main__":
    unittest.main()
