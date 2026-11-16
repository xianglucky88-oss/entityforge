"""Replaceable normalization, matching and survivorship policies."""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Protocol


def text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


class Matcher(Protocol):
    def compare(self, left: dict, right: dict) -> dict: ...


class Normalizer:
    def __init__(self, aliases=None):
        self.aliases = {text(k): text(v) for k, v in (aliases or {}).items()}

    def normalize(self, fields):
        normalized = {key: text(value) for key, value in fields.items()}
        for field in ("name", "city"):
            value = normalized.get(field, "")
            seen = set()
            while value in self.aliases:
                if value in seen:
                    raise ValueError("alias cycle")
                seen.add(value)
                value = self.aliases[value]
            normalized[field] = value
        normalized["phone"] = re.sub(r"\D", "", normalized.get("phone", ""))
        return normalized


class EvidenceMatcher:
    def __init__(self, name_threshold=0.88):
        if not 0 <= name_threshold <= 1:
            raise ValueError("name threshold must be between zero and one")
        self.threshold = name_threshold

    def compare(self, left, right):
        exact = [f for f in ("email", "phone") if left.get(f) and left.get(f) == right.get(f)]
        # Contradicting durable identifiers block weak and transitive name matches.
        conflicts = [f for f in ("email", "phone")
                     if left.get(f) and right.get(f) and left[f] != right[f]]
        similarity = SequenceMatcher(None, left.get("name", ""), right.get("name", "")).ratio()
        fuzzy = bool(left.get("name") and right.get("name") and left.get("city") and
                     left.get("city") == right.get("city") and similarity >= self.threshold)
        matched = bool(exact or fuzzy) and not conflicts
        return {"matched": matched, "exact": exact, "conflicts": conflicts,
                "name_similarity": round(similarity, 6), "rule": "identity" if exact else "name_city"}


class PrioritySurvivor:
    def __init__(self, source_priority=None):
        self.priorities = source_priority or {}

    def select(self, records):
        ordered = sorted(records, key=lambda r: (self.priorities.get(r["source"], 100), r["key"]))
        fields, origins = {}, {}
        for record in ordered:
            for field, value in record["fields"].items():
                if value not in ("", None) and field not in fields:
                    fields[field], origins[field] = value, record["key"]
        return fields, origins
