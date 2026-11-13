"""Task ownership (creator / assignee) and optimistic locking via ``version``."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=BASE)


def _add_user(db, username: str, disabled: int = 0) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "INSERT INTO users (username, disabled) VALUES (?, ?)", (username, disabled)
        ).lastrowid


def test_local_user_owns_everything(client):
    assert client.get("/api/me").json()["username"] == "local"
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert (t["created_by"], t["assignee_id"], t["version"]) == (1, 1, 0)


def test_insert_without_owner_defaults_to_local_user(db, client):
    # CLI / triage inserts do not name users -- the column default covers them.
    with sqlite3.connect(db) as conn:
        task_id = conn.execute("INSERT INTO tasks (title) VALUES ('cli')").lastrowid
    t = client.get(f"/api/tasks/{task_id}").json()
    assert (t["created_by"], t["assignee_id"]) == (1, 1)


def test_init_db_is_idempotent(db):
    init_db(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_assignee_roundtrip_and_validation(db, client):
    bob = _add_user(db, "bob")
    gone = _add_user(db, "gone", disabled=1)
    t = client.post("/api/tasks", json={"title": "t", "assignee_id": bob}).json()
    assert t["assignee_id"] == bob
    assert client.post("/api/tasks", json={"title": "t", "assignee_id": gone}).status_code == 400
    assert client.patch(f"/api/tasks/{t['id']}", json={"assignee_id": 999}).status_code == 400
    assert client.patch(f"/api/tasks/{t['id']}", json={"assignee_id": 1}).json()["assignee_id"] == 1
    assert [u["username"] for u in client.get("/api/users").json()] == ["bob", "local"]


def test_stale_version_is_refused(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    r = client.patch(f"/api/tasks/{t['id']}", json={"title": "a", "version": 0})
    assert r.status_code == 200 and r.json()["version"] == 1
    r = client.patch(f"/api/tasks/{t['id']}", json={"title": "b", "version": 0})
    assert r.status_code == 409
    assert r.json()["task"]["title"] == "a"
    # Without a version: last write wins, the counter still moves.
    r = client.patch(f"/api/tasks/{t['id']}", json={"title": "c"})
    assert r.status_code == 200 and r.json()["version"] == 2


def test_missing_task_stays_404(client):
    assert client.patch("/api/tasks/999", json={"title": "x", "version": 0}).status_code == 404
