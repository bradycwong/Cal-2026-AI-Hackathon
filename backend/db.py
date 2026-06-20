"""db.py — SQLite persistence for the log (the one stateful "organ").

Only the **log/notes** is persisted; protocols + inventory are file-driven and
the rest of SessionState is in-memory. The DB is an optional, drop-in organ: pass
``db_path=None`` (the default for tests) and SessionState stays purely in-memory.

The `notes` schema mirrors the in-memory log row exactly
(``id, text, timestamp, sample_id, step_ref``) so reading back is a 1:1 hydrate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    text      TEXT    NOT NULL,
    timestamp TEXT    NOT NULL,
    sample_id TEXT,
    step_ref  INTEGER
);
"""


class NoteStore:
    """Thin sqlite wrapper for the log feed. One connection, serialized writes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI may touch this from different threads.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add_note(
        self, text: str, timestamp: str, sample_id: Optional[str], step_ref: Optional[int]
    ) -> dict[str, Any]:
        cur = self._conn.execute(
            "INSERT INTO notes (text, timestamp, sample_id, step_ref) VALUES (?, ?, ?, ?)",
            (text, timestamp, sample_id, step_ref),
        )
        self._conn.commit()
        return {
            "id": int(cur.lastrowid),
            "text": text,
            "timestamp": timestamp,
            "sample_id": sample_id,
            "step_ref": step_ref,
        }

    def all_notes(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, text, timestamp, sample_id, step_ref FROM notes ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
