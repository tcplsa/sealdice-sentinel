from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path


class DiagnosticStore:
    """Separate bounded stores: root writes resource samples; Sentinel writes evidence.

    No chat text, HTTP payload, credential or command line belongs in either store.
    Connections are closed on every path, including rollback and read errors.
    """
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self, readonly: bool = False):
        if readonly:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=1)
        else:
            connection = sqlite3.connect(self.path, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            if not readonly:
                connection.execute("PRAGMA synchronous=NORMAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize_resources(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # Readers have group-read permissions only. WAL readers may need to create
            # shared-memory files, which would otherwise require directory write access.
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA journal_size_limit=1048576")
            db.execute("PRAGMA max_page_count=16384")  # 64 MiB at SQLite's usual 4 KiB pages.
            db.execute("CREATE TABLE IF NOT EXISTS resource_samples "
                       "(id INTEGER PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_resource_time ON resource_samples(at)")

    def initialize_evidence(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA journal_size_limit=1048576")
            db.execute("PRAGMA max_page_count=16384")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, scope TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observation_scope_time ON observations(scope, at);
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, created_at TEXT NOT NULL,
                    due_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)

    @staticmethod
    def _encode(payload: dict, maximum: int = 262144) -> str:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(text.encode()) > maximum:
            raise ValueError("diagnostic record exceeds its size bound")
        return text

    def append_resource(self, sample: dict, maximum: int = 720) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO resource_samples(at,payload) VALUES (?,?)",
                       (sample["at"], self._encode(sample)))
            db.execute("DELETE FROM resource_samples WHERE id NOT IN "
                       "(SELECT id FROM resource_samples ORDER BY id DESC LIMIT ?)", (maximum,))

    def resources(self, since: datetime | None = None, limit: int = 720) -> list[dict]:
        if not self.path.is_file():
            return []
        with self.connect(readonly=True) as db:
            rows = db.execute("SELECT payload FROM resource_samples WHERE at>=? "
                              "ORDER BY id DESC LIMIT ?",
                              ((since or datetime.min.replace(tzinfo=UTC)).isoformat(),
                               min(max(limit, 1), 3600))).fetchall()
            return [json.loads(row[0]) for row in reversed(rows)]

    def append_observation(self, at: datetime, scope: str, kind: str, payload: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO observations(at,scope,kind,payload) VALUES (?,?,?,?)",
                       (at.isoformat(), scope, kind, self._encode(payload, 16384)))
            db.execute("DELETE FROM observations WHERE id NOT IN "
                       "(SELECT id FROM observations ORDER BY id DESC LIMIT 6000)")

    def observations(self, since: datetime, scope: str | None = None) -> list[dict]:
        with self.connect(readonly=True) as db:
            rows = db.execute(
                "SELECT at,scope,kind,payload FROM observations WHERE at>=? "
                "AND (? IS NULL OR scope=? OR scope=? OR "
                "(instr(?, '/')=0 AND substr(scope,1,length(?)+1)=?||'/') OR scope='server') "
                "ORDER BY id DESC LIMIT 800",
                (since.isoformat(), scope, scope, scope.split("/")[0] if scope else None,
                 scope, scope, scope),
            ).fetchall()
            return [{"at": row["at"], "scope": row["scope"], "kind": row["kind"],
                     **json.loads(row["payload"])} for row in reversed(rows)]

    def save_report(self, report: dict, maximum: int, retention_days: int) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        # Preserve endpoints and findings if an unusually busy process has many sockets.
        # Explicitly label compacted timelines rather than silently losing samples.
        while len(self._encode(report, 8 * 1048576).encode()) > 524288:
            report["timeline_compacted"] = True
            changed = False
            for key in ("resources_before", "resources_after", "observations"):
                values = report.get(key, [])
                if len(values) > 3:
                    report[key] = [values[0], *values[1:-1:2], values[-1]]
                    changed = True
            if not changed:
                raise ValueError("diagnostic report cannot fit its storage limit")
        with self.connect() as db:
            db.execute("INSERT INTO reports VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
                       "SET state=excluded.state,payload=excluded.payload",
                       (report["id"], report["scope"], report["created_at"], report["due_at"],
                        report["state"], self._encode(report, 524288)))
            db.execute("DELETE FROM reports WHERE created_at<?", (cutoff.isoformat(),))
            db.execute("DELETE FROM reports WHERE id NOT IN "
                       "(SELECT id FROM reports ORDER BY created_at DESC LIMIT ?)", (maximum,))

    def reports(self, pending: bool = False, limit: int = 20) -> list[dict]:
        if not self.path.is_file():
            return []
        with self.connect(readonly=True) as db:
            rows = db.execute("SELECT payload FROM reports WHERE (?=0 OR state='collecting') "
                              "ORDER BY created_at DESC LIMIT ?", (int(pending), limit)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def report(self, identifier: str) -> dict | None:
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT payload FROM reports WHERE id=?", (identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def last_capture(self, scope: str) -> datetime | None:
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT created_at FROM reports WHERE scope=? "
                             "ORDER BY created_at DESC LIMIT 1", (scope,)).fetchone()
            return datetime.fromisoformat(row[0]) if row else None

    def checkpoint(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
