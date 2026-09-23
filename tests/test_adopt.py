"""Adopting a session ntasker did not start: the task is pointed at an existing
conversation (id + directory) so the board can resume it."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, projects
from ntasker.app import app
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def client(tmp_path):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    claude_runner.EXTERNAL.clear()
    return TestClient(app, base_url=BASE)


def test_adopt_binds_id_and_directory(client, tmp_path):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    r = client.post(
        f"/api/claude/sessions/{t['id']}/adopt",
        json={"session_id": SID, "cwd": str(tmp_path)},
    )
    assert r.status_code == 200
    row = client.get(f"/api/tasks/{t['id']}").json()
    assert (row["session_id"], row["session_cwd"]) == (SID, str(tmp_path))


def test_adopt_without_a_pid_leaves_the_task_free(client, tmp_path):
    """An ended session is adoptable -- it must not mark the task busy."""
    t = client.post("/api/tasks", json={"title": "t"}).json()
    client.post(f"/api/claude/sessions/{t['id']}/adopt", json={"session_id": SID})
    assert client.get("/api/claude/sessions").json()["external"] == []


def test_adopt_with_a_pid_holds_the_task(client):
    """A session still running keeps the task busy, like the /task loader does."""
    t = client.post("/api/tasks", json={"title": "t"}).json()
    client.post(
        f"/api/claude/sessions/{t['id']}/adopt",
        json={"session_id": SID, "pid": os.getpid()},
    )
    assert client.get("/api/claude/sessions").json()["external"] == [t["id"]]


def test_adopt_rejects_unknown_task_and_bad_id(client):
    assert client.post(
        "/api/claude/sessions/999/adopt", json={"session_id": SID}
    ).status_code == 404
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert client.post(
        f"/api/claude/sessions/{t['id']}/adopt", json={"session_id": "nope"}
    ).status_code == 422


def test_project_name_prefers_a_known_project(client, tmp_path, monkeypatch):
    """A session in a subdirectory is filed under its project, not next to it."""
    monkeypatch.setattr(projects, "_resolve_projects_base", lambda: None)
    monkeypatch.setattr(projects.Path, "home", classmethod(lambda cls: tmp_path))
    client.post("/api/tasks", json={"title": "t", "project": "repo"})
    assert projects.name_for_dir(tmp_path / "repo" / "src" / "deep") == "repo"
    assert projects.name_for_dir(tmp_path / "other") == "other"
    assert projects.name_for_dir(tmp_path) is None
