"""Inbox proposals: schema, invisibility to task consumers, and the start guards."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, taskqueue
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.delenv("NTASKER_QUEUE_ENABLED", raising=False)
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: [])
    monkeypatch.setattr(taskqueue, "_runnable_agents", lambda: {"claude"})
    taskqueue._running.clear()
    taskqueue._booted = True
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=BASE)


def _columns(path, table):
    with sqlite3.connect(path) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _propose(title="p", project="x"):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, project, proposed, triage) VALUES (?, ?, 1, ?)",
            (title, project, '{"raw": "note"}'),
        )
        return int(cur.lastrowid)


# --- schema ---------------------------------------------------------------

def test_init_db_twice_is_idempotent(tmp_path):
    path = tmp_path / "fresh.db"
    init_db(path)
    init_db(path)
    assert {"proposed", "triage"} <= set(_columns(path, "tasks"))
    assert {"inbox", "project_summaries", "triage_examples"} <= _tables(path)


def test_init_db_migrates_pre_inbox_database(tmp_path):
    path = tmp_path / "old.db"
    init_db(path)
    with sqlite3.connect(path) as conn:   # roll the file back to the pre-inbox shape
        conn.execute("ALTER TABLE tasks DROP COLUMN proposed")
        conn.execute("ALTER TABLE tasks DROP COLUMN triage")
        for table in ("inbox", "project_summaries", "triage_examples"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("INSERT INTO tasks (title) VALUES ('legacy')")
    init_db(path)

    assert {"proposed", "triage"} <= set(_columns(path, "tasks"))
    assert {"inbox", "project_summaries", "triage_examples"} <= _tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT proposed, triage FROM tasks").fetchone() == (0, None)


def test_task_json_carries_proposed_and_triage(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert t["proposed"] is False and t["triage"] is None
    pid = _propose()
    p = client.get(f"/api/tasks/{pid}").json()
    assert p["proposed"] is True and p["triage"] == {"raw": "note"}


# --- invisibility -----------------------------------------------------------

def test_proposal_is_absent_from_task_lists_and_counts(client):
    client.post("/api/tasks", json={"title": "real", "project": "x"})
    pid = _propose(project="x")
    ids = [t["id"] for t in client.get("/api/tasks").json()]
    assert pid not in ids and len(ids) == 1
    assert client.get("/api/tasks", params={"status": "open"}).json()[0]["proposed"] is False
    stats = client.get("/api/stats").json()
    assert stats["open"] == 1 and stats["inbox"] == 1
    x = next(p for p in client.get("/api/projects").json() if p["name"] == "x")
    assert x["open_count"] == 1
    assert client.get("/api/phases").json()[0]["open_count"] == 1
    normal = next(p for p in client.get("/api/priorities").json() if p["value"] == "normal")
    assert normal["open_count"] == 1


def test_stats_inbox_counts_pending_and_failed_rows(client):
    with get_conn() as conn:
        conn.execute("INSERT INTO inbox (text, status) VALUES ('a', 'pending')")
        conn.execute("INSERT INTO inbox (text, status, error) VALUES ('b', 'failed', 'x')")
        conn.execute("INSERT INTO inbox (text, status) VALUES ('c', 'triaged')")
    assert client.get("/api/stats").json()["inbox"] == 2


# --- guards -----------------------------------------------------------------

def test_proposal_cannot_be_queued(client):
    pid = _propose()
    assert client.put("/api/queue", json={"ids": [pid]}).json()["items"] == []
    r = client.post("/api/queue/run", json={"id": pid})
    assert r.status_code == 409
    assert client.get(f"/api/tasks/{pid}").json()["phase"] == "planned"


def test_worker_retires_proposal_flagged_behind_its_back(client, monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(
        taskqueue, "start_detached_session", lambda tid, seed, **kw: started.append(tid) or True
    )
    a = client.post("/api/tasks", json={"title": "a"}).json()
    client.put("/api/queue", json={"ids": [a["id"]]})
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET proposed = 1 WHERE id = ?", (a["id"],))
    taskqueue.tick()
    assert started == []
    assert taskqueue.load_queue() == []


def test_spawn_refuses_proposal(client):
    pid = _propose()
    with pytest.raises(claude_runner.DraftTaskError):
        claude_runner._start_session(pid)
    assert claude_runner.start_detached_session(pid, "seed") is False
