"""Comments on tasks and the in-app notifications they (and task events) raise.

Who hears about a task: its creator, its assignee and everyone who commented
on it -- never the person who caused the event. Kinds:

- ``assigned``: the task was assigned to you.
- ``commented``: someone commented.
- ``review`` / ``done``: the task moved to review / was marked done.
- ``run_ended``: its agent session ended without finishing the task.

Notifications are rows in ``notifications`` (no email, no push); the topnav
bell polls them. In local mode there is nobody to tell, so nothing is written.
"""

from __future__ import annotations

import sqlite3

from ntasker import auth

KINDS = ("assigned", "commented", "review", "done", "run_ended")


def watchers(conn: sqlite3.Connection, task_id: int) -> set[int]:
    """Users who follow ``task_id``: creator, assignee, commenters."""
    row = conn.execute("SELECT created_by, assignee_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return set()
    ids = {row["created_by"], row["assignee_id"]}
    ids |= {r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM comments WHERE task_id = ?", (task_id,))}
    return {i for i in ids if i is not None}


def notify(
    conn: sqlite3.Connection, task_id: int, kind: str, actor_id: int | None,
    recipients: set[int] | None = None,
) -> None:
    """Write one ``kind`` notification per watcher (or ``recipients``), minus the actor."""
    if not auth.auth_enabled():
        return
    targets = (watchers(conn, task_id) if recipients is None else recipients) - {actor_id}
    conn.executemany(
        "INSERT INTO notifications (user_id, task_id, actor_id, kind) VALUES (?, ?, ?, ?)",
        [(uid, task_id, actor_id, kind) for uid in sorted(targets)],
    )


def comment_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "user_id": row["user_id"],
        "author": row["author"],
        "body": row["body"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
    }


_COMMENT_SELECT = (
    "SELECT c.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS author "
    "FROM comments c LEFT JOIN users u ON u.id = c.user_id"
)


def list_comments(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    rows = conn.execute(f"{_COMMENT_SELECT} WHERE c.task_id = ? ORDER BY c.id", (task_id,))
    return [comment_dict(r) for r in rows]


def get_comment(conn: sqlite3.Connection, comment_id: int) -> dict | None:
    row = conn.execute(f"{_COMMENT_SELECT} WHERE c.id = ?", (comment_id,)).fetchone()
    return comment_dict(row) if row else None


def add_comment(conn: sqlite3.Connection, task_id: int, user_id: int, body: str) -> dict:
    """Store a comment and notify the task's watchers."""
    cur = conn.execute(
        "INSERT INTO comments (task_id, user_id, body) VALUES (?, ?, ?)", (task_id, user_id, body)
    )
    notify(conn, task_id, "commented", user_id)
    return get_comment(conn, int(cur.lastrowid or 0)) or {}


def list_notifications(conn: sqlite3.Connection, user_id: int, unread_only: bool, limit: int = 20) -> list[dict]:
    sql = (
        "SELECT n.id, n.task_id, n.kind, n.created_at, n.read_at, t.title, "
        "COALESCE(NULLIF(u.display_name, ''), u.username) AS actor "
        "FROM notifications n LEFT JOIN tasks t ON t.id = n.task_id "
        "LEFT JOIN users u ON u.id = n.actor_id WHERE n.user_id = ?"
    )
    if unread_only:
        sql += " AND n.read_at IS NULL"
    rows = conn.execute(sql + " ORDER BY n.id DESC LIMIT ?", (user_id, limit))
    return [dict(r) for r in rows]


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)
    ).fetchone()
    return int(row[0])


def mark_read(conn: sqlite3.Connection, user_id: int, ids: list[int] | None) -> None:
    """Mark ``ids`` (or all, with None) of ``user_id`` as read."""
    sql = "UPDATE notifications SET read_at = datetime('now') WHERE user_id = ? AND read_at IS NULL"
    params: list = [user_id]
    if ids is not None:
        if not ids:
            return
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params += ids
    conn.execute(sql, params)
