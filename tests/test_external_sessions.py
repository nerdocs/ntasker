"""Sessions started outside ntasker (``/task`` in a terminal) -- registered by
the loader with the ``claude`` pid, alive as long as that process is."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner
from ntasker.app import app
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    claude_runner.EXTERNAL.clear()
    return TestClient(app, base_url=BASE)


def test_register_and_expose(client):
    t = client.post("/api/tasks", json={"title": "t", "project": "p"}).json()
    r = client.post(f"/api/claude/sessions/{t['id']}/external", json={"pid": os.getpid()})
    assert r.status_code == 200
    body = client.get("/api/claude/sessions").json()
    assert body["external"] == [t["id"]] and body["active"] == []
    assert body["projects"] == {str(t["id"]): "p"} and body["titles"] == {}


def test_dead_pid_is_forgotten(client, monkeypatch):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    client.post(f"/api/claude/sessions/{t['id']}/external", json={"pid": os.getpid()})
    monkeypatch.setattr(claude_runner, "_pid_alive", lambda pid: False)
    assert client.get("/api/claude/sessions").json()["external"] == []
    assert claude_runner.EXTERNAL == {}


def test_second_task_in_same_process_replaces_first(client):
    a = client.post("/api/tasks", json={"title": "a"}).json()["id"]
    b = client.post("/api/tasks", json={"title": "b"}).json()["id"]
    client.post(f"/api/claude/sessions/{a}/external", json={"pid": os.getpid()})
    client.post(f"/api/claude/sessions/{b}/external", json={"pid": os.getpid()})
    assert client.get("/api/claude/sessions").json()["external"] == [b]


def test_unknown_task_or_bad_pid(client):
    assert client.post("/api/claude/sessions/999/external", json={"pid": 1}).status_code == 404
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert client.post(f"/api/claude/sessions/{t['id']}/external", json={"pid": 0}).status_code == 422


SID = "11111111-2222-3333-4444-555555555555"


def test_session_id_is_stored_on_the_task(client, tmp_path):
    """A terminal session reports its own id -- the task can be resumed later."""
    t = client.post("/api/tasks", json={"title": "t"}).json()
    r = client.post(
        f"/api/claude/sessions/{t['id']}/external",
        json={"pid": os.getpid(), "session_id": SID, "cwd": str(tmp_path)},
    )
    assert r.status_code == 200 and r.json()["session_id"] == SID
    row = client.get(f"/api/tasks/{t['id']}").json()
    assert row["session_id"] == SID and row["session_cwd"] == str(tmp_path)
    assert claude_runner._stored_session(t["id"]) == (SID, str(tmp_path))


def test_registration_without_a_session_id_still_works(client):
    """An older loader (or no exported id) registers the busy state only."""
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert client.post(
        f"/api/claude/sessions/{t['id']}/external", json={"pid": os.getpid()}
    ).status_code == 200
    assert client.get(f"/api/tasks/{t['id']}").json()["session_id"] is None


def test_malformed_session_id_is_rejected(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    r = client.post(
        f"/api/claude/sessions/{t['id']}/external",
        json={"pid": os.getpid(), "session_id": "not-a-uuid"},
    )
    assert r.status_code == 422


def test_stale_session_cwd_falls_back(client, tmp_path):
    """A recorded directory that no longer exists must not steer a resume."""
    t = client.post("/api/tasks", json={"title": "t"}).json()
    client.post(
        f"/api/claude/sessions/{t['id']}/external",
        json={"pid": os.getpid(), "session_id": SID, "cwd": str(tmp_path / "gone")},
    )
    assert claude_runner._stored_session(t["id"]) == (SID, None)
