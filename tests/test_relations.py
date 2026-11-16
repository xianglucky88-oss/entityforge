"""Tests for the independent relationship hierarchy capability.

The exhaustive oracle in ``solver.enumerate_forests`` independently enumerates
every legal forest on small graphs; every solving result is cross-checked
against it, including the alternative-root counterexample from the request,
ties, duplicate assertions, post-merge self-loops, interval boundaries and
budget rejection.

Edge notation follows the request: an edge written ``B->A`` is parent -> child,
i.e. A's parent is B.  The assertion contract always names the child endpoint
explicitly, so ``edge(aid, "b", "a", score)`` builds an assertion whose child
is record ``a`` and whose parent is record ``b``.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from entityforge import EntityService
from entityforge.relations import (
    BudgetExhausted,
    RelationBatchRejected,
    RelationEngine,
    ROOT,
    VerificationError,
    verify_segment,
)
from entityforge.relations.contract import (
    RULE_VERSION,
    Assertion,
    load_pinned_snapshot,
    parse_instant,
    record_digest,
)
from entityforge.relations.solver import (
    branch_and_bound,
    edmonds_contract,
    enumerate_all_forests,
    enumerate_forests,
    fold_support,
    solve_segment,
)
from entityforge.relations.timeline import boundary_segments, solve_timeline

START = "2026-01-01T00:00:00Z"
END = "2027-01-01T00:00:00Z"


def assertion(aid, child, parent, score, *, start=START, end=END):
    """Build a raw assertion row; parent None means an explicit root claim."""
    return {
        "assertion_id": aid,
        "child": {"source": "crm", "id": child},
        "parent": None if parent is None else {"source": "crm", "id": parent},
        "score": score, "start": start, "end": end,
    }


class RelationTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entity_db = Path(self.tmp.name) / "entities.sqlite"
        self.artifact = Path(self.tmp.name) / "relations.sqlite"
        self.service = EntityService(self.entity_db)
        self.service.ingest([
            {"source": "crm", "id": "a", "fields": {"name": "A"}},
            {"source": "crm", "id": "b", "fields": {"name": "B"}},
            {"source": "crm", "id": "c", "fields": {"name": "C"}},
        ])
        self.snapshot = self.service.resolve()
        self.pinned = load_pinned_snapshot(self.entity_db)
        self.order = sorted(self.pinned.entity_members)
        self.entity_of = {
            letter: self.pinned.member_map[EntityService.record_key("crm", letter)]
            for letter in "abc"}
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def engine(self, **kwargs):
        return RelationEngine(self.artifact, self.entity_db, **kwargs)

    def model_assertion(self, aid, child_letter, parent_letter, score):
        """Assertion with explicit child; parent_letter None is a root claim."""
        child_entity = self.entity_of[child_letter]
        child_key = self.pinned.entity_members[child_entity][0]
        if parent_letter is None:
            return Assertion(aid, child_key, None, score,
                             parse_instant(START), parse_instant(END),
                             f"h{aid}", child_entity, None)
        parent_entity = self.entity_of[parent_letter]
        parent_key = self.pinned.entity_members[parent_entity][0]
        return Assertion(aid, child_key, parent_key, score,
                         parse_instant(START), parse_instant(END),
                         f"h{aid}", child_entity, parent_entity)

    def edge(self, aid, parent_letter, child_letter, score):
        """Request-notation edge parent->child (root parent uses None)."""
        return self.model_assertion(aid, child_letter, parent_letter, score)


class AlternativeRootCounterexampleTests(RelationTestBase):
    """B->A=5, C->B=6, A->C=7 form a cycle; root->A=0, root->B=0, root->C=6.

    The exact optimum is root->C, C->B, B->A, scoring 6+6+5 = 17.  Picking
    each node's maximum incoming edge then deleting the weakest cycle edge
    (B->A=5) drops A onto a zero-score root and yields only 7+6+0 = 13, so a
    global exact solver is mandatory.  The exhaustive enumeration oracle is
    the ground truth here.
    """

    def _request_assertions(self, root_c=6):
        return [
            self.edge("ba", "b", "a", 5),
            self.edge("cb", "c", "b", 6),
            self.edge("ac", "a", "c", 7),
            self.edge("ra", None, "a", 0),
            self.edge("rb", None, "b", 0),
            self.edge("rc", None, "c", root_c),
        ]

    def test_named_forest_scores_seventeen_and_is_unique_optimum(self):
        model = fold_support(self._request_assertions(), self.order)
        result = solve_segment(model, method="edmonds")
        _, oracle_score, _ = enumerate_forests(model)
        self.assertEqual(oracle_score, 17)
        self.assertEqual(result["total_score"], 17)
        self.assertGreater(result["total_score"], 7 + 6 + 0)
        self.assertEqual(result["selected"][self.entity_of["a"]], self.entity_of["b"])
        self.assertEqual(result["selected"][self.entity_of["b"]], self.entity_of["c"])
        self.assertEqual(result["selected"][self.entity_of["c"]], ROOT)
        optimal = [f for f in enumerate_all_forests(model) if f[0] == 17]
        self.assertEqual(len(optimal), 1)

    def test_all_solvers_and_oracle_agree_on_the_request_example(self):
        model = fold_support(self._request_assertions(), self.order)
        edmonds = solve_segment(model, method="edmonds")
        bounded = solve_segment(model, method="branch_and_bound")
        oracle_set, oracle_score, oracle_parents = enumerate_forests(model)
        self.assertEqual(edmonds["total_score"], 17)
        self.assertEqual(bounded["total_score"], 17)
        self.assertEqual(edmonds["selected"], bounded["selected"])
        expected_sequence = [
            ROOT if p == model.root_idx else self.order[p] for p in oracle_parents]
        self.assertEqual(edmonds["parent_sequence"], expected_sequence)
        self.assertEqual(bounded["parent_sequence"], expected_sequence)
        self.assertEqual(edmonds_contract(model), oracle_set)

    def test_edmonds_matches_oracle_across_root_weight_sweep(self):
        for root_c in range(0, 9):
            model = fold_support(self._request_assertions(root_c), self.order)
            selected = edmonds_contract(model)
            oracle_set, oracle_score, _ = enumerate_forests(model)
            score = sum(model.edges[eid].score for eid in selected)
            self.assertEqual(score, oracle_score)
            self.assertEqual(selected, oracle_set)

    def test_greedy_weakest_edge_deletion_is_demonstrably_suboptimal(self):
        model = fold_support(self._request_assertions(), self.order)
        exact = solve_segment(model, method="edmonds")["total_score"]
        # Simulate the forbidden heuristic independently.
        chosen, incoming = {}, {}
        for e in model.edges:
            incoming.setdefault(e.child_idx, []).append(e)
        for child_i, candidates in incoming.items():
            chosen[child_i] = max(candidates, key=lambda e: e.score)
        # The chosen edges contain a cycle; the heuristic removes its weakest
        # edge and reattaches that child to its implicit zero-score root.
        cycle_edges = [e for e in chosen.values() if e.parent_idx != model.root_idx]
        weakest = min(cycle_edges, key=lambda e: e.score)
        greedy_score = sum(e.score for e in chosen.values()) - weakest.score
        self.assertLess(greedy_score, exact)
        self.assertEqual((greedy_score, exact), (13, 17))


class TieBreakTests(RelationTestBase):
    def test_parent_sequence_tie_break_is_stable(self):
        # A has two equal-score parents B and C; fixed identity order decides.
        assertions = [
            self.edge("ab", "b", "a", 4),
            self.edge("ac", "c", "a", 4),
            self.edge("root-a", None, "a", 4),
        ]
        model = fold_support(assertions, self.order)
        first = solve_segment(model, method="edmonds")
        second = solve_segment(model, method="edmonds")
        bounded = solve_segment(model, method="branch_and_bound")
        self.assertEqual(first["selected"], second["selected"])
        self.assertEqual(first["selected"], bounded["selected"])
        _, score, _ = enumerate_forests(model)
        self.assertEqual(first["total_score"], score)
        chosen_parent = first["selected"][self.entity_of["a"]]
        self.assertEqual(chosen_parent, min(
            self.entity_of["b"], self.entity_of["c"], key=self.order.index))

    def test_tie_lost_edges_are_not_labeled_as_wrong(self):
        assertions = [
            self.edge("ab", "b", "a", 4),
            self.edge("ac", "c", "a", 4),
        ]
        model = fold_support(assertions, self.order)
        result = solve_segment(model, method="edmonds")
        excluded = [row for row in result["evidence"] if row["status"] == "excluded"]
        self.assertTrue(excluded)
        for row in excluded:
            self.assertIn(row["reason"],
                          {"lower_score", "tie_lost", "acyclicity_constraint"})
            serialized = json.dumps(row)
            for forbidden in ("false_relation", "wrong", "incorrect", "invalid"):
                self.assertNotIn(forbidden, serialized)


class FoldingAndDuplicateTests(RelationTestBase):
    def test_same_edge_folds_to_max_score_and_keeps_top_sources(self):
        assertions = [
            self.edge("low", "b", "a", 2),
            self.edge("high", "b", "a", 9),
            self.edge("mid", "b", "a", 5),
        ]
        model = fold_support(assertions, self.order)
        edge = next(e for e in model.edges
                    if e.child_idx == self.order.index(self.entity_of["a"])
                    and e.parent_idx == self.order.index(self.entity_of["b"]))
        self.assertEqual(edge.score, 9)
        self.assertEqual([s["assertion_id"] for s in edge.support], ["high"])

    def test_equal_supporting_assertions_are_all_retained(self):
        assertions = [
            self.edge("one", "b", "a", 7),
            self.edge("two", "b", "a", 7),
        ]
        model = fold_support(assertions, self.order)
        edge = next(e for e in model.edges if e.support)
        self.assertEqual(edge.score, 7)
        self.assertEqual(len(edge.support), 2)

    def test_duplicate_id_is_idempotent_never_additive(self):
        engine = self.engine()
        engine.submit([assertion("d1", "a", "b", 3)])
        view = engine.submit([assertion("d1", "a", "b", 3)])
        self.assertEqual(view["batches"][-1]["duplicates"], ["d1"])
        interval = engine.intervals()[0]
        row = next(r for r in interval["evidence"]
                   if r["child_entity"] == self.entity_of["a"]
                   and r["parent_entity"] == self.entity_of["b"])
        self.assertEqual(row["score"], 3)
        self.assertEqual(len(row["support"]), 1)

    def test_same_id_with_changed_content_rejects_batch(self):
        engine = self.engine()
        engine.submit([assertion("d1", "a", "b", 3)])
        with self.assertRaises(RelationBatchRejected):
            engine.submit([assertion("d1", "a", "c", 3)])
        self.assertEqual(
            [a.assertion_id for a in engine.store.load_assertions()], ["d1"])

    def test_distinct_ids_same_edge_are_independent_sources(self):
        engine = self.engine()
        engine.submit([assertion("d1", "a", "b", 3)])
        engine.submit([assertion("d2", "a", "b", 3)])
        row = next(r for r in engine.intervals()[0]["evidence"]
                   if r["child_entity"] == self.entity_of["a"]
                   and r["parent_entity"] == self.entity_of["b"])
        self.assertEqual(len(row["support"]), 2)
        self.assertEqual(row["score"], 3)


class EndpointContractTests(RelationTestBase):
    def test_unknown_endpoint_rejects_whole_batch(self):
        engine = self.engine()
        with self.assertRaises(RelationBatchRejected):
            engine.submit([assertion("ok", "a", "b", 1),
                           assertion("bad", "a", "ghost", 1)])
        self.assertEqual(engine.store.load_assertions(), [])

    def test_content_digest_mismatch_rejects_whole_batch(self):
        record = self.service.snapshot()["records"][
            EntityService.record_key("crm", "a")]
        row = assertion("ok", "a", "b", 1)
        row["child"]["digest"] = "wrong"
        self.assertNotEqual("wrong", record_digest(record))
        engine = self.engine()
        with self.assertRaises(RelationBatchRejected):
            engine.submit([row])
        self.assertEqual(engine.store.load_assertions(), [])

    def test_matching_digest_is_accepted(self):
        record = self.service.snapshot()["records"][
            EntityService.record_key("crm", "a")]
        row = assertion("ok", "a", "b", 1)
        row["child"]["digest"] = record_digest(record)
        engine = self.engine()
        view = engine.submit([row])
        self.assertEqual(view["pinned_revision"], self.pinned.revision)

    def test_naive_datetime_is_rejected(self):
        engine = self.engine()
        row = assertion("ok", "a", "b", 1)
        row["start"] = "2026-01-01T00:00:00"
        with self.assertRaises(RelationBatchRejected):
            engine.submit([row])

    def test_negative_and_boolean_scores_rejected(self):
        engine = self.engine()
        with self.assertRaises(RelationBatchRejected):
            engine.submit([assertion("n", "a", "b", -1)])
        with self.assertRaises(RelationBatchRejected):
            engine.submit([assertion("b", "a", "b", True)])

    def test_null_parent_is_explicit_root(self):
        engine = self.engine()
        view = engine.submit([assertion("root", "a", None, 2)])
        interval = view["intervals"][0]
        row = next(r for r in interval["evidence"]
                   if r["child_entity"] == self.entity_of["a"]
                   and r["parent_entity"] == ROOT and not r["implicit_root"])
        self.assertEqual(row["status"], "selected")
        self.assertEqual(row["score"], 2)

    def test_implicit_zero_root_keeps_forest_feasible_without_root_claims(self):
        engine = self.engine()
        view = engine.submit([assertion("edge", "a", "b", 4)])
        selected = view["intervals"][0]["selected"]
        self.assertEqual(selected[self.entity_of["a"]], self.entity_of["b"])
        self.assertEqual(selected[self.entity_of["b"]], ROOT)
        self.assertEqual(selected[self.entity_of["c"]], ROOT)

    def test_non_half_open_or_invalid_interval_rejected(self):
        engine = self.engine()
        bad = assertion("x", "a", "b", 1,
                        start="2026-02-01T00:00:00Z", end="2026-02-01T00:00:00Z")
        with self.assertRaises(RelationBatchRejected):
            engine.submit([bad])

    def test_offset_timezones_are_normalized_to_utc(self):
        engine = self.engine()
        engine.submit([assertion(
            "tz", "a", "b", 3,
            start="2026-01-01T09:00:00+09:00",
            end="2026-02-01T09:00:00+09:00")])
        stored = engine.store.load_assertions()[0]
        self.assertEqual(stored.start.isoformat(),
                         "2026-01-01T00:00:00+00:00")
        inside = engine.at("2026-01-01T00:30:00Z")
        self.assertTrue(inside["covered"])
        before = engine.at("2026-01-01T08:30:00+09:00")  # 23:30Z prior day
        self.assertFalse(before["covered"])


class SelfLoopTests(RelationTestBase):
    def test_intra_entity_edge_is_excluded_as_self_loop(self):
        merged_id = self.service.merge(list(self.snapshot["entities"]))
        engine = RelationEngine(
            Path(self.tmp.name) / "merged-rel.sqlite", self.entity_db)
        view = engine.submit([
            assertion("loop", "a", "b", 9),
            assertion("root", "a", None, 2),
        ])
        interval = view["intervals"][0]
        self.assertEqual(len(interval["self_loops"]), 1)
        self.assertEqual(interval["self_loops"][0]["reason"], "self_loop_excluded")
        self.assertEqual(interval["self_loops"][0]["child_entity"], merged_id)
        self.assertEqual(interval["selected"], {merged_id: ROOT})
        engine.close()

    def test_self_loop_never_enters_solver_model(self):
        merged_id = self.service.merge(list(self.snapshot["entities"]))
        pinned = load_pinned_snapshot(self.entity_db)
        order = sorted(pinned.entity_members)
        key = pinned.entity_members[merged_id][0]
        loop = Assertion("loop", key, key, 9,
                         parse_instant(START), parse_instant(END),
                         "hloop", merged_id, merged_id)
        model = fold_support([loop], order)
        self.assertEqual(len(model.edges), 1)
        self.assertTrue(model.edges[0].implicit_root)
        self.assertEqual(len(model.self_loops), 1)


class SingleEntityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.entity_db = Path(self.tmp.name) / "entities.sqlite"
        self.artifact = Path(self.tmp.name) / "relations.sqlite"
        self.service = EntityService(self.entity_db)
        self.service.ingest([
            {"source": "crm", "id": "solo", "fields": {"name": "Solo"}}])
        self.snapshot = self.service.resolve()
        self.addCleanup(self.service.close)

    def test_single_entity_root_claim_and_feasibility(self):
        engine = RelationEngine(self.artifact, self.entity_db)
        view = engine.submit([assertion("r", "solo", None, 4)])
        interval = view["intervals"][0]
        self.assertEqual(interval["total_score"], 4)
        self.assertEqual(len(interval["selected"]), 1)
        self.assertTrue(engine.verify()["ok"])
        engine.close()

    def test_self_loop_on_single_entity_is_excluded(self):
        engine = RelationEngine(self.artifact, self.entity_db)
        view = engine.submit([assertion("loop", "solo", "solo", 9)])
        interval = view["intervals"][0]
        self.assertEqual(len(interval["self_loops"]), 1)
        # Only the implicit zero root remains, so the score is zero.
        self.assertEqual(interval["total_score"], 0)
        engine.close()


class TimelineTests(RelationTestBase):
    def test_resolve_only_at_boundaries_and_half_open_semantics(self):
        engine = self.engine()
        engine.submit([
            assertion("p1", "a", None, 4,
                      start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"),
            assertion("p2", "a", None, 4,
                      start="2026-02-01T00:00:00Z", end="2026-03-01T00:00:00Z"),
        ])
        intervals = engine.intervals()
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["merged_segments"], 2)
        self.assertEqual(len(intervals[0]["constituents"]), 2)

        before = engine.at("2026-01-31T23:59:59Z")
        boundary = engine.at("2026-02-01T00:00:00Z")
        after = engine.at("2026-02-01T00:00:01Z")
        self.assertEqual(before["active_assertions"], ["p1"])
        self.assertEqual(boundary["active_assertions"], ["p2"])
        self.assertEqual(after["active_assertions"], ["p2"])

    def test_same_instant_revoke_and_add_half_open(self):
        engine = self.engine()
        engine.submit([
            assertion("old", "a", "b", 3,
                      start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"),
            assertion("new", "a", "c", 8,
                      start="2026-02-01T00:00:00Z", end="2026-03-01T00:00:00Z"),
        ])
        self.assertEqual(len(engine.intervals()), 2)
        at_boundary = engine.at("2026-02-01T00:00:00Z")
        just_before = engine.at("2026-01-31T23:59:59.999999Z")
        self.assertEqual(at_boundary["active_assertions"], ["new"])
        self.assertEqual(just_before["active_assertions"], ["old"])

    def test_query_outside_coverage_returns_baseline_not_stale_forest(self):
        engine = self.engine()
        engine.submit([assertion("p1", "a", "b", 3,
                                 start="2026-01-01T00:00:00Z",
                                 end="2026-02-01T00:00:00Z")])
        result = engine.at("2030-01-01T00:00:00Z")
        self.assertFalse(result["covered"])
        self.assertEqual(result["total_score"], 0)
        self.assertTrue(all(row["implicit_root"] for row in result["evidence"]))

    def test_evidence_changes_survive_adjacent_interval_merge(self):
        engine = self.engine()
        engine.submit([
            assertion("p1", "a", None, 4,
                      start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"),
            assertion("p2", "a", None, 4,
                      start="2026-02-01T00:00:00Z", end="2026-03-01T00:00:00Z"),
        ])
        merged = engine.intervals()[0]
        constituent_ids = [
            sorted(c["active_assertions"]) for c in merged["constituents"]]
        self.assertEqual(constituent_ids, [["p1"], ["p2"]])
        # Point queries inside the merged interval expose the right evidence.
        self.assertEqual(
            engine.at("2026-01-15T00:00:00Z")["active_assertions"], ["p1"])
        self.assertEqual(
            engine.at("2026-02-15T00:00:00Z")["active_assertions"], ["p2"])

    def test_boundary_segments_cover_each_active_set_once(self):
        loop = Assertion(
            "x", self.pinned.entity_members[self.entity_of["a"]][0],
            self.pinned.entity_members[self.entity_of["b"]][0], 1,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 4, 1, tzinfo=timezone.utc), "hx",
            self.entity_of["a"], self.entity_of["b"])
        segments = boundary_segments([loop])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][2], [loop])


class PinnedRevisionTests(RelationTestBase):
    def test_head_advance_does_not_replace_member_mapping(self):
        engine = self.engine()
        view = engine.submit([assertion("p1", "a", "b", 2)])
        pinned_before = view["pinned_revision"]
        self.service.ingest([
            {"source": "crm", "id": "d", "fields": {"name": "D"}}])
        self.service.resolve()
        view_after = engine.view()
        self.assertEqual(view_after["pinned_revision"], pinned_before)
        self.assertEqual(view_after["assertion_summary"]["count"], 1)
        for interval in engine.intervals():
            self.assertEqual(interval["pinned_revision"], pinned_before)
            self.assertEqual(interval["rule_version"], RULE_VERSION)
            self.assertIn("digest", interval["assertion_summary"])

    def test_artifact_reopens_independently_and_stays_reproducible(self):
        engine = self.engine()
        engine.submit([assertion("p1", "a", "b", 2)])
        engine.close()
        reopened = RelationEngine(self.artifact, self.entity_db)
        interval = reopened.intervals()[0]
        self.assertEqual(interval["selected"][self.entity_of["a"]],
                         self.entity_of["b"])
        reopened.close()

    def test_result_carries_revision_summary_and_rule_version(self):
        engine = self.engine()
        view = engine.submit([assertion("p1", "a", "b", 2)])
        self.assertEqual(view["rule_version"], RULE_VERSION)
        self.assertIn("solver_config", view)
        interval = view["intervals"][0]
        self.assertEqual(interval["assertion_summary"]["assertion_ids"], ["p1"])
        self.assertEqual(interval["solver"]["rule_version"], RULE_VERSION)


class BudgetTests(RelationTestBase):
    def _cycle_model(self):
        return fold_support([
            self.edge("ba", "b", "a", 5),
            self.edge("cb", "c", "b", 6),
            self.edge("ac", "a", "c", 7),
            self.edge("rc", None, "c", 6),
        ], self.order)

    def test_node_budget_exhaustion_raises_instead_of_returning_partial(self):
        model = self._cycle_model()
        with self.assertRaises(BudgetExhausted) as caught:
            branch_and_bound(model, node_budget=2, solution_budget=10**9)
        self.assertIn("no partial result is accepted as optimal",
                      str(caught.exception))
        self.assertLessEqual(caught.exception.stats["nodes"], 3)

    def test_solution_budget_is_enforced(self):
        model = fold_support([
            self.edge("ba", "b", "a", 5),
            self.edge("cb", "c", "b", 6),
        ], self.order)
        with self.assertRaises(BudgetExhausted):
            branch_and_bound(model, node_budget=10**9, solution_budget=0)

    def test_budget_zero_never_returns_a_result(self):
        model = self._cycle_model()
        with self.assertRaises(BudgetExhausted):
            branch_and_bound(model, node_budget=0, solution_budget=0)

    def test_generous_budget_completes_and_matches_exact_solver(self):
        model = self._cycle_model()
        selected = branch_and_bound(
            model, node_budget=10**9, solution_budget=10**9)
        self.assertEqual(selected, edmonds_contract(model))

    def test_edmonds_is_budget_free_and_always_exact(self):
        model = self._cycle_model()
        selected = edmonds_contract(model)
        oracle_set, oracle_score, _ = enumerate_forests(model)
        self.assertEqual(selected, oracle_set)
        self.assertEqual(sum(model.edges[e].score for e in selected), oracle_score)


class VerificationTests(RelationTestBase):
    def _solved(self):
        assertions = [
            self.edge("ba", "b", "a", 5),
            self.edge("cb", "c", "b", 6),
            self.edge("ac", "a", "c", 7),
            self.edge("rc", None, "c", 6),
        ]
        model = fold_support(assertions, self.order)
        return solve_segment(model, method="edmonds"), assertions

    def test_verifier_accepts_genuine_result(self):
        result, assertions = self._solved()
        report = verify_segment(
            result, assertions, self.order,
            assertion_digests={a.assertion_id: a.digest for a in assertions})
        self.assertTrue(report["ok"])
        self.assertIn("acyclic", report["checks"])
        self.assertIn("one_parent", report["checks"])
        self.assertIn("independent_optimality", report["checks"])

    def test_verifier_detects_tampered_score(self):
        result, assertions = self._solved()
        result["total_score"] = 999
        with self.assertRaises(VerificationError):
            verify_segment(result, assertions, self.order)

    def test_verifier_detects_cycle_introduction(self):
        result, assertions = self._solved()
        ring = {
            self.entity_of["a"]: self.entity_of["c"],
            self.entity_of["b"]: self.entity_of["a"],
            self.entity_of["c"]: self.entity_of["b"],
        }
        for row in result["evidence"]:
            if row["status"] == "selected" and row["child_entity"] in ring:
                row["parent_entity"] = ring[row["child_entity"]]
        result["parent_sequence"] = [
            ring[entity] for entity in result["entity_order"]]
        with self.assertRaises(VerificationError):
            verify_segment(result, assertions, self.order)

    def test_verifier_detects_support_digest_tampering(self):
        result, assertions = self._solved()
        for row in result["evidence"]:
            for support in row["support"]:
                support["digest"] = "tampered"
        with self.assertRaises(VerificationError):
            verify_segment(
                result, assertions, self.order,
                assertion_digests={a.assertion_id: a.digest for a in assertions})

    def test_engine_verifies_every_persisted_interval(self):
        engine = self.engine()
        engine.submit([
            assertion("p1", "a", "b", 4,
                      start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"),
            assertion("p2", "b", "c", 4,
                      start="2026-02-01T00:00:00Z", end="2026-03-01T00:00:00Z"),
        ])
        report = engine.verify()
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["intervals"]), 2)

    def test_excluded_edges_carry_factual_reasons_only(self):
        result, _ = self._solved()
        allowed = {"lower_score", "tie_lost", "acyclicity_constraint"}
        for row in result["evidence"]:
            if row["status"] == "excluded":
                self.assertIn(row["reason"], allowed)


class SolverAgreementTests(RelationTestBase):
    def test_all_solvers_agree_across_dense_random_graphs(self):
        import random
        random.seed(11)
        for _ in range(120):
            assertions, aid = [], 0
            for parent in "abc":
                for child in "abc":
                    if parent != child and random.random() < 0.7:
                        aid += 1
                        assertions.append(self.edge(
                            f"e{aid}", parent, child, random.randint(0, 9)))
                if random.random() < 0.5:
                    aid += 1
                    assertions.append(self.edge(
                        f"r{aid}", None, parent, random.randint(0, 9)))
            model = fold_support(assertions, self.order)
            edmonds = solve_segment(model, method="edmonds")
            bounded = solve_segment(model, method="branch_and_bound")
            _, oracle_score, oracle_parents = enumerate_forests(model)
            self.assertEqual(edmonds["total_score"], oracle_score)
            self.assertEqual(bounded["total_score"], oracle_score)
            self.assertEqual(edmonds["selected"], bounded["selected"])
            self.assertEqual(
                edmonds["parent_sequence"],
                [ROOT if p == model.root_idx else self.order[p]
                 for p in oracle_parents])

    def test_timeline_solver_matches_direct_segment_solver(self):
        assertions = [
            self.edge("ba", "b", "a", 5),
            self.edge("rc", None, "c", 6),
        ]
        intervals = solve_timeline(assertions, self.order)
        model = fold_support(assertions, self.order)
        direct = solve_segment(model, method="edmonds")
        self.assertEqual(intervals[0]["selected"], direct["selected"])
        self.assertEqual(intervals[0]["total_score"], direct["total_score"])


class LargeGraphCrossCheckTests(unittest.TestCase):
    """Beyond the exhaustive-oracle limit, verification must cross-check with
    the other exact solver rather than pretending enumeration is feasible."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from entityforge.relations.verify import ORACLE_LIMIT
        self.n = ORACLE_LIMIT + 2
        self.order = [f"e{i:02d}" for i in range(self.n)]
        import random
        random.seed(5)
        start = parse_instant("2026-01-01T00:00:00Z")
        end = parse_instant("2027-01-01T00:00:00Z")
        assertions, aid = [], 0
        for ci, child in enumerate(self.order):
            for pi, parent in enumerate(self.order):
                if ci != pi and random.random() < 0.35:
                    aid += 1
                    assertions.append(Assertion(
                        f"a{aid}", f"k{ci}", f"k{pi}",
                        random.randint(0, 20), start, end, f"h{aid}",
                        child, parent))
            aid += 1
            assertions.append(Assertion(
                f"r{aid}", f"k{ci}", None,
                random.randint(0, 20), start, end, f"hr{aid}", child, None))
        self.assertions = assertions

    def test_both_solvers_agree_and_verify_beyond_oracle_limit(self):
        model = fold_support(self.assertions, self.order)
        edmonds = solve_segment(model, method="edmonds")
        bounded = solve_segment(model, method="branch_and_bound")
        self.assertEqual(edmonds["total_score"], bounded["total_score"])
        self.assertEqual(edmonds["selected"], bounded["selected"])
        report = verify_segment(edmonds, self.assertions, self.order)
        self.assertTrue(report["ok"])
        self.assertEqual(report["entities"], self.n)


class IsolationTests(RelationTestBase):
    def test_relation_work_does_not_modify_entity_state(self):
        before = self.service.snapshot()
        engine = self.engine()
        engine.submit([assertion("p1", "a", "b", 2)])
        engine.close()
        after = self.service.snapshot()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["entities"], after["entities"])
        self.assertEqual(before["records"], after["records"])

    def test_batch_rejection_leaves_no_partial_assertions(self):
        engine = self.engine()
        engine.submit([assertion("good", "a", "b", 2)])
        with self.assertRaises(RelationBatchRejected):
            engine.submit([assertion("ok", "b", "c", 2),
                           assertion("broken", "a", "ghost", 2)])
        self.assertEqual(
            [a.assertion_id for a in engine.store.load_assertions()], ["good"])
        self.assertTrue(engine.verify()["ok"])

    def test_rejected_batch_is_audited_but_not_applied(self):
        engine = self.engine()
        engine.submit([assertion("good", "a", "b", 2)])
        with self.assertRaises(RelationBatchRejected):
            engine.submit([assertion("broken", "a", "ghost", 2)])
        batches = engine.store.batches()
        self.assertFalse(batches[-1]["accepted"])
        self.assertEqual(batches[-1]["details"]["reason"], "batch_rejected")

    def test_solver_failure_leaves_previous_consistent_artifact(self):
        engine = self.engine(method="branch_and_bound",
                             node_budget=10**9, solution_budget=10**9)
        engine.submit([assertion("good", "a", "b", 2)])
        before_assertions = [a.assertion_id
                             for a in engine.store.load_assertions()]
        before_intervals = engine.store.load_intervals()
        # A budget far too small to finish makes solving raise before any
        # persistence; the previous artifact must remain byte-for-byte intact.
        engine.node_budget = 1
        with self.assertRaises(BudgetExhausted):
            engine.submit([assertion("more", "b", "c", 3)])
        self.assertEqual(
            [a.assertion_id for a in engine.store.load_assertions()],
            before_assertions)
        self.assertEqual(engine.store.load_intervals(), before_intervals)
        self.assertTrue(engine.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
