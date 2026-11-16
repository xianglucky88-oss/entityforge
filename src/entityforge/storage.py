"""Transactional revision storage; mutations use compare-and-swap."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class RevisionConflict(RuntimeError):
    pass


class SQLiteStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS revisions (
              revision INTEGER PRIMARY KEY, action TEXT NOT NULL,
              at TEXT NOT NULL, state TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS head (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO revisions VALUES (0,'init',?,?,?)",
                            (self.now(), json.dumps({"records": {}, "entities": {}, "evidence": []}), "{}"))
            self.db.execute("INSERT OR IGNORE INTO head VALUES (1,0)")

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def read(self, revision=None):
        if revision is None:
            revision = self.db.execute("SELECT revision FROM head").fetchone()[0]
        row = self.db.execute("SELECT state FROM revisions WHERE revision=?", (revision,)).fetchone()
        if row is None:
            raise KeyError(f"unknown revision: {revision}")
        return revision, json.loads(row[0])

    @contextmanager
    def edit(self, action, expected=None, details=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            revision, state = self.read()
            if expected is not None and revision != expected:
                raise RevisionConflict(f"expected {expected}, current {revision}")
            yield state
            self.db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",
                            (revision + 1, action, self.now(), json.dumps(state, ensure_ascii=False),
                             json.dumps(details or {}, ensure_ascii=False)))
            self.db.execute("UPDATE head SET revision=?", (revision + 1,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def history(self):
        return [{"revision": r, "action": a, "at": t, "details": json.loads(d)}
                for r, a, t, d in self.db.execute(
                    "SELECT revision,action,at,details FROM revisions ORDER BY revision")]

    def close(self):
        self.db.close()
