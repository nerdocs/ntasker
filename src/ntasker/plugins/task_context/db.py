"""Schema and row helpers for task-context attachments (``task_context`` table)."""

from __future__ import annotations

import os
import sqlite3

# Workspace context attached to a task: the skills, notes, team personas
# and generated documents an agent should have in hand when it starts.
# Paths, not copies -- the file stays the single source of truth and keeps
# being editable in Obsidian or on the workspace page. A row whose file
# was moved away simply stops resolving; the attachment is then shown as
# missing rather than silently dropped.
SCHEMA = """
CREATE TABLE IF NOT EXISTS task_context (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    -- One of CONTEXT_KINDS: skill | note | member | doc | file | mcp.
    -- mcp (an MCP server from ~/.claude.json) stores ``mcp://<name>`` -- not
    -- a file. file is any path on this machine the user pointed at
    -- explicitly; it is the one kind that is not confined to the workspace
    -- directories.
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    label      TEXT NOT NULL,
    -- Free-form "why is this attached", surfaced to the agent verbatim.
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- The same file attached twice to one task is always a mistake.
    UNIQUE (task_id, path)
);
CREATE INDEX IF NOT EXISTS idx_task_context_task ON task_context(task_id);
"""

#: What kind of workspace file an attachment points at. Purely descriptive
#: -- it drives the icon in the UI and the wording in the agent briefing,
#: never any filesystem behaviour. Rows of any other kind (a fork's
#: ``brain`` notes, say) are ignored on read.
CONTEXT_KINDS: tuple[str, ...] = ("skill", "note", "member", "doc", "file", "mcp")

#: ``task_context.path`` prefix of an MCP server attachment (``mcp://<name>``).
MCP_SCHEME = "mcp://"
#: Path prefixes that are pointers, not files on disk.
REMOTE_SCHEMES = (MCP_SCHEME,)

_SELECT = "SELECT task_id, id, kind, path, label, note, created_at FROM task_context"
_KIND_FILTER = " AND kind IN (" + ", ".join("?" for _ in CONTEXT_KINDS) + ")"


def load_context_for(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """Return a task's attachments, oldest first.

    ``exists`` is resolved on read rather than stored: the files live in
    cloud folders and vaults that move around, and a stale flag in the DB
    would be wrong more often than right.
    """
    rows = conn.execute(
        f"{_SELECT} WHERE task_id = ?{_KIND_FILTER} ORDER BY id ASC",
        (task_id, *CONTEXT_KINDS),
    ).fetchall()
    return [_context_row(r) for r in rows]


def load_context_bulk(conn: sqlite3.Connection, task_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk lookup: task_id -> list of attachment dicts."""
    if not task_ids:
        return {}
    placeholders = ", ".join("?" for _ in task_ids)
    rows = conn.execute(
        f"{_SELECT} WHERE task_id IN ({placeholders}){_KIND_FILTER} ORDER BY id ASC",
        (*task_ids, *CONTEXT_KINDS),
    ).fetchall()
    out: dict[int, list[dict]] = {tid: [] for tid in task_ids}
    for r in rows:
        out[int(r["task_id"])].append(_context_row(r))
    return out


def _context_row(row: sqlite3.Row) -> dict:
    """Shape one ``task_context`` row for the API."""
    path = row["path"]
    # An MCP server is remote -- there is no file to go missing locally. It
    # reports as present; a removed server surfaces at briefing time.
    remote = path.startswith(REMOTE_SCHEMES)
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "kind": row["kind"],
        "path": path,
        "label": row["label"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "exists": True if remote else os.path.exists(path),
        "remote": remote,
        # A "file" attachment may point at a folder; the UI icons it so.
        "is_dir": False if remote else os.path.isdir(path),
    }


def add_context(
    conn: sqlite3.Connection, task_id: int, kind: str, path: str, label: str, note: str = ""
) -> dict:
    """Attach one entry to a task; re-attaching the same path updates in place.

    ``INSERT .. ON CONFLICT`` rather than a failure, because attaching a
    file that is already attached is how a user edits its note -- refusing
    would make them detach and re-add for a one-word change.
    """
    conn.execute(
        """
        INSERT INTO task_context (task_id, kind, path, label, note)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (task_id, path) DO UPDATE SET
            kind = excluded.kind,
            label = excluded.label,
            note = excluded.note
        """,
        (task_id, kind, path, label, note or None),
    )
    row = conn.execute(
        f"{_SELECT} WHERE task_id = ? AND path = ?", (task_id, path)
    ).fetchone()
    return _context_row(row)


def remove_context(conn: sqlite3.Connection, task_id: int, context_id: int) -> bool:
    """Detach one attachment. Returns False if it was not on this task.

    Detaching never touches the file itself -- the row is a pointer, and
    dropping a pointer is not a delete.
    """
    cur = conn.execute(
        "DELETE FROM task_context WHERE id = ? AND task_id = ?", (context_id, task_id)
    )
    return cur.rowcount > 0
