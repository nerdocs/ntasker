"""Directory locks: storage, normalisation, grant/release API, path check."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, locks
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.delenv("NTASKER_DIR_LOCKS", raising=False)
    # Project name -> tmp_path/<name>; keeps the lock key independent of the host.
    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(locks, "known_projects", lambda conn: {"x", "y", "z", "y/sub"})
    claude_runner.SESSIONS.clear()
    yield TestClient(app, base_url=BASE)
    claude_runner.SESSIONS.clear()


class _FakeSession:
    alive = True


def _live(task_id: int) -> None:
    claude_runner.SESSIONS[task_id] = _FakeSession()  # type: ignore[assignment]


def test_parse_dump_roundtrip():
    assert locks.parse(None) == [] and locks.parse("garbage") == [] and locks.parse('{"a":1}') == []
    assert locks.parse(locks.dump(["a", "b"])) == ["a", "b"]


def test_normalize_drops_own_project_and_dupes():
    assert locks.normalize([" y ", "x", "", "y", "z"], "x") == ["y", "z"]


def test_create_and_patch_normalise_locks(client):
    t = client.post("/api/tasks", json={"title": "a", "project": "x", "locks": ["x", "y", "y"]}).json()
    assert t["locks"] == ["y"]
    t = client.patch(f"/api/tasks/{t['id']}", json={"locks": ["z", "x"]}).json()
    assert t["locks"] == ["z"]
    t = client.patch(f"/api/tasks/{t['id']}", json={"locks": []}).json()
    assert t["locks"] == []


def test_grant_is_all_or_nothing_and_names_holder(client):
    a = client.post("/api/tasks", json={"title": "a", "project": "x"}).json()
    b = client.post("/api/tasks", json={"title": "b", "project": "y"}).json()
    _live(a["id"])
    _live(b["id"])
    r = client.post(f"/api/tasks/{b['id']}/locks", json={"projects": ["z", "x"]})
    assert r.status_code == 409 and f"#{a['id']}" in r.json()["detail"] and "x" in r.json()["detail"]
    assert client.get(f"/api/tasks/{b['id']}").json()["locks"] == []
    r = client.post(f"/api/tasks/{b['id']}/locks", json={"projects": ["z"]})
    assert r.status_code == 200 and r.json()["locks"] == ["z"]
    with get_conn() as conn:
        assert locks.parse(conn.execute("SELECT locks FROM tasks WHERE id = ?", (b["id"],)).fetchone()["locks"]) == ["z"]
    r = client.delete(f"/api/tasks/{b['id']}/locks/z")
    assert r.status_code == 200 and r.json()["locks"] == []


def test_patch_locks_on_running_task_conflicts(client):
    a = client.post("/api/tasks", json={"title": "a", "project": "x"}).json()
    b = client.post("/api/tasks", json={"title": "b", "project": "y"}).json()
    _live(a["id"])
    _live(b["id"])
    assert client.patch(f"/api/tasks/{b['id']}", json={"locks": ["x"]}).status_code == 409
    # not running -> no check, the worker decides at start
    c = client.post("/api/tasks", json={"title": "c", "project": "z"}).json()
    assert client.patch(f"/api/tasks/{c['id']}", json={"locks": ["x"]}).status_code == 200


def test_locks_check(client, tmp_path):
    a = client.post("/api/tasks", json={"title": "a", "project": "x", "locks": ["y"]}).json()
    holder = client.post("/api/tasks", json={"title": "h", "project": "z"}).json()
    _live(holder["id"])

    def check(path):
        return client.get("/api/locks/check", params={"task": a["id"], "path": path}).json()

    assert check(str(tmp_path / "x" / "f.py"))["allowed"] is True
    assert check(str(tmp_path / "y" / "deep" / "f.py"))["allowed"] is True
    refused = check(str(tmp_path / "z" / "f.py"))
    assert refused == {"allowed": False, "project": "z", "holder": holder["id"]}
    # paths belonging to no project always pass
    assert check(str(tmp_path / "elsewhere" / "f.py"))["allowed"] is True
    assert check("/tmp/whatever.txt")["allowed"] is True
    assert client.get("/api/locks/check", params={"task": 9999, "path": "/x"}).status_code == 404


def test_locks_check_off_switch(client, tmp_path):
    a = client.post("/api/tasks", json={"title": "a", "project": "x"}).json()
    client.put("/api/settings/dir_locks", json={"value": "off"})
    r = client.get("/api/locks/check", params={"task": a["id"], "path": str(tmp_path / "z" / "f")})
    assert r.json()["allowed"] is True


def test_project_for_path_skips_home_and_tmp(client, tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(locks, "known_projects", lambda conn: {"scratch", "x"})
    monkeypatch.setattr(
        locks, "resolve_dir",
        lambda name: tempfile.gettempdir() if name == "scratch" else str(tmp_path / name),
    )
    with get_conn() as conn:
        assert locks.project_for_path(conn, tempfile.gettempdir() + "/plan.md") is None
        assert locks.project_for_path(conn, str(tmp_path / "x" / "f")) == "x"


def test_project_for_path_longest_match(client, tmp_path):
    with get_conn() as conn:
        assert locks.project_for_path(conn, str(tmp_path / "y" / "sub" / "a.py")) == "y/sub"
        assert locks.project_for_path(conn, str(tmp_path / "y" / "other.py")) == "y"
        assert locks.project_for_path(conn, str(tmp_path / "yy" / "a.py")) is None
