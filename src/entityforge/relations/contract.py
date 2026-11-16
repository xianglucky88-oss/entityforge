"""Endpoint and time contract for relationship assertions.

Assertions identify their endpoints by *source record keys* (the same
``source + id`` keys the entity pipeline uses).  A fixed entity revision is
pinned when the relationship artifact is created; the snapshot's member map
resolves record keys to entity identities, and that mapping never changes even
if the entity HEAD advances later.

Time ranges are timezone-explicit half-open intervals ``[start, end)``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..service import EntityService

# Bumped whenever solving/verification semantics change in a way that could
# alter outputs; every result carries this version.
RULE_VERSION = "relation-hierarchy-1.0.0"

# Identity of the virtual root vertex used internally and in outputs.
ROOT = "ROOT"


class RelationBatchRejected(ValueError):
    """The whole assertion batch is invalid; nothing is persisted."""


def record_key(source, external_id):
    return EntityService.record_key(source, external_id)


def record_digest(record) -> str:
    """Content fingerprint of a source record at the pinned revision."""
    body = json.dumps(
        {"key": record["key"], "fields": record["fields"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def parse_instant(value) -> datetime:
    """Parse an ISO-8601 instant; a timezone designator is mandatory."""
    if not isinstance(value, str) or not value:
        raise RelationBatchRejected("time bounds must be ISO-8601 strings")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        instant = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RelationBatchRejected(f"unparseable instant {value!r}: {exc}") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise RelationBatchRejected(f"instant {value!r} must carry an explicit timezone")
    return instant.astimezone(timezone.utc)


def format_instant(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat()


def assertion_digest(payload: dict) -> str:
    """Canonical content fingerprint, independent of the assertion id."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class Assertion:
    assertion_id: str
    child_key: str
    parent_key: Optional[str]          # None == explicit root declaration
    score: int
    start: datetime
    end: datetime
    digest: str                        # canonical content hash
    child_entity: Optional[str] = None
    parent_entity: Optional[str] = None  # ROOT marker stored as None

    def interval(self):
        return format_instant(self.start), format_instant(self.end)


@dataclass(frozen=True)
class PinnedSnapshot:
    """Immutable view of an entity revision used for endpoint resolution."""

    revision: int
    member_map: dict                   # record key -> entity id
    entity_members: dict               # entity id -> sorted record keys
    record_digests: dict               # record key -> content digest
    records: dict

    def boundary_instants(self):
        return sorted({b for a in () for b in (a.start, a.end)})


def load_pinned_snapshot(path, revision=None) -> PinnedSnapshot:
    """Open the entity database read-only and pin one revision.

    The read-only URI mode guarantees that relationship processing can never
    mutate the master data and that a concurrent HEAD advance cannot replace
    the member mapping underneath an open artifact.
    """
    import sqlite3

    uri = f"file:{path}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        if revision is None:
            row = db.execute("SELECT revision FROM head").fetchone()
            if row is None:
                raise RelationBatchRejected("entity store has no HEAD revision")
            revision = row[0]
        state_row = db.execute(
            "SELECT state FROM revisions WHERE revision=?", (revision,)).fetchone()
        if state_row is None:
            raise RelationBatchRejected(f"unknown entity revision: {revision}")
        state = json.loads(state_row[0])
    finally:
        db.close()

    records, entities = state["records"], state["entities"]
    member_map, entity_members, digests = {}, {}, {}
    for entity_id, entity in entities.items():
        members = sorted(entity["members"])
        entity_members[entity_id] = members
        for key in members:
            member_map[key] = entity_id
    for key, record in records.items():
        digests[key] = record_digest(record)
    return PinnedSnapshot(revision=revision, member_map=member_map,
                          entity_members=entity_members,
                          record_digests=digests, records=records)


def parse_batch(rows, snapshot: PinnedSnapshot, *, existing=None):
    """Validate and resolve one assertion batch against the pinned snapshot.

    Any single failure rejects the entire batch (``RelationBatchRejected``);
    no partial state is returned.  The assertion id is the unique identity of
    a claim: resubmitting an existing id with the same content is an idempotent
    duplicate and can never add support or score; resubmitting an id with
    different content is a conflicting redefinition and rejects the batch.
    Distinct ids that support the same parent/child edge are independent
    sources; folding keeps the maximum score, so they never inflate it either.

    ``existing`` maps persisted assertion ids to their canonical content digest.
    """
    if not isinstance(rows, list) or not rows:
        raise RelationBatchRejected("assertion batch must be a nonempty list")
    existing = dict(existing or {})
    local_ids = set()
    parsed, duplicates = [], []

    for index, row in enumerate(rows):
        where = f"assertion #{index}"
        if not isinstance(row, dict):
            raise RelationBatchRejected(f"{where}: must be an object")
        assertion_id = row.get("assertion_id")
        if not isinstance(assertion_id, str) or not assertion_id:
            raise RelationBatchRejected(f"{where}: assertion_id must be a nonempty string")
        if assertion_id in local_ids:
            raise RelationBatchRejected(f"{where}: duplicated assertion_id {assertion_id!r}")
        local_ids.add(assertion_id)

        child = row.get("child")
        parent = row.get("parent", "missing")
        if not isinstance(child, dict) or not isinstance(child.get("source"), str) \
                or not isinstance(child.get("id"), str):
            raise RelationBatchRejected(
                f"{where}: child must be a source/id object")
        child_key = record_key(child["source"], child["id"])
        if child_key not in snapshot.member_map:
            raise RelationBatchRejected(
                f"{where}: unknown endpoint record {child_key!r}; batch rejected")
        if child.get("digest") is not None:
            if not isinstance(child["digest"], str) \
                    or child["digest"] != snapshot.record_digests[child_key]:
                raise RelationBatchRejected(
                    f"{where}: same record id with different content at pinned "
                    f"revision {snapshot.revision}; batch rejected")

        if parent is None:
            parent_key = None
        elif parent == "missing":
            raise RelationBatchRejected(
                f"{where}: parent is required (use null for an explicit root)")
        elif isinstance(parent, dict) and isinstance(parent.get("source"), str) \
                and isinstance(parent.get("id"), str):
            parent_key = record_key(parent["source"], parent["id"])
            if parent_key not in snapshot.member_map:
                raise RelationBatchRejected(
                    f"{where}: unknown endpoint record {parent_key!r}; batch rejected")
            if parent.get("digest") is not None and (
                    not isinstance(parent["digest"], str)
                    or parent["digest"] != snapshot.record_digests[parent_key]):
                raise RelationBatchRejected(
                    f"{where}: same record id with different content at pinned "
                    f"revision {snapshot.revision}; batch rejected")
        else:
            raise RelationBatchRejected(
                f"{where}: parent must be a source-id object or null")

        score = row.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or score < 0:
            raise RelationBatchRejected(f"{where}: score must be a nonnegative integer")

        start, end = parse_instant(row.get("start")), parse_instant(row.get("end"))
        if not start < end:
            raise RelationBatchRejected(
                f"{where}: half-open interval requires start < end")

        canonical = {
            "child": child_key,
            "parent": parent_key,
            "score": score,
            "start": format_instant(start),
            "end": format_instant(end),
        }
        digest = assertion_digest(canonical)
        if assertion_id in existing:
            if existing[assertion_id] != digest:
                raise RelationBatchRejected(
                    f"{where}: assertion_id {assertion_id!r} already exists with "
                    "different content; redefining an assertion id rejects the batch")
            duplicates.append(assertion_id)
            continue

        child_entity = snapshot.member_map[child_key]
        parent_entity = None if parent_key is None else snapshot.member_map[parent_key]
        parsed.append(Assertion(
            assertion_id=assertion_id, child_key=child_key, parent_key=parent_key,
            score=score, start=start, end=end, digest=digest,
            child_entity=child_entity, parent_entity=parent_entity))

    # A batch made entirely of duplicates is a valid idempotent no-op; an
    # empty request is a client error and is rejected above.
    return parsed, duplicates
