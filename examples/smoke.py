"""Run real CLI processes against a persistent database and verify the result."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONPATH=str(project / "src"))


def cli(db, *args):
    completed = subprocess.run([sys.executable, "-m", "entityforge.cli", "--db", str(db), *map(str, args)],
                               env=env, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / "entities.sqlite"
    imported = cli(db, "ingest", project / "examples" / "records.json")
    result = cli(db, "resolve")
    if len(result["entities"]) != 2 or len(result["records"]) != 3:
        raise RuntimeError("unexpected entity resolution")
    persisted = cli(db, "show")
    if persisted != result: raise RuntimeError("database did not survive process restart")
    restored = cli(db, "rollback", imported["revision"])
    if restored["entities"]: raise RuntimeError("rollback did not restore pre-resolution revision")
    if len(cli(db, "history")) != 4: raise RuntimeError("missing audit history")
    print(json.dumps({"ok": True, "records": 3, "entities": 2, "restart": True, "rollback": True, "cli_processes": 5}))
