import hashlib
import json
from collections import defaultdict
from .matching import EvidenceMatcher, Normalizer, PrioritySurvivor
from .storage import BatchConflict, RevisionConflict, SQLiteStore

# Bump when the clustering/blocking algorithm itself changes; part of the rules fingerprint.
RESOLUTION_SCHEMA = 1


class _BatchReplay(Exception):
    """Control flow inside edit(): the batch id was already committed."""


class _CountingMatcher:
    """Counts matcher invocations so batch receipts can prove localized recomputation."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def compare(self, left, right):
        self.calls += 1
        return self.inner.compare(left, right)


class EntityService:
    def __init__(self, path, *, normalizer=None, matcher=None, survivor=None):
        self.store = SQLiteStore(path)
        self.normalizer = normalizer or Normalizer()
        self.matcher = matcher or EvidenceMatcher()
        self.survivor = survivor or PrioritySurvivor()

    @staticmethod
    def record_key(source, external_id):
        return json.dumps([source, external_id], ensure_ascii=False, separators=(",", ":"))

    def _rules_fingerprint(self):
        """Fingerprint of the resolution rules that derived entities must agree with.

        The normalizer is deliberately excluded: normalized forms are frozen into
        each record at ingest time, so resolution only depends on the matcher,
        the survivorship policy and the algorithm schema."""
        def params(component):
            try:
                return json.dumps(vars(component), sort_keys=True, default=repr)
            except (TypeError, ValueError):
                return repr(component)
        payload = json.dumps({"schema": RESOLUTION_SCHEMA,
                              "matcher": params(self.matcher),
                              "survivor": params(self.survivor)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _block_keys(normalized):
        # Name/city records need a candidate block even if they have no durable
        # identifier. Blocking by city alone preserves recall; the matcher still
        # requires a strong name similarity. JSON keys avoid separator collisions.
        return tuple(json.dumps([field, normalized[field]], ensure_ascii=False)
                     for field in ("email", "phone", "city") if normalized.get(field))

    def _index_rows(self, records):
        return {key: {"normalized": record["normalized"],
                      "blocks": list(self._block_keys(record["normalized"]))}
                for key, record in records.items()}

    def _cluster(self, keys, records, matcher):
        """Deterministic complete-link clustering over the given record keys.

        Used by full resolve and by the incremental path on its invalidation
        closure, so both follow exactly the same pair order and conflict rules."""
        keys = sorted(keys)
        blocks = defaultdict(set)
        for key in keys:
            for block in self._block_keys(records[key]["normalized"]):
                blocks[block].add(key)
        pairs = set()
        for members in blocks.values():
            ordered = sorted(members)
            pairs.update((a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:])
        groups = {key: {key} for key in keys}
        owner = {key: key for key in keys}
        evidence = []
        for a, b in sorted(pairs):
            result = matcher.compare(records[a]["normalized"], records[b]["normalized"])
            ga, gb = owner[a], owner[b]
            if result["matched"] and ga != gb:
                # Complete-link compatibility prevents A~B~C from joining A and C
                # when their identifiers contradict one another.
                conflicts = any(matcher.compare(records[x]["normalized"], records[y]["normalized"])
                                .get("conflicts") for x in groups[ga] for y in groups[gb])
                if conflicts:
                    result["matched"], result["blocked_transitive"] = False, True
                else:
                    groups[ga].update(groups.pop(gb))
                    for key in groups[ga]:
                        owner[key] = ga
            evidence.append({"left": a, "right": b, **result})
        return list(groups.values()), evidence

    @staticmethod
    def _validate_fields(fields):
        return isinstance(fields, dict) and fields and all(
            isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in fields.items())

    def ingest(self, rows, expected=None):
        validated = []
        for row in rows:
            source, external = row.get("source"), row.get("id")
            fields = row.get("fields")
            if not isinstance(source, str) or not source or not isinstance(external, str) or not external:
                raise ValueError("source and id must be nonempty strings")
            if not self._validate_fields(fields):
                raise ValueError("fields must map strings to strings or null")
            validated.append({"key": self.record_key(source, external), "source": source,
                              "id": external, "fields": fields,
                              "normalized": self.normalizer.normalize(fields)})
        with self.store.edit("ingest", expected, {"count": len(validated)}) as state:
            for record in validated:
                state["records"][record["key"]] = record
            # Ingestion invalidates derived memberships; no stale master is served.
            state["entities"], state["evidence"] = {}, []
            state["meta"] = {"resolved": not state["records"], "rules": None}
            self.store.update_index(
                self._index_rows({r["key"]: r for r in validated}), [],
                self.store.current_revision() + 1)
        return self.store.read()[0]

    def _canonical_operations(self, operations):
        """Validate every operation before any write; collapse to last-op-wins per record.

        A late invalid record aborts the whole batch before the transaction starts,
        so a failed batch never persists partial entities and never burns its id."""
        if not isinstance(operations, (list, tuple)):
            raise ValueError("operations must be a list")
        effective = {}
        for raw in operations:
            if not isinstance(raw, dict):
                raise ValueError("each operation must be an object")
            op = raw.get("op")
            source, external = raw.get("source"), raw.get("id")
            if not isinstance(source, str) or not source or not isinstance(external, str) or not external:
                raise ValueError("source and id must be nonempty strings")
            key = self.record_key(source, external)
            if op == "upsert":
                fields = raw.get("fields")
                if not self._validate_fields(fields):
                    raise ValueError("fields must map strings to strings or null")
                effective[key] = ("upsert", {"key": key, "source": source, "id": external,
                                             "fields": fields,
                                             "normalized": self.normalizer.normalize(fields)})
            elif op == "delete":
                effective[key] = ("delete", None)
            else:
                raise ValueError(f"unknown operation: {op!r}")
        return effective

    def apply_batch(self, batch_id, operations, expected=None):
        """Idempotent batch of upserts/deletes resolved in the same revision.

        A committed batch id replays its original receipt without touching audit
        or HEAD (recognized before the expected check, so a stale expected on a
        retry is tolerated); the same id with different content is rejected."""
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("batch_id must be a nonempty string")
        effective = self._canonical_operations(operations)
        content = {key: [op, record["fields"] if record else None]
                   for key, (op, record) in sorted(effective.items())}
        content_hash = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        details = {"batch_id": batch_id}
        try:
            with self.store.edit("batch", None, details) as state:
                existing = self.store.get_batch(batch_id)
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise BatchConflict(
                            f"batch {batch_id!r} already committed with different content")
                    raise _BatchReplay(existing["receipt"])
                revision = self.store.current_revision()
                if expected is not None and revision != expected:
                    raise RevisionConflict(f"expected {expected}, current {revision}")
                receipt = self._commit_batch(state, effective, revision, batch_id,
                                             content_hash, details)
        except _BatchReplay as replay:
            return dict(replay.args[0], status="replayed")
        return receipt

    def _commit_batch(self, state, effective, revision, batch_id, content_hash, details):
        records = state["records"]
        new_revision = revision + 1
        old_index = self.store.index_rows()
        index_stale = (self.store.index_revision() != revision
                       or set(old_index) != set(records))
        if index_stale:
            # The index is a pure function of HEAD records, so rebuilding it is
            # always safe and never by itself forces a full re-resolution.
            old_index = self._index_rows(records)
        meta = state.get("meta") or {}
        fingerprint = self._rules_fingerprint()
        if meta.get("resolved"):
            fallback = bool(records or state["entities"]) and meta.get("rules") != fingerprint
            reason = "rules_changed" if fallback else None
        else:
            # Entities were cleared by ingest or deviate via manual merge/split:
            # no trustworthy clustering baseline, so recompute everything.
            fallback, reason = True, "entities_not_resolved"
        changed, upserted, deleted = [], 0, 0
        for key in sorted(effective):
            op, record = effective[key]
            if op == "delete":
                deleted += 1
                if key in records:
                    changed.append(key)
                    del records[key]
            else:
                upserted += 1
                if records.get(key) != record:
                    changed.append(key)
                records[key] = record
        counter = _CountingMatcher(self.matcher)
        if fallback:
            groups, evidence = self._cluster(sorted(records), records, counter)
            entities = dict(self._entity(members, records) for members in groups)
            affected = len(records)
        else:
            entities, evidence, affected = self._incremental(state, changed, old_index, counter)
        state["entities"], state["evidence"] = entities, evidence
        state["meta"] = {"resolved": True, "rules": fingerprint}
        if fallback or index_stale:
            self.store.replace_index(self._index_rows(records), new_revision)
        else:
            present = {key: records[key] for key in changed if key in records}
            self.store.update_index(self._index_rows(present),
                                    [key for key in changed if key not in records],
                                    new_revision)
        receipt = {"batch_id": batch_id, "revision": new_revision, "status": "committed",
                   "fallback": fallback, "fallback_reason": reason,
                   "upserted": upserted, "deleted": deleted,
                   "changed_records": len(changed), "affected_records": affected,
                   "matcher_calls": counter.calls, "entity_count": len(entities),
                   "content_hash": content_hash, "at": self.store.now()}
        self.store.save_batch(batch_id, new_revision, content_hash, receipt)
        details.update({"fallback": fallback, "fallback_reason": reason,
                        "changed_records": len(changed)})
        return receipt

    def _incremental(self, state, changed, old_index, matcher):
        """Re-resolve only the invalidation closure of the changed records.

        The closure grows from the old and new blocks of changed records to the
        whole components and whole blocks they touch, so no candidate block,
        HEAD component or evidence pair crosses its boundary. Records outside
        keep their entities and evidence untouched and the matcher is never
        invoked for their pairs; inside, the shared deterministic _cluster runs.
        """
        records = state["records"]
        old_blocks = {key: tuple(row["blocks"]) for key, row in old_index.items()}
        new_blocks = {key: self._block_keys(record["normalized"])
                      for key, record in records.items()}
        old_members, new_members = defaultdict(set), defaultdict(set)
        for key, blocks in old_blocks.items():
            for block in blocks:
                old_members[block].add(key)
        for key, blocks in new_blocks.items():
            for block in blocks:
                new_members[block].add(key)
        entity_members = {eid: set(entity["members"]) for eid, entity in state["entities"].items()}
        member_entity = {member: eid for eid, members in entity_members.items() for member in members}
        # Deletion, contact or alias changes move records between blocks: seed
        # with both the old and the new blocks of every changed record.
        affected = set()
        for key in changed:
            affected.update(old_blocks.get(key, ()))
            affected.update(new_blocks.get(key, ()))
        components = set()
        while True:
            in_blocks = set()
            for block in affected:
                in_blocks.update(old_members.get(block, ()))
                in_blocks.update(new_members.get(block, ()))
            found = {member_entity[key] for key in in_blocks if key in member_entity}
            scope = {key for key in in_blocks if key in records}
            for eid in found:
                scope.update(entity_members[eid])
            scope.update(key for key in changed if key in records)
            grown = set(affected)
            for key in scope:
                grown.update(old_blocks.get(key, ()))
                grown.update(new_blocks.get(key, ()))
            if grown == affected and found == components:
                break
            affected, components = grown, found
        # Deleted records still appear in HEAD entity members; they seeded the
        # closure through their old blocks but take no part in re-clustering.
        scope = {key for key in scope if key in records}
        groups, scope_evidence = self._cluster(scope, records, matcher)
        entities = {eid: entity for eid, entity in state["entities"].items()
                    if eid not in components}
        for members in groups:
            entity_id, entity = self._entity(members, records)
            entities[entity_id] = entity
        # Evidence for pairs fully outside the closure is still exact: their
        # records, blocks and surrounding components are unchanged. Pairs that
        # lost a member (deletes) or touch the closure are recomputed above.
        outside = [entry for entry in state["evidence"]
                   if entry["left"] in records and entry["right"] in records
                   and entry["left"] not in scope and entry["right"] not in scope]
        evidence = sorted(outside + scope_evidence,
                          key=lambda entry: (entry["left"], entry["right"]))
        return entities, evidence, len(scope)

    def _entity(self, members, records):
        fields, origins = self.survivor.select([records[key] for key in sorted(members)])
        entity_id = hashlib.sha256(json.dumps(sorted(members)).encode()).hexdigest()[:24]
        return entity_id, {"members": sorted(members), "fields": fields, "origins": origins}

    def resolve(self, expected=None):
        with self.store.edit("resolve", expected) as state:
            records = state["records"]
            counter = _CountingMatcher(self.matcher)
            groups, evidence = self._cluster(sorted(records), records, counter)
            state["entities"] = dict(self._entity(members, records) for members in groups)
            state["evidence"] = evidence
            state["meta"] = {"resolved": True, "rules": self._rules_fingerprint()}
            self.store.replace_index(self._index_rows(records),
                                     self.store.current_revision() + 1)
        return self.snapshot()

    def merge(self, entity_ids, expected=None):
        if len(set(entity_ids)) < 2:
            raise ValueError("merge needs at least two distinct entities")
        with self.store.edit("manual_merge", expected, {"entities": entity_ids}) as state:
            members = set()
            for entity_id in set(entity_ids):
                members.update(state["entities"].pop(entity_id)["members"])
            new_id, entity = self._entity(members, state["records"])
            state["entities"][new_id] = entity
            state["meta"] = {**state.get("meta", {}), "resolved": False}
            self.store.set_index_revision(self.store.current_revision() + 1)
        return new_id

    def split(self, entity_id, partitions, expected=None):
        with self.store.edit("manual_split", expected, {"entity": entity_id}) as state:
            original = state["entities"][entity_id]["members"]
            flattened = [key for part in partitions for key in part]
            if len(partitions) < 2 or any(not p for p in partitions) or sorted(flattened) != sorted(original):
                raise ValueError("partitions must cover every member exactly once")
            del state["entities"][entity_id]
            state["entities"].update(self._entity(p, state["records"]) for p in partitions)
            state["meta"] = {**state.get("meta", {}), "resolved": False}
            self.store.set_index_revision(self.store.current_revision() + 1)
        return self.snapshot()

    def rollback(self, revision, expected=None):
        _, previous = self.store.read(revision)
        with self.store.edit("rollback", expected, {"target": revision}) as state:
            state.clear()
            state.update(previous)
            # The index mirrors HEAD records, so rebuild it for the restored state.
            self.store.replace_index(self._index_rows(state["records"]),
                                     self.store.current_revision() + 1)
        return self.snapshot()

    def snapshot(self, revision=None):
        rev, state = self.store.read(revision)
        return {"revision": rev, **state}

    def close(self):
        self.store.close()
