"""Connected workers: the team members' machines that run the agent sessions.

On a team server (``auth_mode = password``) nothing runs on the server itself.
Each team member starts ``ntasker worker`` (see :mod:`ntasker.worker`), which
connects *out* to ``/ws/worker`` with their token. The queue starts a task on a
connected worker of the task's assignee; the session's terminal, hook state and
buffer live on the server as a normal :class:`~ntasker.claude_runner.TermSession`
whose I/O goes through the :class:`WorkerConn` (see that class).

Protocol, JSON text frames (``data`` = base64 bytes):

worker -> server
  ``hello {name, hostname, version, sessions: [{task_id, alive, buffer}]}`` once
  per connection -- ``sessions`` re-adopts runs that outlived a dropped link;
  ``started {task_id, cwd, baselines}`` / ``refused {task_id, reason}``;
  ``output {task_id, data}``; ``exit {task_id, code}``;
  ``transcript {task_id, session_id, offset, data}`` -- the agent's session
  transcript, appended from ``offset`` (0 = from scratch) into a mirror on the
  server (:func:`mirror_home`), so the Conversation pane reads it as if local;
  ``diff {task_id, diff}`` -- the run's Diff view, computed on the worker.

server -> worker
  ``start {spec}`` (:func:`ntasker.claude_runner.spawn_spec`);
  ``input {task_id, data}``; ``resize {task_id, rows, cols}``; ``stop {task_id}``;
  ``upload {task_id, name, data}`` (a file dropped onto the terminal).

A dropped link keeps the worker's runs for :data:`GRACE_SECONDS`; a worker
that reconnects in time takes them back, the rest end like a crashed session.
The worker never stops a run because the link dropped.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from ntasker import __version__, db
from ntasker.claude_runner import SESSION_ID_RE, SESSIONS, TermSession, feed_exit, feed_output, record_start
from ntasker.db import get_conn

#: How long a disconnected worker's runs wait for it to come back.
GRACE_SECONDS = 60.0


class WorkerConn:
    """One connected ``ntasker worker``.

    :meth:`send` is safe from any thread (a sync route in the threadpool stops
    a run, too): it hands the message to the loop, where one writer task
    drains the outbox into the socket in order.
    """

    def __init__(self, websocket: WebSocket, user_id: int, name: str, hostname: str) -> None:
        self.websocket = websocket
        self.user_id = user_id
        self.name = name
        self.hostname = hostname
        self.connected_at = time.time()
        self.loop = asyncio.get_running_loop()
        self.outbox: asyncio.Queue = asyncio.Queue()
        # Spawn specs sent but not yet answered, by task id (see :meth:`start`).
        self.pending: dict[int, dict] = {}

    def send(self, msg: dict) -> None:
        self.loop.call_soon_threadsafe(self.outbox.put_nowait, msg)

    def start(self, spec: dict) -> None:
        """Run ``spec`` on this worker. The session is registered right away,
        so the queue sees the task busy while the worker spawns it."""
        task_id = spec["task_id"]
        SESSIONS[task_id] = TermSession(task_id=task_id, worker=self)
        self.pending[task_id] = spec
        self.send({"type": "start", "spec": spec})

    def sessions(self) -> list[TermSession]:
        return [s for s in SESSIONS.values() if s.worker is self and s.alive]

    def describe(self) -> dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "hostname": self.hostname,
            "connected_at": self.connected_at,
            "runs": [s.task_id for s in self.sessions()],
        }


#: Connected workers by ``(user id, worker name)``.
CONNECTED: dict[tuple[int, str], WorkerConn] = {}


def pick(user_id: int | None) -> WorkerConn | None:
    """The worker a run of ``user_id``'s task goes to: their most recently connected one."""
    mine = [c for (uid, _name), c in CONNECTED.items() if uid == user_id]
    return max(mine, key=lambda c: c.connected_at, default=None)


def _session_of(conn: WorkerConn, task_id) -> TermSession | None:
    sess = SESSIONS.get(int(task_id)) if str(task_id).isdigit() else None
    return sess if sess is not None and sess.worker is conn else None


def _decode(data) -> bytes:
    try:
        return base64.b64decode(str(data or ""), validate=True)
    except (ValueError, binascii.Error):
        return b""


def _adopt(conn: WorkerConn, reported: list[dict]) -> None:
    """Take over the runs a (re)connecting worker still has.

    A run the server still knows (the link dropped) just changes connection;
    one it does not (the server restarted) is registered anew from the
    worker's replay buffer. Runs of this worker the server holds but the
    worker no longer reports have ended.
    """
    alive = set()
    for item in reported:
        task_id = int(item.get("task_id", 0))
        if not item.get("alive") or task_id <= 0:
            continue
        alive.add(task_id)
        sess = SESSIONS.get(task_id)
        if sess is None or not sess.alive:
            sess = TermSession(task_id=task_id, worker=conn, cwd=item.get("cwd"))
            SESSIONS[task_id] = sess
        sess.worker = conn
        # The worker's buffer is the complete tail, including what it printed
        # while the link was down.
        buffer = _decode(item.get("buffer"))
        if buffer:
            sess.buffer = bytearray(buffer)
    for sess in list(SESSIONS.values()):
        if (
            sess.alive and sess.worker is not None and sess.worker.user_id == conn.user_id
            and sess.worker.name == conn.name and sess.task_id not in alive
        ):
            feed_exit(sess, None)


def mirror_home(task_id: int) -> Path:
    """Where a remote run's transcript is mirrored: laid out like an agent home
    (``projects/<dir>/<session>.jsonl``), so :mod:`ntasker.transcript` reads it
    unchanged. Next to the database, one folder per task."""
    assert db.DB_PATH is not None
    return db.DB_PATH.parent / "mirror" / str(int(task_id))


def _mirror_transcript(task_id: int, msg: dict) -> None:
    session_id = str(msg.get("session_id") or "")
    if not re.match(SESSION_ID_RE, session_id):
        return
    path = mirror_home(task_id) / "projects" / "remote" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    offset = int(msg.get("offset") or 0)
    data = _decode(msg.get("data"))
    size = path.stat().st_size if path.exists() else 0
    if offset == 0:
        path.write_bytes(data)
    elif offset == size:
        with path.open("ab") as fh:
            fh.write(data)
    # Any other offset is a gap (a lost message): wait for the next full send.


def store_diff(task_id: int, diff) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO run_snapshots (task_id, diff, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(task_id) DO UPDATE SET diff = excluded.diff, updated_at = excluded.updated_at",
            (task_id, json.dumps(diff)),
        )


def stored_diff(task_id: int) -> dict | None:
    """The last Diff view a worker reported for ``task_id``, or ``None``."""
    with get_conn() as conn:
        row = conn.execute("SELECT diff FROM run_snapshots WHERE task_id = ?", (task_id,)).fetchone()
    return json.loads(row["diff"]) if row and row["diff"] else None


def _assigned_to(task_id, user_id: int) -> bool:
    if not str(task_id).isdigit():
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT assignee_id FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    return row is not None and row["assignee_id"] == user_id


def _dispatch(conn: WorkerConn, msg: dict) -> None:
    kind = msg.get("type")
    task_id = msg.get("task_id")
    if kind in ("transcript", "diff"):
        # Also arrive right after the run's exit, so they are checked against
        # the task's assignee rather than a live session.
        if _assigned_to(task_id, conn.user_id):
            if kind == "transcript":
                _mirror_transcript(int(task_id), msg)
            elif isinstance(msg.get("diff"), dict):
                store_diff(int(task_id), msg["diff"])
        return
    sess = _session_of(conn, task_id)
    if sess is None:
        return
    if kind == "output":
        feed_output(sess, _decode(msg.get("data")))
    elif kind == "started":
        sess.cwd = msg.get("cwd")
        spec = conn.pending.pop(sess.task_id, {})
        record_start(sess.task_id, spec, sess.cwd, msg.get("baselines"))
    elif kind == "refused":
        conn.pending.pop(sess.task_id, None)
        print(f"nTasker: worker {conn.name} refused task #{sess.task_id}: {msg.get('reason')}")
        feed_exit(sess, None)
    elif kind == "exit":
        code = msg.get("code")
        feed_exit(sess, int(code) if isinstance(code, int) else None)



def _orphan_later(conn: WorkerConn) -> None:
    """After the grace period, end the runs nobody took back."""

    def check() -> None:
        for sess in list(SESSIONS.values()):
            if sess.worker is conn and sess.alive:
                feed_exit(sess, None)

    conn.loop.call_later(GRACE_SECONDS, check)


async def serve_worker(websocket: WebSocket, user: dict) -> None:
    """The ``/ws/worker`` endpoint for one worker connection (already authenticated)."""
    await websocket.accept()
    try:
        hello = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError):
        return
    if hello.get("type") != "hello":
        await websocket.close(code=1002)
        return
    name = str(hello.get("name") or "worker")[:100]
    key = (user["id"], name)
    old = CONNECTED.get(key)
    if old is not None:  # the same machine again -- the new link wins
        with contextlib.suppress(Exception):
            await old.websocket.close(code=1000)
    conn = WorkerConn(websocket, user["id"], name, str(hello.get("hostname") or ""))
    CONNECTED[key] = conn
    _adopt(conn, hello.get("sessions") or [])
    conn.send({"type": "welcome", "server_version": __version__})

    async def pump() -> None:
        while True:
            await websocket.send_json(await conn.outbox.get())

    writer = asyncio.create_task(pump())
    try:
        while True:
            _dispatch(conn, await websocket.receive_json())
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        writer.cancel()
        if CONNECTED.get(key) is conn:
            del CONNECTED[key]
        if conn.sessions():
            _orphan_later(conn)


def connected() -> list[dict]:
    """``GET /api/workers``: who is connected, with their live runs."""
    return [c.describe() for c in CONNECTED.values()]
