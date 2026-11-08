"""A team server and a real ``ntasker worker``, end to end over websockets.

A uvicorn server runs in a thread with ``auth_mode = password``; a
:class:`~ntasker.worker.Worker` connects with alice's token; a shell script
stands in for the ``claude`` CLI. The queue must start the task on the worker,
the browser terminal must relay keystrokes and output both ways, and a
member who is not the run's owner only watches.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from websockets.sync.client import connect as ws_connect

from ntasker import auth, claude_runner, taskqueue, workers
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import set_setting
from ntasker.worker import Worker

AGENT = """#!/bin/sh
echo "ready token=${NTASKER_TOKEN:+yes}"
read line
echo "got:$line"
sleep 0.2
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def team(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("NTASKER_DB", str(db))
    monkeypatch.delenv("NTASKER_AUTH_MODE", raising=False)
    set_db_path(db)
    init_db(db)
    agent = tmp_path / "fake-claude"
    agent.write_text(AGENT)
    agent.chmod(0o755)
    monkeypatch.setenv("NTASKER_CLAUDE_BIN", str(agent))
    with get_conn() as conn:
        alice = auth.create_user(conn, "alice", "pw", admin=False)
        bob = auth.create_user(conn, "bob", "pw")
        _id, alice_token = auth.create_token(conn, alice, "laptop")
        _id, bob_token = auth.create_token(conn, bob, "laptop")
        auth.create_user(conn, "root", "pw", admin=True)
    set_setting("auth_mode", "password")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert _wait(lambda: server.started)

    worker = Worker(f"http://127.0.0.1:{port}", alice_token, "laptop")
    wthread = threading.Thread(target=lambda: asyncio.run(worker.run(once=True)), daemon=True)
    wthread.start()
    assert _wait(lambda: workers.pick(alice) is not None)
    yield {"port": port, "alice": alice, "alice_token": alice_token, "bob": bob, "bob_token": bob_token}
    server.should_exit = True
    thread.join(5)
    wthread.join(5)
    claude_runner.SESSIONS.clear()
    workers.CONNECTED.clear()
    # Queue module state keyed by task id -- the next test's DB reuses the ids.
    taskqueue._running.clear()
    taskqueue._was_live.clear()


def _api(port, token, method, path, body=None):
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _terminal(port, token, task_id):
    ws = ws_connect(
        f"ws://127.0.0.1:{port}/ws/claude/{task_id}",
        additional_headers={"Authorization": f"Bearer {token}"},
    )
    ws.send(json.dumps({"type": "attach"}))
    return ws


def _read_until(ws, needle: bytes, timeout=10.0) -> bytes:
    import base64  # noqa: PLC0415

    seen = b""
    deadline = time.monotonic() + timeout
    while needle not in seen and time.monotonic() < deadline:
        msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        if msg["type"] == "output":
            seen += base64.b64decode(msg["data"])
        elif msg["type"] == "exit":
            break
    return seen


def test_queue_runs_task_on_the_assignees_worker(team):
    port, tok = team["port"], team["alice_token"]
    task = _api(port, tok, "POST", "/api/tasks", {"title": "remote run", "project": "shared"})
    _api(port, tok, "PUT", "/api/queue", {"ids": [task["id"]]})
    assert _wait(lambda: task["id"] in claude_runner.SESSIONS)
    sess = claude_runner.SESSIONS[task["id"]]
    assert sess.worker is not None and sess.worker.user_id == team["alice"]

    ws = _terminal(port, tok, task["id"])
    # The session's environment carries the worker's server token.
    assert b"ready token=yes" in _read_until(ws, b"ready")
    # bob (member via the admin) watches read-only: his keystrokes are dropped.
    with get_conn() as conn:
        conn.execute("INSERT INTO project_members VALUES ('shared', ?)", (team["bob"],))
    watcher = _terminal(port, team["bob_token"], task["id"])
    watcher.send(json.dumps({"type": "input", "data": "bob\r"}))
    time.sleep(0.3)
    ws.send(json.dumps({"type": "input", "data": "ping\r"}))
    out = _read_until(ws, b"got:")
    assert b"got:ping" in out and b"got:bob" not in out
    ws.close()
    watcher.close()

    # The run ends on the worker -> the server's session ends too, and the
    # forced session id + directory were recorded from the worker's report.
    assert _wait(lambda: not claude_runner.SESSIONS.get(task["id"], sess).alive)
    stored = _api(port, tok, "GET", f"/api/tasks/{task['id']}")
    assert stored["session_id"] and stored["session_cwd"]


def test_no_worker_means_the_task_waits(team):
    port, tok = team["port"], team["bob_token"]
    task = _api(port, tok, "POST", "/api/tasks", {"title": "nobody runs me"})
    queue = _api(port, tok, "PUT", "/api/queue", {"ids": [task["id"]]})
    time.sleep(2.5)  # one queue tick
    assert task["id"] not in claude_runner.SESSIONS
    queue = _api(port, tok, "GET", "/api/queue")
    assert queue["skipped"][str(task["id"])]["reason"] == "no_worker"
