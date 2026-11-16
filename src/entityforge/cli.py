import argparse
import json
from .service import EntityService
from .relations import (
    BudgetExhausted,
    RelationBatchRejected,
    RelationEngine,
)


def _relation_parser(sub):
    relation = sub.add_parser("relations",
                              help="independent relationship hierarchy artifact")
    relation.add_argument("--artifact", required=True,
                          help="separate SQLite database for the relation artifact")
    relation.add_argument("--method", choices=["edmonds", "branch_and_bound"],
                          default="edmonds")
    relation.add_argument("--node-budget", type=int, default=1_000_000)
    relation.add_argument("--solution-budget", type=int, default=1_000_000)
    rsub = relation.add_subparsers(dest="relation_command", required=True)
    submit = rsub.add_parser("submit")
    submit.add_argument("input")
    rsub.add_parser("view")
    rsub.add_parser("verify")
    at = rsub.add_parser("at")
    at.add_argument("instant")
    return relation


def main():
    parser = argparse.ArgumentParser(description="Versioned entity resolution")
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("input")
    sub.add_parser("resolve")
    sub.add_parser("show")
    sub.add_parser("history")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("revision", type=int)
    _relation_parser(sub)
    args = parser.parse_args()

    if args.command == "relations":
        engine = RelationEngine(
            args.artifact, args.db, method=args.method,
            node_budget=args.node_budget, solution_budget=args.solution_budget)
        try:
            if args.relation_command == "submit":
                with open(args.input, encoding="utf-8") as stream:
                    result = engine.submit(json.load(stream))
            elif args.relation_command == "view":
                result = engine.view()
            elif args.relation_command == "verify":
                result = engine.verify()
            else:
                result = engine.at(args.instant)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (RelationBatchRejected, BudgetExhausted, RuntimeError, ValueError) as exc:
            parser.exit(2, f"error: {exc}\n")
        finally:
            engine.close()
        return

    service = EntityService(args.db)
    try:
        if args.command == "ingest":
            with open(args.input, encoding="utf-8") as stream:
                result = {"revision": service.ingest(json.load(stream))}
        elif args.command == "resolve":
            result = service.resolve()
        elif args.command == "history":
            result = service.store.history()
        elif args.command == "rollback":
            result = service.rollback(args.revision)
        else:
            result = service.snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    finally:
        service.close()


if __name__ == "__main__":
    main()
