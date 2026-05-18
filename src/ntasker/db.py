"""SQLite layer for ntasker — schema + connection helpers.

No ORM, no Alembic. The schema is created on demand via ``init_db`` from
either ``ntasker init`` (CLI) or the FastAPI startup hook. ``init_db`` is
idempotent: ``CREATE TABLE IF NOT EXISTS`` plus ``ALTER TABLE`` blocks
wrapped in ``try/except OperationalError`` so a re-run is a no-op.

The active DB path is resolved at app/CLI startup (see
:mod:`ntasker.paths`) and stored in :data:`DB_PATH`. Helpers that open a
connection read this module-level global so a single resolve at boot
threads through every request without function-arg plumbing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Module-level "current DB" — set once at startup by paths.resolve_db_path()
# via :func:`set_db_path`. The smoke test rebinds it to a tempfile.
DB_PATH: Path | None = None


def set_db_path(path: Path) -> None:
    """Bind the active DB path. Called once by CLI / FastAPI startup."""
    global DB_PATH
    DB_PATH = path


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    phase TEXT NOT NULL DEFAULT 'planned',
    priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_archived ON tasks(archived);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS task_tags (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(path: Path | None = None) -> None:
    """Create / migrate the schema. Idempotent.

    If ``path`` is given, it overrides :data:`DB_PATH` for this call (used
    by tests). Otherwise the active :data:`DB_PATH` is used; it must be
    set first via :func:`set_db_path`.
    """
    target = path if path is not None else DB_PATH
    if target is None:
        raise RuntimeError("init_db called without DB_PATH set")
    with sqlite3.connect(target) as conn:
        conn.executescript(SCHEMA)
        # Idempotent legacy-cleanup: drop the long-gone `source` column
        # from pre-0.2 databases. SQLite >= 3.35 supports DROP COLUMN.
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            if "source" in cols:
                conn.execute("ALTER TABLE tasks DROP COLUMN source")
        except sqlite3.OperationalError:
            pass
        # Idempotent migration: add `priority` column on pre-0.4 DBs.
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
            )
        except sqlite3.OperationalError:
            pass
        # v2.0 phase migration: legacy values `later` and NULL collapse into
        # `planned`. The new vocabulary is {planned, wip, review}; the column
        # also becomes NOT NULL. We update existing rows in-place; SQLite
        # CREATE TABLE's NOT NULL constraint only applies to *new* rows, so
        # this is enough -- no table rewrite needed.
        conn.execute(
            "UPDATE tasks SET phase = 'planned' "
            "WHERE phase IS NULL OR phase = 'later'"
        )
        conn.commit()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with row factory + foreign keys on."""
    if DB_PATH is None:
        raise RuntimeError("get_conn called without DB_PATH set")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_task(row: sqlite3.Row, tags: list[str] | None = None) -> dict:
    """Convert a sqlite3.Row to a JSON-serialisable dict."""
    return {
        "id": row["id"],
        "project": row["project"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "phase": row["phase"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "archived": bool(row["archived"]),
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------


def normalize_tag(raw: str) -> str:
    """Lower-case + strip. Empty strings are filtered by callers."""
    return raw.strip().lower()


def normalize_tags(raw_list: list[str]) -> list[str]:
    """Apply normalize+dedupe (preserving first-seen order). Drops empties."""
    seen: set[str] = set()
    out: list[str] = []
    for t in raw_list:
        n = normalize_tag(t)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def ensure_tags(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """Insert missing tags, return ID list aligned with ``names``."""
    ids: list[int] = []
    for n in names:
        cur = conn.execute("SELECT id FROM tags WHERE name = ?", (n,))
        row = cur.fetchone()
        if row is None:
            cur = conn.execute("INSERT INTO tags (name) VALUES (?)", (n,))
            ids.append(int(cur.lastrowid))
        else:
            ids.append(int(row["id"]))
    return ids


def set_task_tags(conn: sqlite3.Connection, task_id: int, names: list[str]) -> None:
    """Replace the full tag set of a task (delete + reinsert)."""
    conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
    if not names:
        return
    tag_ids = ensure_tags(conn, names)
    conn.executemany(
        "INSERT OR IGNORE INTO task_tags (task_id, tag_id) VALUES (?, ?)",
        [(task_id, tid) for tid in tag_ids],
    )


def load_tags_for(conn: sqlite3.Connection, task_id: int) -> list[str]:
    """Return tag names for a single task, alphabetically."""
    rows = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN task_tags tt ON tt.tag_id = t.id
        WHERE tt.task_id = ?
        ORDER BY t.name ASC
        """,
        (task_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def load_tags_bulk(conn: sqlite3.Connection, task_ids: list[int]) -> dict[int, list[str]]:
    """Bulk lookup: task_id -> list of tag names."""
    if not task_ids:
        return {}
    placeholders = ", ".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT tt.task_id AS task_id, t.name AS name
        FROM task_tags tt
        JOIN tags t ON t.id = tt.tag_id
        WHERE tt.task_id IN ({placeholders})
        ORDER BY t.name ASC
        """,
        task_ids,
    ).fetchall()
    out: dict[int, list[str]] = {tid: [] for tid in task_ids}
    for r in rows:
        out[int(r["task_id"])].append(r["name"])
    return out
