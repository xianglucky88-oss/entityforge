import hashlib
import json
from collections import defaultdict
from .matching import EvidenceMatcher, Normalizer, PrioritySurvivor
from .storage import SQLiteStore


class EntityService:
    def __init__(self, path, *, normalizer=None, matcher=None, survivor=None):
        self.store = SQLiteStore(path)
        self.normalizer = normalizer or Normalizer()
        self.matcher = matcher or EvidenceMatcher()
        self.survivor = survivor or PrioritySurvivor()

    @staticmethod
    def record_key(source, external_id):
        return json.dumps([source, external_id], ensure_ascii=False, separators=(",", ":"))

    def ingest(self, rows, expected=None):
        validated = []
        for row in rows:
            source, external = row.get("source"), row.get("id")
            fields = row.get("fields")
            if not isinstance(source, str) or not source or not isinstance(external, str) or not external:
                raise ValueError("source and id must be nonempty strings")
            if not isinstance(fields, dict) or not fields or any(
                    not isinstance(k, str) or (v is not None and not isinstance(v, str))
                    for k, v in fields.items()):
                raise ValueError("fields must map strings to strings or null")
            validated.append({"key": self.record_key(source, external), "source": source,
                              "id": external, "fields": fields,
                              "normalized": self.normalizer.normalize(fields)})
        with self.store.edit("ingest", expected, {"count": len(validated)}) as state:
            for record in validated:
                state["records"][record["key"]] = record
            # Ingestion invalidates derived memberships; no stale master is served.
            state["entities"], state["evidence"] = {}, []
        return self.store.read()[0]

    def _entity(self, members, records):
        fields, origins = self.survivor.select([records[key] for key in sorted(members)])
        entity_id = hashlib.sha256(json.dumps(sorted(members)).encode()).hexdigest()[:24]
        return entity_id, {"members": sorted(members), "fields": fields, "origins": origins}

    def resolve(self, expected=None):
        with self.store.edit("resolve", expected) as state:
            records = state["records"]
            blocks = defaultdict(set)
            for key, row in sorted(records.items()):
                n = row["normalized"]
                # Name/city records need a candidate block even if they have no
                # durable identifier. Blocking by city alone preserves recall;
                # the matcher still requires a strong name similarity.
                for field in ("email", "phone", "city"):
                    if n.get(field):
                        blocks[(field, n[field])].add(key)
            pairs = set()
            for keys in blocks.values():
                ordered = sorted(keys)
                pairs.update((a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:])
            groups = {key: {key} for key in records}
            owner = {key: key for key in records}
            evidence = []
            for a, b in sorted(pairs):
                result = self.matcher.compare(records[a]["normalized"], records[b]["normalized"])
                ga, gb = owner[a], owner[b]
                if result["matched"] and ga != gb:
                    # Complete-link compatibility prevents A~B~C from joining A and C
                    # when their identifiers contradict one another.
                    conflicts = any(self.matcher.compare(records[x]["normalized"], records[y]["normalized"])
                                    .get("conflicts") for x in groups[ga] for y in groups[gb])
                    if conflicts:
                        result["matched"], result["blocked_transitive"] = False, True
                    else:
                        groups[ga].update(groups.pop(gb))
                        for key in groups[ga]:
                            owner[key] = ga
                evidence.append({"left": a, "right": b, **result})
            state["entities"] = dict(self._entity(members, records) for members in groups.values())
            state["evidence"] = evidence
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
        return new_id

    def split(self, entity_id, partitions, expected=None):
        with self.store.edit("manual_split", expected, {"entity": entity_id}) as state:
            original = state["entities"][entity_id]["members"]
            flattened = [key for part in partitions for key in part]
            if len(partitions) < 2 or any(not p for p in partitions) or sorted(flattened) != sorted(original):
                raise ValueError("partitions must cover every member exactly once")
            del state["entities"][entity_id]
            state["entities"].update(self._entity(p, state["records"]) for p in partitions)
        return self.snapshot()

    def rollback(self, revision, expected=None):
        _, previous = self.store.read(revision)
        with self.store.edit("rollback", expected, {"target": revision}) as state:
            state.clear()
            state.update(previous)
        return self.snapshot()

    def snapshot(self, revision=None):
        rev, state = self.store.read(revision)
        return {"revision": rev, **state}

    def close(self):
        self.store.close()
