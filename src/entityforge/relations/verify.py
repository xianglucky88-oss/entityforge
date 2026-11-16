"""Independent verification of relationship results.

The verifier never trusts the solver: it rebuilds every folded edge from the
segment model and re-checks the one-parent constraint, acyclicity, the score
sum, support integrity and the reason attached to every selected or excluded
edge.  An independent solver (exhaustive enumeration for small graphs, the
other production solver otherwise) then confirms optimality and the stable
tie break.  Excluded edges are only ever given factual reasons; they are not
declared to be wrong relationships.
"""
from __future__ import annotations

from .contract import ROOT, RULE_VERSION
from .solver import (
    branch_and_bound,
    edmonds_contract,
    enumerate_forests,
    fold_support,
)

ORACLE_LIMIT = 8
ALLOWED_REASONS = {None, "lower_score", "tie_lost", "acyclicity_constraint",
                   "self_loop_excluded"}


class VerificationError(AssertionError):
    """A relationship result failed independent verification."""


def verify_segment(result, assertions, entity_order, *, assertion_digests=None):
    """Recompute and fully validate one solved segment.

    ``assertions`` are the assertions active during the segment; the segment
    model is rebuilt from scratch so the check is independent of solver state.
    Returns a structured report on success.
    """
    problems = []
    model = fold_support(assertions, entity_order)
    n = len(entity_order)

    if result.get("entity_order") != entity_order:
        problems.append("entity_order does not match the pinned snapshot")
    rows = result.get("evidence", [])
    folded = {(e.child_idx, e.parent_idx): e for e in model.edges}

    selected_rows = [r for r in rows if r.get("status") == "selected"]
    if sorted(r["child_entity"] for r in selected_rows) != sorted(entity_order):
        problems.append("one-parent constraint violated: each entity needs exactly one parent edge")

    chosen = {}
    # First pass: validate selected edges so exclusion reasons have a baseline.
    for row in rows:
        if row.get("status") != "selected":
            continue
        child_i = _entity_index(entity_order, row.get("child_entity"))
        parent_i = n if row.get("parent_entity") == ROOT else _entity_index(
            entity_order, row.get("parent_entity"))
        if child_i is None or parent_i is None:
            problems.append(f"evidence row references unknown entity: {row}")
            continue
        edge = folded.get((child_i, parent_i))
        if edge is None:
            problems.append(f"evidence row has no folded edge: {row['child_entity']} -> {row['parent_entity']}")
            continue
        if row.get("score") != edge.score:
            problems.append(f"score mismatch on {row['child_entity']} -> {row['parent_entity']}")
        chosen[child_i] = edge
        _check_support(edge, row, problems, assertion_digests)

    # Second pass: every excluded edge needs a factual reason.
    for row in rows:
        if row.get("status") == "selected":
            continue
        child_i = _entity_index(entity_order, row.get("child_entity"))
        parent_i = n if row.get("parent_entity") == ROOT else _entity_index(
            entity_order, row.get("parent_entity"))
        if child_i is None or parent_i is None:
            problems.append(f"evidence row references unknown entity: {row}")
            continue
        edge = folded.get((child_i, parent_i))
        if edge is None:
            problems.append(f"evidence row has no folded edge: {row['child_entity']} -> {row['parent_entity']}")
            continue
        if row.get("reason") not in ALLOWED_REASONS:
            problems.append(f"unsupported exclusion reason: {row.get('reason')!r}")
        _check_reason(model, edge, row, chosen, problems)

    _check_acyclic(chosen, n, model.root_idx, problems)
    recomputed_score = sum(e.score for e in chosen.values())
    if recomputed_score != result.get("total_score"):
        problems.append(
            f"score sum {recomputed_score} != reported total_score {result.get('total_score')}")
    if len(chosen) == n:
        rebuilt_sequence = [
            ROOT if chosen[i].parent_idx == model.root_idx
            else entity_order[chosen[i].parent_idx] for i in range(n)]
        if result.get("parent_sequence") != rebuilt_sequence:
            problems.append("parent_sequence does not match selected edges")
    else:
        problems.append("one-parent constraint violated: missing selected edge(s)")

    _check_self_loops(model, result, problems)
    _check_optimality(model, result, problems)

    if problems:
        raise VerificationError("; ".join(problems))
    return {
        "ok": True,
        "entities": n,
        "selected_edges": len(chosen),
        "total_score": recomputed_score,
        "rule_version": result.get("solver", {}).get("rule_version", RULE_VERSION),
        "checks": ["one_parent", "acyclic", "score_sum", "support",
                   "exclusion_reasons", "independent_optimality", "self_loops"],
    }


def _entity_index(entity_order, entity_id):
    if entity_id is None or entity_id not in _index_cache(entity_order):
        return None
    return _index_cache(entity_order)[entity_id]


def _index_cache(entity_order):
    return {entity_id: i for i, entity_id in enumerate(entity_order)}


def _check_support(edge, row, problems, assertion_digests):
    if edge.implicit_root:
        if edge.score != 0 or row.get("support"):
            problems.append(f"implicit root edge for {edge.child_idx} must be zero-score without support")
        return
    if not row.get("support"):
        problems.append(f"selected edge of {row.get('child_entity')} lacks supporting assertions")
        return
    scores = {s["score"] for s in row["support"]}
    if max(scores) != edge.score:
        problems.append(f"support max score does not back edge score for {row.get('child_entity')}")
    if assertion_digests:
        for support in row["support"]:
            expected = assertion_digests.get(support["assertion_id"])
            if expected is not None and support.get("digest") != expected:
                problems.append(
                    f"support digest mismatch for {support['assertion_id']}")


def _check_reason(model, edge, row, chosen, problems):
    child_i = edge.child_idx
    chosen_edge = chosen.get(child_i)
    if chosen_edge is None:
        problems.append(
            f"excluded edge {row['child_entity']} has no selected parent to compare with")
        return
    reason = row.get("reason")
    closes_cycle = (edge.parent_idx != model.root_idx
                    and _would_close_cycle(model, edge, chosen))
    if reason == "acyclicity_constraint":
        if not closes_cycle:
            problems.append(
                f"acyclicity_constraint reason unsupported for {row['child_entity']}")
        return
    if closes_cycle:
        problems.append(
            f"cycle-closing edge for {row['child_entity']} must be labeled "
            "acyclicity_constraint")
        return
    if reason == "lower_score" and not edge.score < chosen_edge.score:
        problems.append(f"mislabeled lower_score edge for {row['child_entity']}")
    if reason == "tie_lost" and edge.score != chosen_edge.score:
        problems.append(f"mislabeled tie_lost edge for {row['child_entity']}")
    if reason is None:
        problems.append(
            f"excluded edge {row['child_entity']} -> {row['parent_entity']} needs a reason")


def _would_close_cycle(model, candidate, chosen):
    """True when following selected parents from the candidate parent reaches its child."""
    v, seen = candidate.parent_idx, set()
    while v in chosen:
        edge = chosen[v]
        if edge.parent_idx == candidate.child_idx:
            return True
        if edge.parent_idx == model.root_idx or edge.parent_idx in seen:
            return False
        seen.add(edge.parent_idx)
        v = edge.parent_idx
    return False


def _check_acyclic(chosen, n, root, problems):
    for start in range(n):
        v, seen = start, set()
        while v in chosen:
            edge = chosen[v]
            if edge.parent_idx in seen:
                problems.append(f"cycle reachable from entity index {start}")
                break
            seen.add(v)
            v = edge.parent_idx
        else:
            if v != root:
                problems.append(f"entity index {start} does not terminate at the virtual root")


def _check_self_loops(model, result, problems):
    reported = {(s["child_key"], tuple(a["assertion_id"] for a in s["assertions"]))
                for s in result.get("self_loops", [])}
    rebuilt = {(s["child_key"], tuple(a["assertion_id"] for a in s["assertions"]))
               for s in model.self_loops}
    if reported != rebuilt:
        problems.append("self-loop exclusion list does not match folded model")
    for row in result.get("self_loops", []):
        if row.get("reason") != "self_loop_excluded":
            problems.append("self-loop row must carry reason self_loop_excluded")


def _check_optimality(model, result, problems):
    reported_method = result.get("solver", {}).get("method")
    if len(model.entity_order) <= ORACLE_LIMIT:
        selected, score, parents = enumerate_forests(model)
        if score != result["total_score"]:
            problems.append(
                f"oracle optimum {score} != reported {result['total_score']}")
        oracle_sequence = [ROOT if p == model.root_idx else model.entity_order[p]
                           for p in parents]
        if oracle_sequence != result.get("parent_sequence"):
            problems.append("stable tie break disagrees with exhaustive oracle")
        return
    # Beyond the oracle limit, cross-check with the other exact solver.
    alternative = (branch_and_bound(model) if reported_method == "cycle_contraction_edmonds"
                   else edmonds_contract(model))
    alternative_score = sum(model.edges[eid].score for eid in alternative)
    if alternative_score != result["total_score"]:
        problems.append(
            f"independent solver score {alternative_score} != reported {result['total_score']}")
