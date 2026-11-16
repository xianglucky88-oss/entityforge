"""Half-open interval timeline for relationship solving.

Solving is re-triggered only at assertion start/end boundaries.  At a single
instant where one assertion ends and another starts, half-open semantics make
the ending assertion inactive and the starting assertion active.  Adjacent
segments whose selected forest is identical may be merged for presentation,
but every constituent segment and its supporting evidence are retained.
"""
from __future__ import annotations

from .contract import ROOT, format_instant
from .solver import fold_support, solve_segment


def boundary_segments(assertions):
    """Split time into [b_i, b_{i+1}) segments with fixed active assertion sets."""
    if not assertions:
        return []
    boundaries = sorted({instant for a in assertions
                         for instant in (a.start, a.end)})
    segments = []
    for start, end in zip(boundaries, boundaries[1:]):
        active = [a for a in assertions if a.start <= start and a.end > start]
        segments.append((start, end, active))
    return [seg for seg in segments if seg[2]]


def solve_timeline(assertions, entity_order, *, method="edmonds",
                   node_budget=1_000_000, solution_budget=1_000_000,
                   cross_check_with=None):
    """Solve every boundary segment and merge equal adjacent forests."""
    solved = []
    for start, end, active in boundary_segments(assertions):
        model = fold_support(active, entity_order)
        result = solve_segment(
            model, method=method, node_budget=node_budget,
            solution_budget=solution_budget, cross_check_with=cross_check_with)
        result["start"], result["end"] = format_instant(start), format_instant(end)
        result["active_assertions"] = sorted(a.assertion_id for a in active)
        solved.append(result)

    merged = []
    for segment in solved:
        if merged and _same_forest(merged[-1], segment):
            target = merged[-1]
            target["end"] = segment["end"]
            target["constituents"].append(_constituent(segment))
        else:
            segment["constituents"] = [_constituent(segment)]
            merged.append(segment)
    for segment in merged:
        segment["merged_segments"] = len(segment["constituents"])
    return merged


def _same_forest(left, right):
    return left["selected"] == right["selected"] and \
        left["total_score"] == right["total_score"]


def _constituent(segment):
    """Preserve evidence exactly as computed, even when intervals merge."""
    return {"start": segment["start"], "end": segment["end"],
            "active_assertions": list(segment["active_assertions"]),
            "evidence": segment["evidence"],
            "self_loops": segment["self_loops"]}


def baseline_forest(entity_order):
    """All-root fallback used where no assertion is in force."""
    return {
        "start": None, "end": None,
        "selected": {entity_id: ROOT for entity_id in entity_order},
        "total_score": 0,
        "parent_sequence": [ROOT for _ in entity_order],
        "entity_order": list(entity_order),
        "evidence": [
            {"child_entity": entity_id, "parent_entity": ROOT, "score": 0,
             "implicit_root": True, "support": [],
             "status": "selected", "reason": None}
            for entity_id in entity_order],
        "self_loops": [],
        "solver": {"method": "baseline_no_active_assertions",
                   "optimality": "trivial", "rule_version": None, "budget": None},
        "covered": False,
        "merged_segments": 0,
        "constituents": [],
    }


def at_instant(intervals, entity_order, instant):
    """Point query with half-open [start, end) semantics.

    When adjacent segments were merged, the query is resolved against the
    exact constituent segment covering the instant so its active-assertion
    set and evidence remain truthful even though the forest is unchanged.
    """
    parsed = instant if hasattr(instant, "isoformat") else \
        _parse_query_instant(instant)
    for interval in intervals:
        start = _parse_query_instant(interval["start"])
        end = _parse_query_instant(interval["end"])
        if start <= parsed < end:
            for constituent in interval.get("constituents", []):
                c_start = _parse_query_instant(constituent["start"])
                c_end = _parse_query_instant(constituent["end"])
                if c_start <= parsed < c_end:
                    resolved = dict(interval)
                    resolved.update({
                        "active_assertions": list(constituent["active_assertions"]),
                        "evidence": constituent["evidence"],
                        "self_loops": constituent["self_loops"],
                        "segment_start": constituent["start"],
                        "segment_end": constituent["end"],
                    })
                    resolved["queried_at"] = format_instant(parsed)
                    resolved["covered"] = True
                    return resolved
            result = dict(interval)
            result["queried_at"] = format_instant(parsed)
            result["covered"] = True
            return result
    result = baseline_forest(entity_order)
    result["queried_at"] = format_instant(parsed)
    return result


def _parse_query_instant(value):
    from .contract import parse_instant
    return parse_instant(value)


def merge_intervals(intervals):
    """Public helper: re-merge an already solved interval list if needed."""
    merged = []
    for interval in intervals:
        if merged and _same_forest(merged[-1], interval):
            merged[-1]["end"] = interval["end"]
            merged[-1]["constituents"].extend(interval.get("constituents", []))
            merged[-1]["merged_segments"] = len(merged[-1]["constituents"])
        else:
            merged.append(dict(interval))
    return merged
