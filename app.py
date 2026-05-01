"""nerdocs Tracker -- lightweight local task tracker.

Single-user FastAPI app backed by SQLite. Bound hard to 127.0.0.1.
No auth: this is a local desktop tool for Christian. Do not expose.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# Single source of truth for the app version. Surfaced into the HTML
# template as a cache-buster (``?v=<VERSION>`` query suffix on every
# static asset URL) so a release reliably forces clients to fetch the
# new CSS/JS instead of replaying a stale ``index.html`` -> stale
# ``app.js`` from disk cache (the cause of "phantom button" reports
# after the v0.3.1 fix shipped: clients still saw the old DOM).
VERSION = "0.3.4"

# Sentinel for "tasks without a project" (cross-project tasks). Used in
# multi-value project filters: ?project=__none__ -> include rows with project IS NULL.
PROJECT_NONE_SENTINEL = "__none__"
# Legacy single-value sentinel kept for backwards compatibility with old bookmarks.
PROJECT_NULL_LEGACY = "__null__"

# Sentinel for "tasks without a phase" (analogous to PROJECT_NONE_SENTINEL).
PHASE_NONE_SENTINEL = "__none__"

# Fixed phase order + labels for the sidebar feed. Order is intentional
# (workflow-natural), not alphabetical, and not by count.
PHASE_ORDER: list[tuple[str, str]] = [
    ("wip", "in Arbeit"),
    ("planned", "geplant"),
    ("later", "später"),
    (PHASE_NONE_SENTINEL, "ohne Phase"),
]
PHASE_VALID = {value for value, _ in PHASE_ORDER}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tasks.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Live project discovery: symlinks under nerdocs/Projekte/
PROJECTS_DIR = Path("/home/christian/nerdocs/Projekte")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    phase TEXT,
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
"""


def init_db() -> None:
    """Create the schema if it does not exist. Idempotent.

    Also performs a one-shot drop of the legacy ``source`` column on the
    ``tasks`` table -- this is a no-op on fresh databases. SQLite >= 3.35
    supports ``ALTER TABLE ... DROP COLUMN`` natively; older runtimes will
    raise an ``OperationalError`` which we swallow (the column may already
    be gone).
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        # Idempotent legacy-cleanup: drop the long-gone `source` column.
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            if "source" in cols:
                conn.execute("ALTER TABLE tasks DROP COLUMN source")
        except sqlite3.OperationalError:
            # Old SQLite without DROP COLUMN -- not catastrophic, leave the
            # legacy column in place; it's just unused dead weight then.
            pass
        conn.commit()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with row factory and foreign keys on."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_task(row: sqlite3.Row, tags: list[str] | None = None) -> dict:
    """Convert a sqlite3.Row to a JSON-serialisable dict.

    `tags` is injected by the caller after a separate lookup to avoid
    SQLite GROUP_CONCAT escaping headaches with comma-containing tag names.
    """
    return {
        "id": row["id"],
        "project": row["project"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "phase": row["phase"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "archived": bool(row["archived"]),
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------


def _normalize_tag(raw: str) -> str:
    """Lower-case + strip. Empty strings are filtered by callers."""
    return raw.strip().lower()


def _normalize_tags(raw_list: list[str]) -> list[str]:
    """Apply normalize+dedupe (preserving first-seen order). Drops empties."""
    seen: set[str] = set()
    out: list[str] = []
    for t in raw_list:
        n = _normalize_tag(t)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _ensure_tags(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """Insert missing tags, return ID list aligned with `names`.

    `names` must already be normalized (lower + strip + deduped).
    """
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


def _set_task_tags(conn: sqlite3.Connection, task_id: int, names: list[str]) -> None:
    """Replace the full tag set of a task (delete + reinsert)."""
    conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
    if not names:
        return
    tag_ids = _ensure_tags(conn, names)
    conn.executemany(
        "INSERT OR IGNORE INTO task_tags (task_id, tag_id) VALUES (?, ?)",
        [(task_id, tid) for tid in tag_ids],
    )


def _load_tags_for(conn: sqlite3.Connection, task_id: int) -> list[str]:
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


def _load_tags_bulk(conn: sqlite3.Connection, task_ids: list[int]) -> dict[int, list[str]]:
    """Bulk lookup: task_id -> list of tag names. Used by /api/tasks."""
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
# Project discovery
# ---------------------------------------------------------------------------


def list_projects() -> list[str]:
    """Return live list of project symlink names under nerdocs/Projekte/.

    Filters: must be a symlink, no leading dot, no '.lock' suffix.
    Sorted alphabetically.
    """
    if not PROJECTS_DIR.exists():
        return []
    return sorted(
        p.name
        for p in PROJECTS_DIR.iterdir()
        if p.is_symlink() and not p.name.startswith(".") and not p.name.endswith(".lock")
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


Status = Literal["open", "done"]
Phase = Literal["wip", "planned", "later"]


class TaskCreate(BaseModel):
    project: str | None = None
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    phase: Phase | None = None
    tags: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    project: str | None = None
    title: str | None = Field(None, min_length=1, max_length=500)
    description: str | None = None
    status: Status | None = None
    phase: Phase | None = None
    archived: bool | None = None
    tags: list[str] | None = None  # None = unchanged; [] = clear all


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="nerdocs Tracker",
    description="Local single-user task tracker.",
    version=VERSION,
    docs_url="/api/docs",
    redoc_url=None,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Routes -- HTML
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # ``Cache-Control: no-store`` on the HTML shell guarantees the browser
    # always fetches the current ``index.html``. The static assets it
    # references carry a ``?v=<VERSION>`` query string (see template), so a
    # version bump invalidates their cache atomically.
    response = templates.TemplateResponse(
        request, "index.html", context={"version": VERSION}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Routes -- API: projects + tags (sidebar feed, with open-counts)
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def api_projects() -> JSONResponse:
    """Sidebar feed: ``__none__`` first, then live symlinks, each with open_count.

    Counts are absolute (open + non-archived) and ignore the active filter
    selection -- the sidebar must not flicker as the user toggles entries.
    """
    projects = list_projects()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT project, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY project
            """
        ).fetchall()
    counts: dict[str | None, int] = {row["project"]: int(row["c"]) for row in rows}

    out: list[dict] = [
        {"name": PROJECT_NONE_SENTINEL, "open_count": counts.get(None, 0)},
    ]
    for name in projects:
        out.append({"name": name, "open_count": counts.get(name, 0)})
    return JSONResponse(out)


@app.get("/api/tags")
def api_tags() -> JSONResponse:
    """All known tags with open-counts, sorted by ``open_count DESC, name ASC``."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.name AS name,
                   COALESCE(SUM(CASE WHEN tasks.status = 'open' AND tasks.archived = 0
                                     THEN 1 ELSE 0 END), 0) AS open_count
            FROM tags t
            LEFT JOIN task_tags tt ON tt.tag_id = t.id
            LEFT JOIN tasks ON tasks.id = tt.task_id
            GROUP BY t.id, t.name
            ORDER BY open_count DESC, name ASC
            """
        ).fetchall()
    return JSONResponse([{"name": r["name"], "open_count": int(r["open_count"])} for r in rows])


@app.post("/api/tags/cleanup")
def api_tags_cleanup() -> JSONResponse:
    """Delete dangling tags (no row in ``task_tags``).

    User-triggered, idempotent. Returns the number of removed tags plus their
    names so the UI can render a precise toast.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT name FROM tags
            WHERE id NOT IN (SELECT DISTINCT tag_id FROM task_tags)
            ORDER BY name ASC
            """
        ).fetchall()
        names = [r["name"] for r in rows]
        if names:
            conn.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM task_tags)"
            )
    return JSONResponse({"removed": len(names), "removed_names": names})


@app.get("/api/phases")
def api_phases() -> JSONResponse:
    """Sidebar feed for the phase filter.

    Fixed order (workflow-natural): wip -> planned -> later -> __none__.
    Counts are absolute (open + non-archived) just like /api/projects, so
    the sidebar does not flicker when filters toggle.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT phase, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY phase
            """
        ).fetchall()
    counts: dict[str | None, int] = {row["phase"]: int(row["c"]) for row in rows}

    out: list[dict] = []
    for value, label in PHASE_ORDER:
        key: str | None = None if value == PHASE_NONE_SENTINEL else value
        out.append(
            {
                "value": value,
                "label": label,
                "open_count": counts.get(key, 0),
            }
        )
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# Routes -- API: tasks
# ---------------------------------------------------------------------------


def _build_project_filter(project: list[str]) -> tuple[str, list[object]]:
    """Translate a multi-value project filter to a SQL fragment + params.

    Semantics:
      - empty list  -> no filter (return ('', []))
      - sentinel "__none__" in the list -> include rows with project IS NULL
      - other values -> WHERE project IN (?, ?, ...)
      - mixed (names + __none__) -> (project IN (...) OR project IS NULL)

    All values are bound as ? placeholders; never f-string'd into SQL.
    """
    if not project:
        return "", []

    include_null = False
    names: list[str] = []
    for p in project:
        if p in (PROJECT_NONE_SENTINEL, PROJECT_NULL_LEGACY):
            include_null = True
        elif p:
            names.append(p)

    clauses: list[str] = []
    params: list[object] = []
    if names:
        placeholders = ", ".join("?" for _ in names)
        clauses.append(f"project IN ({placeholders})")
        params.extend(names)
    if include_null:
        clauses.append("project IS NULL")

    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


def _build_phase_filter(phase: list[str]) -> tuple[str, list[object]]:
    """Translate a multi-value phase filter to a SQL fragment + params.

    Semantics mirror :func:`_build_project_filter`:
      - empty list -> no filter
      - sentinel "__none__" -> include rows with phase IS NULL
      - other values -> WHERE phase IN (?, ?, ...)
      - mixed -> (phase IN (...) OR phase IS NULL)

    Unknown phase values are silently dropped to keep the API permissive.
    """
    if not phase:
        return "", []

    include_null = False
    names: list[str] = []
    for p in phase:
        if p == PHASE_NONE_SENTINEL:
            include_null = True
        elif p in PHASE_VALID:
            names.append(p)

    clauses: list[str] = []
    params: list[object] = []
    if names:
        placeholders = ", ".join("?" for _ in names)
        clauses.append(f"phase IN ({placeholders})")
        params.extend(names)
    if include_null:
        clauses.append("phase IS NULL")

    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


def _build_tag_filter(tag: list[str]) -> tuple[str, list[object]]:
    """Multi-value OR filter on tag names (case-insensitive).

    Returns a fragment that joins task IDs against ``task_tags`` via a
    sub-select (keeps the outer query DISTINCT-free and easy to compose with
    project + status filters). Empty list -> no filter.
    """
    norm = _normalize_tags(tag)
    if not norm:
        return "", []
    placeholders = ", ".join("?" for _ in norm)
    fragment = (
        f" AND tasks.id IN (SELECT tt.task_id FROM task_tags tt "
        f"JOIN tags t ON t.id = tt.tag_id "
        f"WHERE t.name IN ({placeholders}))"
    )
    return fragment, list(norm)


def _query_tasks(
    project: list[str],
    tag: list[str],
    phase: list[str],
    status: Status | None,
    archived: bool | None,
    search: str | None,
) -> list[sqlite3.Row]:
    """Run the SELECT against the tasks table with filters applied. Pure SQL helper."""
    sql = "SELECT tasks.* FROM tasks WHERE 1=1"
    params: list[object] = []

    proj_clause, proj_params = _build_project_filter(project)
    if proj_clause:
        sql += proj_clause
        params.extend(proj_params)

    tag_clause, tag_params = _build_tag_filter(tag)
    if tag_clause:
        sql += tag_clause
        params.extend(tag_params)

    phase_clause, phase_params = _build_phase_filter(phase)
    if phase_clause:
        sql += phase_clause
        params.extend(phase_params)

    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    if archived is not None:
        sql += " AND archived = ?"
        params.append(1 if archived else 0)
    if search:
        sql += " AND (title LIKE ? OR COALESCE(description, '') LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])

    sql += " ORDER BY archived ASC, status ASC, created_at DESC"

    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/api/tasks")
def api_list_tasks(
    project: list[str] = Query(default=[]),  # noqa: B008
    tag: list[str] = Query(default=[]),  # noqa: B008
    phase: list[str] = Query(default=[]),  # noqa: B008
    status: Status | None = None,
    archived: bool | None = None,
    search: str | None = None,
) -> JSONResponse:
    rows = _query_tasks(project, tag, phase, status, archived, search)
    ids = [int(r["id"]) for r in rows]
    with get_conn() as conn:
        tags_by_id = _load_tags_bulk(conn, ids)
    return JSONResponse([row_to_task(r, tags_by_id.get(int(r["id"]), [])) for r in rows])


@app.get("/api/stats")
def api_stats(
    project: list[str] = Query(default=[]),  # noqa: B008
    tag: list[str] = Query(default=[]),  # noqa: B008
    phase: list[str] = Query(default=[]),  # noqa: B008
    search: str | None = None,
) -> JSONResponse:
    """Return tab counts (open/done/archive) honoring project + tag + phase + search filter.

    Single roundtrip replacement for three /api/tasks calls in the frontend.
    """
    proj_clause, proj_params = _build_project_filter(project)
    tag_clause, tag_params = _build_tag_filter(tag)
    phase_clause, phase_params = _build_phase_filter(phase)

    base_params: list[object] = []
    base_where = " WHERE 1=1"
    if proj_clause:
        base_where += proj_clause
        base_params.extend(proj_params)
    if tag_clause:
        base_where += tag_clause
        base_params.extend(tag_params)
    if phase_clause:
        base_where += phase_clause
        base_params.extend(phase_params)
    if search:
        base_where += " AND (title LIKE ? OR COALESCE(description, '') LIKE ?)"
        like = f"%{search}%"
        base_params.extend([like, like])

    queries = {
        "open": " AND status = 'open' AND archived = 0",
        "done": " AND status = 'done' AND archived = 0",
        "archive": " AND archived = 1",
    }

    counts: dict[str, int] = {}
    with get_conn() as conn:
        for key, extra in queries.items():
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks" + base_where + extra,
                base_params,
            ).fetchone()
            counts[key] = int(row["c"])
    return JSONResponse(counts)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int) -> JSONResponse:
    """Single-task lookup. Used by FRIDAY for `#<id>` resolution."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        tags = _load_tags_for(conn, task_id)
    return JSONResponse(row_to_task(row, tags))


@app.post("/api/tasks", status_code=201)
def api_create_task(payload: TaskCreate) -> JSONResponse:
    norm_tags = _normalize_tags(payload.tags)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (project, title, description, phase)
            VALUES (?, ?, ?, ?)
            """,
            (payload.project, payload.title, payload.description, payload.phase),
        )
        new_id = int(cur.lastrowid)
        if norm_tags:
            _set_task_tags(conn, new_id, norm_tags)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
        tags = _load_tags_for(conn, new_id)
    return JSONResponse(row_to_task(row, tags), status_code=201)


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: TaskUpdate) -> JSONResponse:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Tags are stored in a separate table -- pop before SQL UPDATE.
    tags_raw = fields.pop("tags", None)

    # Auto-set completed_at when status changes
    if "status" in fields:
        if fields["status"] == "done":
            fields["completed_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            fields["completed_at"] = None

    if "archived" in fields:
        fields["archived"] = 1 if fields["archived"] else 0

    with get_conn() as conn:
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            params = [*fields.values(), task_id]
            cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", params)
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Task not found")
        else:
            # Tags-only PATCH: still verify the task exists.
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="Task not found")

        if tags_raw is not None:
            _set_task_tags(conn, task_id, _normalize_tags(tags_raw))

        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        tags = _load_tags_for(conn, task_id)
    return JSONResponse(row_to_task(row, tags))


@app.delete("/api/tasks/{task_id}", status_code=204)
def api_delete_task(task_id: int) -> None:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Task not found")
