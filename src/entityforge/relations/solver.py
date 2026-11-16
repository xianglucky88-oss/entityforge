"""Exact maximum-weight rooted-forest solvers.

Per time segment the problem is: choose at most one incoming parent edge for
every entity so that the result is acyclic and the total preference score is
maximized, with a lexicographic tie break on parent identities ordered by
fixed entity identity.  This is a maximum-weight spanning arborescence
(branching) problem with a virtual root that offers a zero-score fallback to
every entity, so a feasible forest always exists.

Two solvers are provided and cross-checked:

* ``edmonds_contract`` — exact cycle-contraction (Chu–Liu/Edmonds) with the
  tie break embedded in a composite integer weight, so no heuristic edge
  deletion is ever used;
* ``branch_and_bound`` — constraint search with explicit node/solution
  budgets.  Exhausting a budget raises ``BudgetExhausted``; a partial search
  result is never reported as optimal.

``enumerate_forests`` enumerates every legal forest on small graphs and is
used by the test suite as an independent oracle.
"""
from __future__ import annotations

from dataclasses import dataclass

from .contract import ROOT, RULE_VERSION


class BudgetExhausted(RuntimeError):
    """The bounded search stopped before proving optimality."""

    def __init__(self, *, nodes, node_budget, solutions, solution_budget, **extra):
        super().__init__(
            f"search budget exhausted after {nodes} nodes / {solutions} solutions; "
            "no partial result is accepted as optimal")
        self.stats = {"nodes": nodes, "node_budget": node_budget,
                      "solutions": solutions, "solution_budget": solution_budget, **extra}


@dataclass(frozen=True)
class FoldedEdge:
    edge_id: int
    child_idx: int
    parent_idx: int                 # n means the virtual root
    score: int
    support: tuple                  # assertion summaries witnessing the edge
    implicit_root: bool             # zero-score fallback, not a real assertion


@dataclass
class SegmentModel:
    entity_order: list              # entity ids, index == vertex index
    root_idx: int
    edges: list                     # FoldedEdge
    self_loops: list               # excluded assertion groups (same-entity edges)
    base: int

    def edge_key(self, edge: FoldedEdge) -> int:
        """Composite weight: real score dominates; parent sequence breaks ties."""
        n = len(self.entity_order)
        parent_rank = self.root_idx + 1 if edge.parent_idx == self.root_idx else edge.parent_idx
        tie_digit = (self.base - 1 - parent_rank) * self.base ** (n - 1 - edge.child_idx)
        return edge.score * self.base ** n + tie_digit


def _assertion_summary(assertion):
    start, end = assertion.interval()
    return {"assertion_id": assertion.assertion_id, "score": assertion.score,
            "child_key": assertion.child_key, "parent_key": assertion.parent_key,
            "start": start, "end": end, "digest": assertion.digest}


def fold_support(active_assertions, entity_order):
    """Collapse valid support for identical parent/child edges.

    Same (child entity, parent entity) edges keep the maximum score only; all
    assertions attaining that maximum remain as supporting evidence.  Edges
    whose two endpoints resolve to the same entity are self-loops and are
    excluded rather than fed to the solver.  A zero-score virtual-root edge is
    attached to every entity so the forest is always feasible.
    """
    n = len(entity_order)
    index = {entity_id: i for i, entity_id in enumerate(entity_order)}
    groups, self_loops = {}, []
    for assertion in sorted(active_assertions, key=lambda a: a.assertion_id):
        child_i = index[assertion.child_entity]
        if assertion.parent_entity is None:
            parent_i = n
        elif assertion.parent_entity == assertion.child_entity:
            self_loops.append({
                "child_entity": assertion.child_entity,
                "child_key": assertion.child_key,
                "parent_key": assertion.parent_key,
                "assertions": [_assertion_summary(assertion)],
                "reason": "self_loop_excluded"})
            continue
        else:
            parent_i = index[assertion.parent_entity]
        groups.setdefault((child_i, parent_i), []).append(assertion)

    edges = []
    for edge_id, ((child_i, parent_i), assertions) in enumerate(
            sorted(groups.items(), key=lambda item: item[0])):
        top = max(a.score for a in assertions)
        supporters = tuple(_assertion_summary(a) for a in assertions if a.score == top)
        edges.append(FoldedEdge(edge_id, child_i, parent_i, top, supporters, False))

    # Virtual zero-score root fallback for every entity without an explicit
    # root declaration; explicit root edges already cover the same vertex.
    explicit_roots = {e.child_idx for e in edges if e.parent_idx == n}
    next_id = len(edges)
    for child_i in range(n):
        if child_i not in explicit_roots:
            edges.append(FoldedEdge(next_id, child_i, n, 0, (), True))
            next_id += 1
    model = SegmentModel(entity_order, n, edges, self_loops, n + 2)
    return model


# --------------------------------------------------------------------------- #
# Solver 1: Edmonds / Chu–Liu with explicit cycle contraction
# --------------------------------------------------------------------------- #

def edmonds_contract(model: SegmentModel):
    """Return the set of selected edge ids of a maximum-weight branching."""
    edges = model.edges
    weight = {e.edge_id: model.edge_key(e) for e in edges}
    # Edges point parent -> child: the head is the child that receives a parent.
    head = {e.edge_id: e.child_idx for e in edges}
    tail = {e.edge_id: e.parent_idx for e in edges}
    vertices = list(range(model.root_idx + 1))
    root = model.root_idx

    def solve(edge_ids, vertices_at_level, level_tail, level_head, level_weight):
        enter = {}
        incoming = {v: [] for v in vertices_at_level}
        for eid in edge_ids:
            incoming[level_head[eid]].append(eid)
        for vertex, candidates in incoming.items():
            if vertex == root or not candidates:
                continue
            # Lowest edge id wins an exact weight tie; composite weights
            # already encode the deterministic parent sequence.
            chosen = min(candidates, key=lambda e: (-level_weight[e], e))
            enter[vertex] = chosen

        cycle = _find_cycle(enter, vertices_at_level, level_tail)
        if cycle is None:
            return set(enter.values())

        cycle_set = set(cycle)
        token = ("cycle",) + tuple(sorted(str(v) for v in cycle_set))
        mapping = {v: (token if v in cycle_set else v) for v in vertices_at_level}
        new_vertices = [token] + [v for v in vertices_at_level if v not in cycle_set]

        new_tail, new_head, new_weight = {}, {}, {}
        for eid in edge_ids:
            old_tail, old_head = level_tail[eid], level_head[eid]
            if old_tail in cycle_set and old_head in cycle_set:
                continue  # cycle-internal edges disappear into the component
            new_tail[eid] = mapping[old_tail]
            new_head[eid] = mapping[old_head]
            new_weight[eid] = (
                level_weight[eid] - level_weight[enter[old_head]]
                if old_head in cycle_set else level_weight[eid])

        recursive = solve(list(new_tail), new_vertices, new_tail, new_head, new_weight)
        break_edge_vertex = None
        for eid in recursive:
            if level_head[eid] in cycle_set:
                break_edge_vertex = level_head[eid]
                break
        selected = set(recursive)
        for vertex in cycle_set:
            if vertex != break_edge_vertex:
                selected.add(enter[vertex])
        return selected

    return solve([e.edge_id for e in edges], vertices, tail, head, weight)


def _find_cycle(enter, vertices, tail):
    color = {v: 0 for v in vertices}  # 0 white, 1 gray (on path), 2 black
    for start in vertices:
        if color[start] != 0 or start not in enter:
            color[start] = 2
            continue
        path, v = [], start
        while True:
            if color.get(v) == 1:
                return path[path.index(v):]
            if color.get(v) == 2 or v not in enter:
                break
            color[v] = 1
            path.append(v)
            v = tail[enter[v]]
        for u in path:
            color[u] = 2
    return None


# --------------------------------------------------------------------------- #
# Solver 2: bounded branch-and-bound constraint search
# --------------------------------------------------------------------------- #

def branch_and_bound(model: SegmentModel, *, node_budget=1_000_000,
                     solution_budget=1_000_000):
    """Maximize the same composite weight with explicit search budgets."""
    n, root = len(model.entity_order), model.root_idx
    incoming = [[] for _ in range(n)]
    for edge in model.edges:
        incoming[edge.child_idx].append(edge)
    for candidates in incoming:
        candidates.sort(key=lambda e: (-model.edge_key(e), e.edge_id))
    optimistic = [max((model.edge_key(e) for e in candidates), default=0)
                  for candidates in incoming]

    assignment = [None] * n
    best = {"key": -1, "edges": None}
    stats = {"nodes": 0, "solutions": 0}

    def creates_cycle(child_i, parent_i):
        seen, v = set(), parent_i
        while v is not None and v != root:
            if v == child_i:
                return True
            if v in seen or assignment[v] is None:
                return False
            seen.add(v)
            v = model.edges[assignment[v]].parent_idx
        return False

    def search(i, current_key):
        stats["nodes"] += 1
        if stats["nodes"] > node_budget:
            raise BudgetExhausted(**stats, node_budget=node_budget,
                                  solution_budget=solution_budget)
        if i == n:
            stats["solutions"] += 1
            if stats["solutions"] > solution_budget:
                raise BudgetExhausted(**stats, node_budget=node_budget,
                                      solution_budget=solution_budget)
            if current_key > best["key"]:
                best["key"] = current_key
                best["edges"] = [e.edge_id for e in model.edges
                                 if assignment[e.child_idx] == e.edge_id]
            return
        upper_bound = current_key + sum(optimistic[k] for k in range(i, n))
        if best["edges"] is not None and upper_bound <= best["key"]:
            return  # equal composite keys encode identical parent sequences
        for edge in incoming[i]:
            if edge.parent_idx != root and creates_cycle(i, edge.parent_idx):
                continue
            assignment[i] = edge.edge_id
            search(i + 1, current_key + model.edge_key(edge))
            assignment[i] = None

    search(0, 0)
    if best["edges"] is None:  # pragma: no cover - root fallback guarantees feasibility
        raise RuntimeError("no feasible forest found")
    return set(best["edges"])


# --------------------------------------------------------------------------- #
# Independent oracle: exhaustive enumeration of every legal forest
# --------------------------------------------------------------------------- #

def enumerate_forests(model: SegmentModel):
    """Brute-force oracle for small graphs; returns (edge id set, score, tuple)."""
    forests = enumerate_all_forests(model)
    score, _, selected, parents = forests[0]
    return selected, score, parents


def enumerate_all_forests(model: SegmentModel):
    """Every legal forest of a small graph, best first under the same tie break."""
    n, root = len(model.entity_order), model.root_idx
    incoming = [[] for _ in range(n)]
    for edge in model.edges:
        incoming[edge.child_idx].append(edge)
    assignment = [None] * n
    found = []

    def creates_cycle(child_i, parent_i):
        v, seen = parent_i, set()
        while v is not None and v != root:
            if v == child_i:
                return True
            if v in seen or assignment[v] is None:
                return False
            seen.add(v)
            v = model.edges[assignment[v]].parent_idx
        return False

    def search(i):
        if i == n:
            selected = {assignment[k] for k in range(n)}
            score = sum(model.edges[eid].score for eid in selected)
            parents = tuple(model.edges[eid].parent_idx for eid in
                            (assignment[k] for k in range(n)))
            found.append((score, tuple(-p for p in parents), selected, parents))
            return
        for edge in incoming[i]:
            if edge.parent_idx != root and creates_cycle(i, edge.parent_idx):
                continue
            assignment[i] = edge.edge_id
            search(i + 1)
            assignment[i] = None

    search(0)
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return found


def solve_segment(model: SegmentModel, *, method="edmonds",
                  node_budget=1_000_000, solution_budget=1_000_000,
                  cross_check_with=None):
    """Solve one folded segment and assemble independently verifiable evidence."""
    if method == "edmonds":
        selected = edmonds_contract(model)
        solver_report = {"method": "cycle_contraction_edmonds", "optimality": "exact",
                         "rule_version": RULE_VERSION, "budget": None}
    elif method == "branch_and_bound":
        selected = branch_and_bound(
            model, node_budget=node_budget, solution_budget=solution_budget)
        solver_report = {"method": "branch_and_bound", "optimality": "exact",
                         "rule_version": RULE_VERSION,
                         "budget": {"node_budget": node_budget,
                                    "solution_budget": solution_budget}}
    else:
        raise ValueError(f"unknown solver method: {method}")

    if cross_check_with is not None:
        other = enumerate_forests(model) if cross_check_with == "enumerate" else \
            branch_and_bound(model, node_budget=node_budget,
                             solution_budget=solution_budget)
        other_set = other[0] if isinstance(other, tuple) else other
        if other_set != selected:
            raise RuntimeError("solver disagreement: relationship result is unsafe")
        solver_report["cross_check"] = cross_check_with

    by_id = {e.edge_id: e for e in model.edges}
    selected_edge_of = {by_id[eid].child_idx: by_id[eid] for eid in selected}
    total_score = sum(edge.score for edge in selected_edge_of.values())
    parent_sequence = []
    for child_i in range(len(model.entity_order)):
        edge = selected_edge_of[child_i]
        parent_sequence.append(ROOT if edge.parent_idx == model.root_idx
                               else model.entity_order[edge.parent_idx])

    evidence = []
    for edge in model.edges:
        child_entity = model.entity_order[edge.child_idx]
        parent_entity = ROOT if edge.parent_idx == model.root_idx \
            else model.entity_order[edge.parent_idx]
        chosen = selected_edge_of[edge.child_idx]
        row = {"child_entity": child_entity, "parent_entity": parent_entity,
               "score": edge.score, "implicit_root": edge.implicit_root,
               "support": list(edge.support)}
        if edge.edge_id in selected:
            row.update(status="selected", reason=None)
        else:
            row.update(status="excluded", reason=_exclusion_reason(
                model, edge, chosen, selected_edge_of))
        evidence.append(row)

    return {
        "selected": {model.entity_order[i]: parent for i, parent in
                     enumerate(parent_sequence)},
        "total_score": total_score,
        "parent_sequence": parent_sequence,
        "entity_order": list(model.entity_order),
        "evidence": evidence,
        "self_loops": list(model.self_loops),
        "solver": solver_report,
    }


def _exclusion_reason(model, edge, chosen_edge, selected_edge_of):
    """Factual reason for an unselected edge; never labels it a false relation.

    An edge is excluded by the acyclicity constraint when selecting it would
    close a cycle: following the already-selected parents from its candidate
    parent vertex reaches the child vertex.  This applies regardless of the
    edge's own score, so a high-score edge that would form a cycle is not
    mislabeled as a low-score loss.
    """
    if edge.parent_idx != model.root_idx and _would_close_cycle(
            model, edge, selected_edge_of):
        return "acyclicity_constraint"
    if edge.score == chosen_edge.score:
        return "tie_lost"
    return "lower_score"


def _would_close_cycle(model, candidate, selected_edge_of):
    """True if selected parents from the candidate parent reach its child."""
    v, seen = candidate.parent_idx, set()
    while v in selected_edge_of:
        edge = selected_edge_of[v]
        if edge.parent_idx == candidate.child_idx:
            return True
        if edge.parent_idx == model.root_idx or edge.parent_idx in seen:
            return False
        seen.add(edge.parent_idx)
        v = edge.parent_idx
    return False
