import argparse
import json
from .service import EntityService
from .storage import BatchConflict, RevisionConflict


def main():
    parser = argparse.ArgumentParser(description="Versioned entity resolution")
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("input")
    batch = sub.add_parser("batch")
    batch.add_argument("input")
    batch.add_argument("--batch-id")
    batch.add_argument("--expected", type=int)
    sub.add_parser("resolve")
    sub.add_parser("show")
    sub.add_parser("history")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("revision", type=int)
    args = parser.parse_args()
    service = EntityService(args.db)
    try:
        if args.command == "ingest":
            with open(args.input, encoding="utf-8") as stream:
                result = {"revision": service.ingest(json.load(stream))}
        elif args.command == "batch":
            with open(args.input, encoding="utf-8") as stream:
                payload = json.load(stream)
            operations, batch_id, expected = payload, args.batch_id, args.expected
            if isinstance(payload, dict):
                operations = payload.get("operations", [])
                batch_id = batch_id or payload.get("batch_id")
                if expected is None:
                    expected = payload.get("expected")
            if not batch_id:
                raise ValueError("batch id required (--batch-id or payload batch_id)")
            result = service.apply_batch(batch_id, operations, expected=expected)
        elif args.command == "resolve":
            result = service.resolve()
        elif args.command == "history":
            result = service.store.history()
        elif args.command == "rollback":
            result = service.rollback(args.revision)
        else:
            result = service.snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except RevisionConflict as exc:
        parser.exit(2, f"error: revision_conflict: {exc}\n")
    except BatchConflict as exc:
        parser.exit(2, f"error: batch_conflict: {exc}\n")
    except (ValueError, KeyError) as exc:
        parser.exit(2, f"error: invalid: {exc}\n")
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")
    finally:
        service.close()


if __name__ == "__main__":
    main()
