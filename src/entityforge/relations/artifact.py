"""Independent persistence artifact for relationship hierarchies.

The relation artifact lives in its own SQLite database and never touches the
entity store after the member mapping is pinned.  Every produced interval
carries the pinned input revision, an assertion digest summary and the solver
rule version, so results remain reproducible and independently verifiable
while the entity HEAD moves forward.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .contract import (
    RULE_VERSION,
    Assertion,
    PinnedSnapshot,
    RelationBatchRejected,
    format_instant,
    load_pinned_snapshot,
    parse_batch,
    parse_instant,
)
from .timeline import at_instant, baseline_forest, solve_timeline
from .verify import verify_segment


class RelationStore:
    """SQLite-backed relation artifact with its own revision counter."""

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS relation_meta (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS relation_batches (
              batch_revision INTEGER PRIMARY KEY,
              at TEXT NOT NULL,
              input_digest TEXT NOT NULL,
              accepted INTEGER NOT NULL,
              duplicates TEXT NOT NULL,
              details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assertions (
              assertion_id TEXT PRIMARY KEY,
              payload TEXT NOT NULL,
              digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS relation_intervals (
              ordinal INTEGER PRIMARY KEY,
              start TEXT NOT NULL,
              end TEXT NOT NULL,
              payload TEXT NOT NULL);
        """)

    def meta(self):
        row = self.db.execute(
            "SELECT payload FROM relation_meta WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def initialize(self, snapshot: PinnedSnapshot, *, method, node_budget,
                   solution_budget, cross_check_with):
        if self.meta() is not None:
            raise RuntimeError("relation artifact is already pinned to a snapshot")
        payload = {
            "pinned_revision": snapshot.revision,
            "rule_version": RULE_VERSION,
            "member_map": snapshot.member_map,
            "entity_members": snapshot.entity_members,
            "record_digests": snapshot.record_digests,
            "entity_order": sorted(snapshot.entity_members),
            "solver_config": {"method": method, "node_budget": node_budget,
                              "solution_budget": solution_budget,
                              "cross_check_with": cross_check_with},
            "assertion_summary": {},
        }
        with self.db:
            self.db.execute("INSERT INTO relation_meta VALUES (1,?)",
                            (json.dumps(payload, ensure_ascii=False),))

    def load_assertions(self):
        rows = self.db.execute(
            "SELECT payload FROM assertions ORDER BY assertion_id").fetchall()
        return [_deserialize_assertion(json.loads(row[0])) for row in rows]

    def replace_assertions(self, assertions):
        self.db.execute("DELETE FROM assertions")
        self.db.executemany(
            "INSERT INTO assertions VALUES (?,?,?)",
            [(a.assertion_id, json.dumps(_serialize_assertion(a),
                                         ensure_ascii=False), a.digest)
             for a in assertions])

    def commit_solution(self, assertions, intervals, summary_meta):
        """Atomically persist assertions, intervals and the digest summary.

        All three tables are replaced inside one transaction so a failure at
        any step rolls the artifact back to its previous consistent state;
        a half-written interval set can never be read back.
        """
        with self.db:
            self.replace_assertions(assertions)
            self.db.execute("DELETE FROM relation_intervals")
            self.db.executemany(
                "INSERT INTO relation_intervals VALUES (?,?,?,?)",
                [(i, interval["start"], interval["end"],
                  json.dumps(interval, ensure_ascii=False))
                 for i, interval in enumerate(intervals)])
            meta = self.meta()
            meta["assertion_summary"] = summary_meta
            self.db.execute("UPDATE relation_meta SET payload=? WHERE singleton=1",
                            (json.dumps(meta, ensure_ascii=False),))

    def record_batch(self, *, input_digest, accepted, duplicates, details):
        revision = 1 + self.db.execute(
            "SELECT COALESCE(MAX(batch_revision),0) FROM relation_batches").fetchone()[0]
        with self.db:
            self.db.execute("INSERT INTO relation_batches VALUES (?,?,?,?,?,?)",
                            (revision, datetime.now(timezone.utc).isoformat(),
                             input_digest, int(accepted),
                             json.dumps(duplicates, ensure_ascii=False),
                             json.dumps(details, ensure_ascii=False)))
        return revision

    def load_intervals(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM relation_intervals ORDER BY ordinal")]

    def batches(self):
        return [{"batch_revision": r, "at": t, "input_digest": d,
                 "accepted": bool(a), "duplicates": json.loads(u),
                 "details": json.loads(detail)}
                for r, t, d, a, u, detail in self.db.execute(
                    "SELECT * FROM relation_batches ORDER BY batch_revision")]

    def close(self):
        self.db.close()


def _serialize_assertion(assertion: Assertion):
    start, end = assertion.interval()
    return {"assertion_id": assertion.assertion_id,
            "child_key": assertion.child_key, "parent_key": assertion.parent_key,
            "score": assertion.score, "start": start, "end": end,
            "digest": assertion.digest, "child_entity": assertion.child_entity,
            "parent_entity": assertion.parent_entity}


def _deserialize_assertion(payload):
    return Assertion(
        assertion_id=payload["assertion_id"], child_key=payload["child_key"],
        parent_key=payload["parent_key"], score=payload["score"],
        start=parse_instant(payload["start"]), end=parse_instant(payload["end"]),
        digest=payload["digest"], child_entity=payload["child_entity"],
        parent_entity=payload["parent_entity"])


def _assertion_summary(assertions):
    ids = sorted(a.assertion_id for a in assertions)
    body = json.dumps(
        [{"id": a.assertion_id, "digest": a.digest} for a in
         sorted(assertions, key=lambda x: x.assertion_id)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"count": len(assertions), "assertion_ids": ids,
            "digest": hashlib.sha256(body.encode()).hexdigest()}


class RelationEngine:
    """Application entry point for the relationship hierarchy capability."""

    def __init__(self, artifact_path, entity_db_path, *, method="edmonds",
                 node_budget=1_000_000, solution_budget=1_000_000,
                 cross_check_with=None):
        self.store = RelationStore(artifact_path)
        self.entity_db_path = entity_db_path
        self.method = method
        self.node_budget = node_budget
        self.solution_budget = solution_budget
        self.cross_check_with = cross_check_with

    # ------------------------------------------------------------------ #
    def submit(self, rows, *, expected_artifact_revision=None):
        """Validate a batch against the pinned snapshot and recompute the timeline."""
        meta = self.store.meta()
        if meta is None:
            snapshot = load_pinned_snapshot(self.entity_db_path)
            self.store.initialize(
                snapshot, method=self.method, node_budget=self.node_budget,
                solution_budget=self.solution_budget,
                cross_check_with=self.cross_check_with)
            meta = self.store.meta()
        elif expected_artifact_revision is not None:
            current = self.store.db.execute(
                "SELECT COALESCE(MAX(batch_revision),0) FROM relation_batches").fetchone()[0]
            if current != expected_artifact_revision:
                raise RuntimeError(
                    f"artifact revision conflict: expected {expected_artifact_revision}, "
                    f"current {current}")

        snapshot = self._pinned_view(meta)
        existing = self.store.load_assertions()
        try:
            parsed, duplicates = parse_batch(
                rows, snapshot,
                existing={a.assertion_id: a.digest for a in existing})
        except RelationBatchRejected:
            self.store.record_batch(
                input_digest=_input_digest(rows), accepted=False,
                duplicates=[], details={"reason": "batch_rejected"})
            raise

        merged = self._merge_assertions(existing, parsed)
        intervals = solve_timeline(
            merged, meta["entity_order"], method=self.method,
            node_budget=self.node_budget, solution_budget=self.solution_budget,
            cross_check_with=self.cross_check_with)
        digests = {a.assertion_id: a.digest for a in merged}
        for interval in intervals:
            active = [a for a in merged
                      if a.assertion_id in set(interval["active_assertions"])]
            verify_segment(interval, active, meta["entity_order"],
                           assertion_digests=digests)

        self.store.commit_solution(merged, intervals, _assertion_summary(merged))
        revision = self.store.record_batch(
            input_digest=_input_digest(rows), accepted=True, duplicates=duplicates,
            details={"new_assertions": len(parsed), "duplicates": len(duplicates),
                     "intervals": len(intervals)})
        return self.view(artifact_revision=revision)

    def _merge_assertions(self, existing, parsed):
        by_id = {a.assertion_id: a for a in existing}
        for assertion in parsed:
            by_id[assertion.assertion_id] = assertion
        return [by_id[key] for key in sorted(by_id)]

    def _pinned_view(self, meta):
        records = {}
        # Record bodies are only needed for content digests; the pinned digest
        # map already freezes content, so reconstruct a lightweight snapshot.
        return PinnedSnapshot(
            revision=meta["pinned_revision"], member_map=meta["member_map"],
            entity_members=meta["entity_members"],
            record_digests=meta["record_digests"], records=records)

    # ------------------------------------------------------------------ #
    def intervals(self):
        meta = self.store.meta()
        if meta is None:
            return []
        return [self._decorate(interval, meta) for interval in self.store.load_intervals()]

    def at(self, instant):
        meta = self._require_meta()
        instant = instant if hasattr(instant, "isoformat") else parse_instant(instant)
        interval = at_instant(self.store.load_intervals(), meta["entity_order"], instant)
        return self._decorate(interval, meta, queried_at=format_instant(instant))

    def baseline(self):
        meta = self._require_meta()
        return self._decorate(baseline_forest(meta["entity_order"]), meta)

    def view(self, *, artifact_revision=None):
        meta = self._require_meta()
        return {
            "pinned_revision": meta["pinned_revision"],
            "rule_version": meta["rule_version"],
            "solver_config": meta["solver_config"],
            "assertion_summary": meta["assertion_summary"],
            "artifact_revision": artifact_revision if artifact_revision is not None
            else self.store.db.execute(
                "SELECT COALESCE(MAX(batch_revision),0) FROM relation_batches").fetchone()[0],
            "intervals": self.intervals(),
            "batches": self.store.batches(),
        }

    def verify(self):
        """Independently verify every persisted interval."""
        meta = self._require_meta()
        assertions = self.store.load_assertions()
        digests = {a.assertion_id: a.digest for a in assertions}
        reports = []
        for interval in self.store.load_intervals():
            active = [a for a in assertions
                      if a.assertion_id in set(interval["active_assertions"])]
            reports.append(verify_segment(
                interval, active, meta["entity_order"], assertion_digests=digests))
        return {"ok": all(r["ok"] for r in reports), "intervals": reports,
                "rule_version": RULE_VERSION}

    def _decorate(self, interval, meta, *, queried_at=None):
        decorated = dict(interval)
        decorated["pinned_revision"] = meta["pinned_revision"]
        decorated["rule_version"] = meta["rule_version"]
        decorated["solver_config"] = meta["solver_config"]
        decorated["assertion_summary"] = meta["assertion_summary"]
        if queried_at is not None:
            decorated["queried_at"] = queried_at
        return decorated

    def _require_meta(self):
        meta = self.store.meta()
        if meta is None:
            raise RuntimeError("no assertion batch has been pinned to this artifact yet")
        return meta

    def close(self):
        self.store.close()


def _input_digest(rows):
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()
