"""Draft tasks: parked ideas that are never started -- by anyone."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, cli, taskqueue
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


def test_draft_roundtrip_and_default(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert t["draft"] is False
    d = client.post("/api/tasks", json={"title": "d", "draft": True}).json()
    assert d["draft"] is True
    assert client.patch(f"/api/tasks/{d['id']}", json={"draft": False}).json()["draft"] is False


def test_draft_cannot_be_queued(client):
    d = client.post("/api/tasks", json={"title": "d", "draft": True}).json()
    r = client.post("/api/queue/run", json={"id": d["id"]})
    assert r.status_code == 409
    assert client.put("/api/queue", json={"ids": [d["id"]]}).json()["items"] == []
    assert client.get(f"/api/tasks/{d['id']}").json()["phase"] == "planned"   # no mark_wip


def test_setting_draft_dequeues(client):
    a = client.post("/api/tasks", json={"title": "a"}).json()
    b = client.post("/api/tasks", json={"title": "b"}).json()
    client.put("/api/queue", json={"ids": [a["id"], b["id"]]})
    t = client.patch(f"/api/tasks/{a['id']}", json={"draft": True}).json()
    assert t["draft"] is True and t["queue_order"] is None
    assert [i["id"] for i in client.get("/api/queue").json()["items"]] == [b["id"]]


def test_worker_retires_draft_flagged_behind_its_back(client, monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(
        taskqueue, "start_detached_session", lambda tid, seed, **kw: started.append(tid) or True
    )
    a = client.post("/api/tasks", json={"title": "a"}).json()
    client.put("/api/queue", json={"ids": [a["id"]]})
    with get_conn() as conn:   # direct DB write, bypassing the API's dequeue
        conn.execute("UPDATE tasks SET draft = 1 WHERE id = ?", (a["id"],))
    taskqueue.tick()
    assert started == []
    assert taskqueue.load_queue() == []


def test_spawn_refuses_draft(client):
    d = client.post("/api/tasks", json={"title": "d", "draft": True}).json()
    with pytest.raises(claude_runner.DraftTaskError):
        claude_runner._start_session(d["id"])
    assert claude_runner.start_detached_session(d["id"], "seed") is False
    assert client.get(f"/api/tasks/{d['id']}").json()["phase"] == "planned"


def test_cli_draft_flags(db, client, capsys):
    ntasker = ["--db", str(db)]
    assert cli.main([*ntasker, "add", "--title", "d", "--draft"]) == 0
    tid = int(capsys.readouterr().out.strip().split()[0].lstrip("#"))
    assert client.get(f"/api/tasks/{tid}").json()["draft"] is True
    assert cli.main([*ntasker, "queue", "add", str(tid)]) == 2
    assert f"#{tid}" in capsys.readouterr().err   # refused with a reason (locale-dependent text)
    assert cli.main([*ntasker, "patch", str(tid), "--draft", "false"]) == 0
    assert cli.main([*ntasker, "queue", "add", str(tid)]) == 0
    assert cli.main([*ntasker, "patch", str(tid), "--draft", "true"]) == 0
    assert taskqueue.load_queue() == []
