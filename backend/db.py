"""db.py — SQLite persistence for the log (the one stateful "organ").

Only the **log/notes** is persisted; protocols + inventory are file-driven and
the rest of SessionState is in-memory. The DB is an optional, drop-in organ: pass
``db_path=None`` (the default for tests) and SessionState stays purely in-memory.

The `notes` schema mirrors the in-memory log row exactly
(``id, text, timestamp, sample_id, step_ref``) so reading back is a 1:1 hydrate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    timestamp  TEXT    NOT NULL,
    sample_id  TEXT,
    step_ref   INTEGER,
    entry_type TEXT,
    edited     INTEGER
);
CREATE TABLE IF NOT EXISTS notebooks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_DEFAULT_NOTEBOOK_NAME = "Lab Notebook"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
        self._migrate()
        self._ensure_default_notebook()

    def _columns(self) -> set[str]:
        rows = self._conn.execute("PRAGMA table_info(notes)").fetchall()
        return {r["name"] for r in rows}

    def _migrate(self) -> None:
        """Idempotent additive migrations. Each column is independent so older
        databases gain new fields without dropping data."""
        cols = self._columns()
        if "category" not in cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN category TEXT")
        if "flag" not in cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN flag TEXT")
        if "notebook_id" not in cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN notebook_id INTEGER")
        if "entry_type" not in cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN entry_type TEXT")
            # Back-fill pre-existing rows by text shape: the only auto-generated
            # notes are the step-advance notes ("Completed step N" / "Skipped
            # step N"); everything else was written by a human.
            self._conn.execute(
                "UPDATE notes SET entry_type = CASE "
                "WHEN text LIKE 'Completed step%' OR text LIKE 'Skipped step%' "
                "THEN 'automatic' ELSE 'manual' END "
                "WHERE entry_type IS NULL"
            )
        if "edited" not in cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN edited INTEGER")
            self._conn.execute("UPDATE notes SET edited = 0 WHERE edited IS NULL")
        self._conn.commit()

    # --- notebooks ---------------------------------------------------------
    def _ensure_default_notebook(self) -> None:
        """Guarantee >=1 notebook, adopt any orphan notes, and pin an active id.

        Older databases predate notebooks: their notes carry ``notebook_id NULL``.
        We create a default notebook and re-home those notes into it so the feed
        is unchanged after the upgrade."""
        count = self._conn.execute("SELECT COUNT(*) AS c FROM notebooks").fetchone()["c"]
        if count == 0:
            self._conn.execute(
                "INSERT INTO notebooks (name, created_at) VALUES (?, ?)",
                (_DEFAULT_NOTEBOOK_NAME, _now_iso()),
            )
            self._conn.commit()
        first_id = self._conn.execute(
            "SELECT id FROM notebooks ORDER BY id ASC LIMIT 1"
        ).fetchone()["id"]
        self._conn.execute(
            "UPDATE notes SET notebook_id = ? WHERE notebook_id IS NULL", (first_id,)
        )
        self._conn.commit()
        if self.get_active_notebook_id() is None:
            self.set_active_notebook(int(first_id))

    def get_active_notebook_id(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'active_notebook_id'"
        ).fetchone()
        return int(row["value"]) if row and row["value"] is not None else None

    def set_active_notebook(self, notebook_id: int) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('active_notebook_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(int(notebook_id)),),
        )
        self._conn.commit()

    def list_notebooks(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT nb.id, nb.name, nb.created_at, COUNT(n.id) AS entry_count "
            "FROM notebooks nb LEFT JOIN notes n ON n.notebook_id = nb.id "
            "GROUP BY nb.id, nb.name, nb.created_at ORDER BY nb.id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_notebook(self, name: str) -> dict[str, Any]:
        ts = _now_iso()
        cur = self._conn.execute(
            "INSERT INTO notebooks (name, created_at) VALUES (?, ?)", (name, ts)
        )
        self._conn.commit()
        return {"id": int(cur.lastrowid), "name": name, "created_at": ts}

    def notebook_ids(self) -> set[int]:
        rows = self._conn.execute("SELECT id FROM notebooks").fetchall()
        return {int(r["id"]) for r in rows}

    def add_note(
        self,
        text: str,
        timestamp: str,
        sample_id: Optional[str],
        step_ref: Optional[int],
        category: Optional[str] = None,
        flag: Optional[dict[str, Any]] = None,
        notebook_id: Optional[int] = None,
        entry_type: str = "manual",
        edited: bool = False,
    ) -> dict[str, Any]:
        cur = self._conn.execute(
            "INSERT INTO notes (text, timestamp, sample_id, step_ref, category, flag, "
            "notebook_id, entry_type, edited) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (text, timestamp, sample_id, step_ref, category,
             json.dumps(flag) if flag is not None else None, notebook_id,
             entry_type, 1 if edited else 0),
        )
        self._conn.commit()
        return {
            "id": int(cur.lastrowid),
            "text": text,
            "timestamp": timestamp,
            "sample_id": sample_id,
            "step_ref": step_ref,
            "category": category,
            "flag": flag,
            "entry_type": entry_type,
            "edited": edited,
        }

    def all_notes(self, notebook_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Notes for one notebook (``notebook_id``), or every note when None."""
        if notebook_id is None:
            rows = self._conn.execute(
                "SELECT id, text, timestamp, sample_id, step_ref, category, flag, "
                "entry_type, edited FROM notes ORDER BY id ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, text, timestamp, sample_id, step_ref, category, flag, "
                "entry_type, edited FROM notes WHERE notebook_id = ? ORDER BY id ASC",
                (notebook_id,),
            ).fetchall()
        notes = []
        for r in rows:
            note = dict(r)
            note["flag"] = json.loads(note["flag"]) if note["flag"] else None
            note["entry_type"] = note["entry_type"] or "manual"  # NULL on legacy rows
            note["edited"] = bool(note["edited"])
            notes.append(note)
        return notes

    def delete_note(self, id: int) -> None:
        self._conn.execute("DELETE FROM notes WHERE id = ?", (id,))
        self._conn.commit()

    def clear_all(self) -> None:
        self._conn.execute("DELETE FROM notes")
        self._conn.commit()

    def clear_notebook(self, notebook_id: int) -> None:
        self._conn.execute("DELETE FROM notes WHERE notebook_id = ?", (notebook_id,))
        self._conn.commit()

    def factory_reset(self) -> None:
        """Wipe every note + notebook and re-seed the single default notebook
        (the DB's original state). Restarts ids at 1 for a truly fresh feed."""
        self._conn.execute("DELETE FROM notes")
        self._conn.execute("DELETE FROM notebooks")
        self._conn.execute("DELETE FROM meta WHERE key = 'active_notebook_id'")
        # sqlite_sequence only exists once an AUTOINCREMENT row has been written;
        # guard the reset so a never-written DB doesn't raise "no such table".
        if self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone():
            self._conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('notes', 'notebooks')"
            )
        self._conn.commit()
        self._ensure_default_notebook()

    def update_text(self, id: int, text: str, flag: Optional[dict[str, Any]] = None) -> None:
        # This is the human-edit path (voice "correct that" + the notebook edit
        # button), so the entry becomes manual + edited regardless of its origin.
        self._conn.execute(
            "UPDATE notes SET text = ?, flag = ?, entry_type = 'manual', edited = 1 WHERE id = ?",
            (text, json.dumps(flag) if flag is not None else None, id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
