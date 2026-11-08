"""Users, login and the current-user dependency.

ntasker runs in one of two modes (setting ``auth_mode``, ENV
``NTASKER_AUTH_MODE``):

- ``off`` (default, local mode): no login. Every request acts as the
  implicit user 1 (``local``), which the schema creates.
- ``password`` (server mode): every request must carry either a bearer token
  (``Authorization: Bearer <token>`` -- workers, the CLI, agent hooks) or the
  ``ntasker_session`` cookie the login page sets (browsers).

:class:`AuthMiddleware` resolves the user once per request -- HTTP and
WebSocket alike, so no route can forget the check -- and stores it in
``scope["state"]["user"]``. Routes read it via ``Depends(current_user)`` and
get a plain dict (``id``, ``username``, ``display_name``, ``is_admin``).

Passwords are hashed with stdlib scrypt; session ids and tokens are random
and stored as SHA-256 digests only, so a leaked DB yields no usable secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections import defaultdict, deque

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi import Depends, HTTPException

from ntasker.db import get_conn
from ntasker.i18n import _

LOCAL_USER_ID = 1

AUTH_MODES = ("off", "password")

SESSION_COOKIE = "ntasker_session"
SESSION_DAYS = 30

#: Paths reachable without a login: the login page itself and what it loads.
PUBLIC_PREFIXES = ("/login", "/healthz", "/static/", "/favicon")

_USER_COLUMNS = "id, username, display_name, is_admin, disabled"

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def auth_mode() -> str:
    """The active mode, ``off`` unless set to ``password``."""
    from ntasker.settings import get_setting  # noqa: PLC0415 -- settings imports db

    raw = (get_setting("auth_mode", env_var="NTASKER_AUTH_MODE") or "").strip().lower()
    return raw if raw in AUTH_MODES else "off"


def auth_enabled() -> bool:
    return auth_mode() != "off"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def user_dict(row: sqlite3.Row) -> dict:
    """JSON shape of a ``users`` row (never includes the password hash)."""
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "is_admin": bool(row["is_admin"]),
        "disabled": bool(row["disabled"]),
    }


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The user with ``user_id``, or None."""
    row = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_dict(row) if row else None


def find_user(conn: sqlite3.Connection, username: str) -> dict | None:
    """The user named ``username`` (case-insensitive), or None."""
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    return user_dict(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """All users, ordered by username."""
    rows = conn.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY username COLLATE NOCASE")
    return [user_dict(r) for r in rows]


def create_user(
    conn: sqlite3.Connection, username: str, password: str | None, *, admin: bool = False,
    display_name: str = "",
) -> int:
    """Insert a user; ``ValueError`` on an empty or taken name."""
    name = username.strip()
    if not name:
        raise ValueError(_("The user name must not be empty."))
    if find_user(conn, name) is not None:
        raise ValueError(_("User {name} already exists.").format(name=name))
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin) VALUES (?, ?, ?, ?)",
        (name, display_name.strip(), hash_password(password) if password else None, int(admin)),
    )
    return int(cur.lastrowid or 0)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    # A new password ends every browser session of that user.
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def set_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
    if disabled:
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def has_password_admin(conn: sqlite3.Connection) -> bool:
    """Whether someone could still log in as admin after switching auth on."""
    row = conn.execute(
        "SELECT 1 FROM users WHERE is_admin = 1 AND disabled = 0 AND password_hash IS NOT NULL"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """``scrypt$<salt>$<hash>`` (base64), stdlib only."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _scheme, salt_b64, digest_b64 = stored.split("$", 2)
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    """A new browser session for ``user_id``; returns the cookie value."""
    sid = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO auth_sessions (id, user_id) VALUES (?, ?)", (_digest(sid), user_id))
    return sid


def delete_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM auth_sessions WHERE id = ?", (_digest(sid),))


def create_token(conn: sqlite3.Connection, user_id: int, name: str) -> tuple[int, str]:
    """A new API token; returns ``(id, plaintext)``. The plaintext is never stored."""
    token = "nt_" + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash) VALUES (?, ?, ?)",
        (user_id, name.strip() or "token", _digest(token)),
    )
    return int(cur.lastrowid or 0), token


def list_tokens(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict]:
    """Tokens (never their secret), all users or one."""
    sql = (
        "SELECT t.id, t.name, t.created_at, t.last_used_at, t.revoked_at, u.username "
        "FROM api_tokens t JOIN users u ON u.id = t.user_id"
    )
    params: tuple = ()
    if user_id is not None:
        sql += " WHERE t.user_id = ?"
        params = (user_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY t.id", params)]


def revoke_token(conn: sqlite3.Connection, token_id: int, user_id: int | None = None) -> bool:
    """Revoke a token (only one of ``user_id`` when given). True if one was revoked."""
    sql = "UPDATE api_tokens SET revoked_at = datetime('now') WHERE id = ? AND revoked_at IS NULL"
    params: tuple = (token_id,)
    if user_id is not None:
        sql += " AND user_id = ?"
        params = (token_id, user_id)
    return conn.execute(sql, params).rowcount > 0


def _user_for_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT id, user_id FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (_digest(token),),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_tokens SET last_used_at = datetime('now') WHERE id = ?", (row["id"],))
    return get_user(conn, row["user_id"])


def _user_for_session(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute(
        "SELECT user_id FROM auth_sessions WHERE id = ? "
        f"AND last_seen_at > datetime('now', '-{SESSION_DAYS} days')",
        (_digest(sid),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE auth_sessions SET last_seen_at = datetime('now') WHERE id = ?", (_digest(sid),)
    )
    return get_user(conn, row["user_id"])


def resolve_user(headers: Headers, cookies: dict[str, str]) -> dict | None:
    """The user a request authenticates as (bearer first, then cookie), or None."""
    with get_conn() as conn:
        user = None
        scheme, _sep, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            user = _user_for_token(conn, token.strip())
        elif cookies.get(SESSION_COOKIE):
            user = _user_for_session(conn, cookies[SESSION_COOKIE])
    if user is None or user["disabled"]:
        return None
    return user


# ---------------------------------------------------------------------------
# Login rate limit
# ---------------------------------------------------------------------------

LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 60.0
_attempts: dict[str, deque[float]] = defaultdict(deque)


def login_allowed(client_ip: str) -> bool:
    """Count one attempt from ``client_ip``; False once it exceeds the window's budget."""
    now = time.monotonic()
    q = _attempts[client_ip]
    while q and now - q[0] > LOGIN_WINDOW:
        q.popleft()
    q.append(now)
    return len(q) <= LOGIN_ATTEMPTS


# ---------------------------------------------------------------------------
# Middleware + dependencies
# ---------------------------------------------------------------------------


def _is_public(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in PUBLIC_PREFIXES)


class AuthMiddleware:
    """Resolve the acting user for every HTTP and WebSocket request.

    With ``auth_mode = off`` it is the local user. Otherwise an
    unauthenticated request is refused: 401 JSON for the API, a redirect to
    ``/login`` for pages, close code 1008 for WebSockets. A plain ASGI
    middleware for the same reason as the origin guard -- it must see the
    WebSocket upgrades too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if not auth_enabled():
            user: dict | None = {"id": LOCAL_USER_ID}
        else:
            user = resolve_user(conn.headers, conn.cookies)
        if user is not None or _is_public(scope["path"]):
            scope.setdefault("state", {})["user_id"] = user["id"] if user else None
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008, "reason": "unauthenticated"})
            return
        if scope["path"].startswith("/api/") or scope.get("method") != "GET":
            body = json.dumps({"detail": "Not authenticated"}).encode()
            status, headers = 401, [(b"content-type", b"application/json")]
        else:
            body = b""
            status, headers = 303, [(b"location", b"/login")]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def current_user(conn: HTTPConnection) -> dict:
    """FastAPI dependency: the user the request acts as (HTTP and WebSocket)."""
    user_id = conn.scope.get("state", {}).get("user_id")
    if user_id is None:  # a public path reached without a login
        raise HTTPException(status_code=401, detail="Not authenticated")
    with get_conn() as db:
        user = get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    """FastAPI dependency: 403 unless the acting user is an admin."""
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail=_("Only an administrator may do this."))
    return user


# ---------------------------------------------------------------------------
# Project membership (who sees which tasks)
# ---------------------------------------------------------------------------
#
# A task is visible to admins, to members of its project, and -- with no
# project -- to everyone. Membership lives in ``project_members``; a project
# nobody has used yet is claimed by whoever creates its first task. In local
# mode the only user is an admin, so none of this ever filters anything.

#: SQL fragment limiting ``tasks`` to what a non-admin may see. One ``?``: the user id.
_VISIBLE_SQL = (
    " AND (tasks.project IS NULL OR tasks.project IN "
    "(SELECT project FROM project_members WHERE user_id = ?))"
)


def visibility_clause(user: dict) -> tuple[str, list]:
    """``(sql, params)`` to append to a ``WHERE`` over ``tasks``; empty for admins."""
    if user["is_admin"]:
        return "", []
    return _VISIBLE_SQL, [user["id"]]


def member_projects(conn: sqlite3.Connection, user_id: int) -> set[str]:
    rows = conn.execute("SELECT project FROM project_members WHERE user_id = ?", (user_id,))
    return {r["project"] for r in rows}


def can_see_project(conn: sqlite3.Connection, user: dict, project: str | None) -> bool:
    if user["is_admin"] or project is None:
        return True
    return project in member_projects(conn, user["id"])


def claim_project(conn: sqlite3.Connection, user: dict, project: str | None) -> None:
    """Let ``user`` put a task into ``project``, or raise 403.

    A project that has neither members nor tasks is new: its creator becomes
    its first member. A non-member may not write into an existing project.
    """
    if project is None:
        return
    taken = conn.execute(
        "SELECT 1 FROM project_members WHERE project = ? "
        "UNION SELECT 1 FROM tasks WHERE project = ? LIMIT 1",
        (project, project),
    ).fetchone()
    if taken is None:
        conn.execute(
            "INSERT OR IGNORE INTO project_members (project, user_id) VALUES (?, ?)",
            (project, user["id"]),
        )
    elif not can_see_project(conn, user, project):
        raise HTTPException(status_code=403, detail=_("You are not a member of this project."))


def add_member(conn: sqlite3.Connection, project: str | None, user_id: int) -> None:
    """Make ``user_id`` a member of ``project`` (assigning a task grants access to it)."""
    if project is not None:
        conn.execute(
            "INSERT OR IGNORE INTO project_members (project, user_id) VALUES (?, ?)",
            (project, user_id),
        )


def task_access(conn: HTTPConnection) -> None:
    """App-wide dependency: a route with a ``{task_id}`` path parameter 404s
    for a task the acting user may not see (404, not 403: no existence leak).
    Resolves the user only then -- public routes like ``/login`` have none."""
    raw = conn.path_params.get("task_id")
    if raw is None:
        return
    user = current_user(conn)
    if user["is_admin"]:
        return
    try:
        task_id = int(raw)
    except (TypeError, ValueError):
        return
    with get_conn() as db:
        row = db.execute("SELECT project FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None and not can_see_project(db, user, row["project"]):
            raise HTTPException(status_code=404, detail=_("Task not found"))


def check_task_ids(conn: sqlite3.Connection, user: dict, ids: list[int]) -> None:
    """404 when any of ``ids`` is a task ``user`` may not see (bulk endpoints)."""
    if user["is_admin"] or not ids:
        return
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(f"SELECT project FROM tasks WHERE id IN ({marks})", ids).fetchall()
    if any(not can_see_project(conn, user, r["project"]) for r in rows):
        raise HTTPException(status_code=404, detail=_("Task not found"))
