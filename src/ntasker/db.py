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

CREATE TABLE IF NOT EXISTS task_deps (
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_dep ON task_deps(depends_on_id);

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
        # v2.0 settings cleanup: the projects_dir key is obsolete (projects
        # are now derived from tasks). Drop any stale row so it stops
        # showing up under "All settings (DB content)" in /settings.
        # Wrapped in try/except for pre-1.0 DBs that never had the table.
        try:
            conn.execute("DELETE FROM settings WHERE key = 'projects_dir'")
        except sqlite3.OperationalError:
            pass
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


def row_to_task(
    row: sqlite3.Row,
    tags: list[str] | None = None,
    depends: list[dict] | None = None,
) -> dict:
    """Convert a sqlite3.Row to a JSON-serialisable dict.

    ``depends`` is a list of ``{id, title, done}`` dicts (the tasks this
    one depends on), resolved by the caller. A task is "blocked" as long as
    any of its dependencies is not ``done`` -- the frontend derives that
    from the ``done`` flags.
    """
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
        "depends": depends or [],
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


# ---------------------------------------------------------------------------
# Dependency helpers (task -> task, M2M, kept acyclic)
# ---------------------------------------------------------------------------


class DepError(ValueError):
    """Raised when a proposed dependency set is invalid.

    ``reason`` is one of ``"self"`` / ``"missing"`` / ``"cycle"``; ``ref`` is
    the offending task id (or ``None`` for self-reference). Callers map this
    onto an HTTP 400 with a localized message.
    """

    def __init__(self, reason: str, ref: int | None = None):
        self.reason = reason
        self.ref = ref
        super().__init__(f"dependency error: {reason} (ref={ref})")


def normalize_dep_ids(raw: list[int]) -> list[int]:
    """Dedupe to ints, first-seen order. No other filtering.

    Self-reference is deliberately NOT dropped here -- :func:`validate_deps`
    rejects it (and cycles / missing targets) with a clear error, rather
    than silently turning a bad input into a destructive empty-set update.
    """
    seen: set[int] = set()
    out: list[int] = []
    for v in raw:
        i = int(v)
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _depends_on(conn: sqlite3.Connection, task_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT depends_on_id FROM task_deps WHERE task_id = ?", (task_id,)
    ).fetchall()
    return [int(r["depends_on_id"]) for r in rows]


def validate_deps(conn: sqlite3.Connection, task_id: int, dep_ids: list[int]) -> None:
    """Reject self-reference, missing targets, and cycles. Raises DepError.

    Cycle check: setting ``task_id`` to depend on each ``d`` would close a
    cycle iff ``task_id`` is already reachable from ``d`` along existing
    dependency edges. We walk the graph but ignore ``task_id``'s own current
    outgoing edges, since this call *replaces* them.
    """
    for d in dep_ids:
        if d == task_id:
            raise DepError("self")
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (d,)).fetchone()
        if exists is None:
            raise DepError("missing", d)

    for d in dep_ids:
        # BFS from d; can we get back to task_id? Skip task_id's outgoing
        # edges (they are being overwritten by this very update).
        stack = [d]
        visited: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur == task_id:
                raise DepError("cycle", d)
            if cur in visited:
                continue
            visited.add(cur)
            if cur == task_id:
                continue
            stack.extend(_depends_on(conn, cur))


def set_task_deps(conn: sqlite3.Connection, task_id: int, dep_ids: list[int]) -> None:
    """Replace the full dependency set of a task (delete + reinsert)."""
    conn.execute("DELETE FROM task_deps WHERE task_id = ?", (task_id,))
    if not dep_ids:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO task_deps (task_id, depends_on_id) VALUES (?, ?)",
        [(task_id, d) for d in dep_ids],
    )


def load_deps_for(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """Return ``[{id, title, done}]`` for a single task, ordered by id."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.title AS title, t.status AS status
        FROM task_deps d
        JOIN tasks t ON t.id = d.depends_on_id
        WHERE d.task_id = ?
        ORDER BY t.id ASC
        """,
        (task_id,),
    ).fetchall()
    return [
        {"id": int(r["id"]), "title": r["title"], "done": r["status"] == "done"}
        for r in rows
    ]


def load_deps_bulk(conn: sqlite3.Connection, task_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk lookup: task_id -> ``[{id, title, done}]``."""
    if not task_ids:
        return {}
    placeholders = ", ".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT d.task_id AS task_id, t.id AS id, t.title AS title, t.status AS status
        FROM task_deps d
        JOIN tasks t ON t.id = d.depends_on_id
        WHERE d.task_id IN ({placeholders})
        ORDER BY t.id ASC
        """,
        task_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {tid: [] for tid in task_ids}
    for r in rows:
        out[int(r["task_id"])].append(
            {"id": int(r["id"]), "title": r["title"], "done": r["status"] == "done"}
        )
    return out
