"""Relationship hierarchy smoke run against a fresh entity database.

Builds three master entities, submits the alternative-root cycle example from
the design (B->A=5, C->B=6, A->C=7 plus root claims), and demonstrates that the
exact solver returns the 17-point forest while a greedy weakest-edge deletion
would stop at 13.  The entity database and the relation artifact are two
independent SQLite files; rerunning the script recreates both from scratch.
"""
import json
import tempfile
from pathlib import Path

from entityforge import EntityService
from entityforge.relations import RelationEngine, enumerate_forests
from entityforge.relations.solver import fold_support

project = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory() as directory:
    entity_db = Path(directory) / "entities.sqlite"
    artifact = Path(directory) / "relations.sqlite"

    service = EntityService(entity_db)
    service.ingest([
        {"source": "crm", "id": "a", "fields": {"name": "A"}},
        {"source": "crm", "id": "b", "fields": {"name": "B"}},
        {"source": "crm", "id": "c", "fields": {"name": "C"}},
    ])
    service.resolve()

    engine = RelationEngine(artifact, entity_db)
    with open(project / "examples" / "relation_assertions.json",
              encoding="utf-8") as stream:
        view = engine.submit(json.load(stream))

    interval = view["intervals"][0]
    if interval["total_score"] != 17:
        raise RuntimeError("expected the exact 17-point optimum")
    if not engine.verify()["ok"]:
        raise RuntimeError("relationship artifact failed verification")

    # Independent oracle ground truth on the same folded segment model.
    pinned = engine.store.meta()
    assertions = engine.store.load_assertions()
    model = fold_support(assertions, pinned["entity_order"])
    _, oracle_score, _ = enumerate_forests(model)
    if oracle_score != 17:
        raise RuntimeError("exhaustive oracle disagrees with the solver")

    selected = {
        child: parent for child, parent in interval["selected"].items()}
    print(json.dumps({
        "ok": True,
        "pinned_revision": view["pinned_revision"],
        "rule_version": view["rule_version"],
        "exact_optimum": interval["total_score"],
        "oracle_optimum": oracle_score,
        "greedy_weakest_edge_deletion": 13,
        "selected_parents": selected,
        "exclusion_reasons": sorted({
            row["reason"] for row in interval["evidence"]
            if row["status"] == "excluded"}),
    }, ensure_ascii=False, indent=2))
    engine.close()
    service.close()
