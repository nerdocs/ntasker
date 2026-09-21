"""Stale Claude projects: flagged in /api/projects, deletable via the API."""

import json

from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import set_db_path
from ntasker.projects import (
    delete_claude_project,
    discover_claude_project_dirs,
    stale_claude_projects,
)

LOCAL = "127.0.0.1:8766"


def _session_dir(claude_home, cwd):
    d = claude_home / "projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")
    return d


def test_stale_detection_and_delete(tmp_path):
    home = tmp_path / "home"
    live = home / "live"
    live.mkdir(parents=True)
    (live / "README").write_text("x")
    gone = home / "gone"
    empty = home / "empty"
    empty.mkdir()
    claude_home = tmp_path / "claude"
    live_dir = _session_dir(claude_home, live)
    gone_dir = _session_dir(claude_home, gone)
    empty_dir = _session_dir(claude_home, empty)

    dirs = discover_claude_project_dirs(claude_home, home=home, base=None)
    assert set(dirs) == {"live", "gone", "empty"}
    assert stale_claude_projects(dirs) == {"gone", "empty"}

    assert delete_claude_project("gone", dirs) == 1
    assert not gone_dir.exists()
    assert live_dir.exists()

    assert delete_claude_project("empty", dirs) == 1
    assert not empty_dir.exists()
    assert not empty.exists()


def test_delete_project_api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "live").mkdir(parents=True)
    (home / "live" / "README").write_text("x")
    claude_home = tmp_path / "claude"
    _session_dir(claude_home, home / "live")
    gone_dir = _session_dir(claude_home, home / "gone")
    monkeypatch.setenv("NTASKER_CLAUDE_HOME", str(claude_home))
    monkeypatch.setattr("ntasker.projects.Path.home", staticmethod(lambda: home))
    set_db_path(tmp_path / "tasks.db")

    with TestClient(app, base_url=f"http://{LOCAL}") as client:
        assert client.post("/api/tasks", json={"title": "t", "project": "gone"}).status_code == 201

        projects = {p["name"]: p for p in client.get("/api/projects").json()}
        assert projects["gone"]["stale"] is True
        assert projects["gone"]["task_count"] == 1
        assert projects["live"]["stale"] is False

        # Live projects are never deletable.
        assert client.post("/api/projects/delete", json={"project": "live"}).status_code == 400

        resp = client.post("/api/projects/delete", json={"project": "gone"})
        assert resp.status_code == 200
        assert resp.json() == {"project": "gone", "removed_dirs": 1, "tasks": [1]}
        assert not gone_dir.exists()
        assert "gone" not in {p["name"] for p in client.get("/api/projects").json()}
        assert client.get("/api/tasks").json() == []
