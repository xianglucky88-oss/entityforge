"""Transactional revision storage; mutations use compare-and-swap."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class RevisionConflict(RuntimeError):
    pass


class BatchConflict(RuntimeError):
    """A batch id was reused with different content."""


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
            CREATE TABLE IF NOT EXISTS batches (
              batch_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
              content_hash TEXT NOT NULL, receipt TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS record_index (
              record_key TEXT PRIMARY KEY, normalized TEXT NOT NULL, blocks TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS index_meta (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO revisions VALUES (0,'init',?,?,?)",
                            (self.now(), json.dumps({"records": {}, "entities": {}, "evidence": [],
                                                     "meta": {"resolved": True, "rules": None}}), "{}"))
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

    def current_revision(self):
        return self.db.execute("SELECT revision FROM head").fetchone()[0]

    def _before_commit(self):
        """Runs after all writes of a mutation, just before commit.

        No-op in production; tests subclass the store and abort the process
        here to prove that a crash with uncommitted writes leaves no partial
        revision, index or batch receipt behind."""

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
            self._before_commit()
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get_batch(self, batch_id):
        row = self.db.execute(
            "SELECT revision,content_hash,receipt,at FROM batches WHERE batch_id=?",
            (batch_id,)).fetchone()
        if row is None:
            return None
        revision, content_hash, receipt, at = row
        return {"revision": revision, "content_hash": content_hash,
                "receipt": json.loads(receipt), "at": at}

    def save_batch(self, batch_id, revision, content_hash, receipt):
        # Called inside edit(): the receipt commits or rolls back with the revision.
        self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?)",
                        (batch_id, revision, content_hash,
                         json.dumps(receipt, ensure_ascii=False), self.now()))

    def index_revision(self):
        row = self.db.execute("SELECT revision FROM index_meta").fetchone()
        return row[0] if row else None

    def set_index_revision(self, revision):
        self.db.execute("INSERT INTO index_meta VALUES (1,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision",
                        (revision,))

    def index_rows(self):
        return {key: {"normalized": json.loads(normalized), "blocks": json.loads(blocks)}
                for key, normalized, blocks in
                self.db.execute("SELECT record_key,normalized,blocks FROM record_index")}

    def replace_index(self, rows, revision):
        self.db.execute("DELETE FROM record_index")
        self.update_index(rows, [], revision)

    def update_index(self, upserts, deletes, revision):
        # Called inside edit(): index rows commit or roll back with the revision.
        self.db.executemany(
            "INSERT OR REPLACE INTO record_index VALUES (?,?,?)",
            [(key, json.dumps(row["normalized"], ensure_ascii=False),
              json.dumps(row["blocks"], ensure_ascii=False)) for key, row in upserts.items()])
        self.db.executemany("DELETE FROM record_index WHERE record_key=?",
                            [(key,) for key in deletes])
        self.set_index_revision(revision)

    def history(self):
        return [{"revision": r, "action": a, "at": t, "details": json.loads(d)}
                for r, a, t, d in self.db.execute(
                    "SELECT revision,action,at,details FROM revisions ORDER BY revision")]

    def close(self):
        self.db.close()
