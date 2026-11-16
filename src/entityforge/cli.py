import argparse
import json
from .service import EntityService


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
    args = parser.parse_args()
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
