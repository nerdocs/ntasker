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


def test_rebind_points_the_task_at_the_cleared_session(client):
    from ntasker.db import get_conn

    t = client.post("/api/tasks", json={"title": "t"}).json()
    sid = "0b6c1f7e-5a3d-4c2b-9e8f-1a2b3c4d5e6f"
    r = client.post(f"/api/claude/sessions/{t['id']}/rebind", json={"session_id": sid, "cwd": "/p/mine"})
    assert r.status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT session_id, session_cwd FROM tasks WHERE id = ?", (t["id"],)).fetchone()
    assert (row["session_id"], row["session_cwd"]) == (sid, "/p/mine")
    assert client.post("/api/claude/sessions/9999/rebind", json={"session_id": sid}).status_code == 404
    assert client.post(f"/api/claude/sessions/{t['id']}/rebind", json={"session_id": "x y"}).status_code == 422


def test_claim_held_by_another_live_session_is_refused(client):
    """A second terminal ``/task`` on a held task gets 409, the holder stays."""
    t = client.post("/api/tasks", json={"title": "t"}).json()["id"]
    client.post(
        f"/api/claude/sessions/{t}/external", json={"pid": os.getppid(), "socket": "uds:/a"}
    )
    r = client.post(f"/api/claude/sessions/{t}/external", json={"pid": os.getpid()})
    assert r.status_code == 409 and r.json()["held_by"] == {"socket": "uds:/a"}
    assert claude_runner.EXTERNAL[t] == os.getppid()
    # The holder itself may re-run /task.
    assert client.post(
        f"/api/claude/sessions/{t}/external", json={"pid": os.getppid()}
    ).status_code == 200


def test_claim_of_a_dead_holder_is_taken_over(client, monkeypatch):
    t = client.post("/api/tasks", json={"title": "t"}).json()["id"]
    client.post(f"/api/claude/sessions/{t}/external", json={"pid": os.getppid()})
    monkeypatch.setattr(claude_runner, "_pid_alive", lambda pid: False)
    assert client.post(
        f"/api/claude/sessions/{t}/external", json={"pid": os.getpid()}
    ).status_code == 200


def test_loader_shows_banner_and_skips_wip_when_held(monkeypatch, capsys):
    """The /task loader turns a 409 into a BELEGT banner and leaves the task alone."""
    import importlib.util
    import io
    import json
    import urllib.error
    from pathlib import Path

    path = Path(claude_runner.__file__).parent / "claude_assets/command/_ntasker_loader.py"
    spec = importlib.util.spec_from_file_location("_ntasker_loader", path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)

    def held(req, timeout=2):
        body = io.BytesIO(json.dumps({"held_by": {"socket": "uds:/a"}}).encode())
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, body)

    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.delenv("NTASKER_TASK_ID", raising=False)
    monkeypatch.setattr(loader.urllib.request, "urlopen", held)
    monkeypatch.setattr(loader, "load_via_server", lambda tid: {"id": 5, "title": "T", "status": "open"})
    wip = []
    monkeypatch.setattr(loader, "set_wip_via_server", wip.append)
    assert loader.main(["_ntasker_loader.py", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("> BELEGT") and "uds:/a" in out and wip == []
