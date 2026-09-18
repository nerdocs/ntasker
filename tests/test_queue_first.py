"""The task queue is the only way a session starts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, taskqueue
from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.settings import VALIDATORS, get_queue_enabled

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.delenv("NTASKER_QUEUE_ENABLED", raising=False)
    monkeypatch.delenv("NTASKER_QUICKTASKS_BYPASS_LANES", raising=False)
    taskqueue.QUICK.clear()
    taskqueue.LANELESS.clear()
    return TestClient(app, base_url=BASE)


def test_run_defaults_endpoint_is_gone(client):
    task = client.post("/api/tasks", json={"title": "t"}).json()
    assert client.get(f"/api/tasks/{task['id']}/claude-run/defaults").status_code == 404


def test_attach_without_live_session_does_not_spawn(client, monkeypatch):
    task = client.post("/api/tasks", json={"title": "t"}).json()

    def boom(*a, **kw):
        raise AssertionError("attach must not start a session")

    monkeypatch.setattr(claude_runner, "_start_session", boom)
    monkeypatch.setattr(claude_runner, "terminal_available", lambda spec: (True, None))
    headers = {"Host": "127.0.0.1:8766", "Origin": BASE}
    with client.websocket_connect(f"/ws/claude/{task['id']}", headers=headers) as ws:
        ws.send_json({"type": "attach"})
        assert ws.receive_json()["type"] == "error"


def test_quick_run_creates_wip_task_at_queue_end(client):
    other = client.post("/api/tasks", json={"title": "o", "project": "x"}).json()
    client.put("/api/queue", json={"ids": [other["id"]]})
    r = client.post("/api/projects/quick-run", json={"project": "x"})
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["phase"] == "wip" and task["project"] == "x"
    items = client.get("/api/queue").json()["items"]
    assert [t["id"] for t in items] == [other["id"], task["id"]]
    assert task["id"] in taskqueue.QUICK
    assert task["id"] in taskqueue.LANELESS   # quicktasks_bypass_lanes defaults on


def test_quick_run_with_bypass_off_is_a_laned_run(client):
    client.put("/api/settings/quicktasks_bypass_lanes", json={"value": "off"})
    task = client.post("/api/projects/quick-run", json={"project": "x", "prompt": "hi"}).json()
    assert task["id"] not in taskqueue.LANELESS


def test_quick_run_with_prompt_makes_prompt_the_task(client):
    prompt = "Fix the flaky login test " * 4   # longer than the derived-title cap
    r = client.post("/api/projects/quick-run", json={"project": "x", "prompt": prompt})
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["description"] == prompt.strip()
    assert task["title"].startswith("Fix the flaky login test") and task["title"].endswith("…")
    assert task["phase"] == "wip"
    assert task["id"] not in taskqueue.QUICK   # seeded run, not a blank quick run
    assert task["id"] in taskqueue.LANELESS
    assert task["id"] in [t["id"] for t in client.get("/api/queue").json()["items"]]


def test_quick_run_blank_prompt_is_plain_quick_run(client):
    task = client.post("/api/projects/quick-run", json={"project": "x", "prompt": "  "}).json()
    assert task["description"] is None and task["id"] in taskqueue.QUICK


def test_quick_run_rejects_empty_project(client):
    assert client.post("/api/projects/quick-run", json={"project": " "}).status_code == 400


def test_queue_enabled_defaults_on(client):
    assert get_queue_enabled() is True
    assert client.get("/api/queue").json()["enabled"] is True


def test_compact_seed_setting_is_gone():
    assert "compact_seed" not in VALIDATORS


def test_sessions_payload_has_no_agents(client):
    body = client.get("/api/claude/sessions").json()
    assert "agents" not in body
    assert set(body) == {"active", "waiting", "external", "projects", "titles"}


def test_enqueue_appends_and_keeps_existing_position(client):
    a = client.post("/api/tasks", json={"title": "a"}).json()["id"]
    b = client.post("/api/tasks", json={"title": "b"}).json()["id"]
    c = client.post("/api/tasks", json={"title": "c"}).json()["id"]
    taskqueue.set_queue([a, b])
    assert [int(r["id"]) for r in taskqueue.enqueue(c)] == [a, b, c]
    assert [int(r["id"]) for r in taskqueue.enqueue(a)] == [a, b, c]


def test_run_marks_task_wip_while_still_waiting(client):
    a = client.post("/api/tasks", json={"title": "a"}).json()["id"]
    assert client.get(f"/api/tasks/{a}").json()["phase"] == "planned"
    client.post("/api/queue/run", json={"id": a})
    assert client.get(f"/api/tasks/{a}").json()["phase"] == "wip"
