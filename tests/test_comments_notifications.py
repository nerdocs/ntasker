"""Comments and the in-app notifications they and task events raise."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import auth, collab, taskqueue
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import set_setting

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.delenv("NTASKER_AUTH_MODE", raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    auth._attempts.clear()
    with get_conn() as conn:
        auth.create_user(conn, "alice", "pw", admin=True)
        auth.create_user(conn, "bob", "pw")
    set_setting("auth_mode", "password")

    def login(name):
        c = TestClient(app, base_url=BASE)
        c.post("/login", data={"username": name, "password": "pw"}, follow_redirects=False)
        c.me = c.get("/api/me").json()
        return c

    return login


def _kinds(client):
    return [n["kind"] for n in client.get("/api/notifications?unread=1").json()["items"]]


def test_comment_flow_notifies_the_other_side(team):
    alice, bob = team("alice"), team("bob")
    t = alice.post("/api/tasks", json={"title": "t", "assignee_id": bob.me["id"]}).json()
    assert _kinds(bob) == ["assigned"] and _kinds(alice) == []

    c = bob.post(f"/api/tasks/{t['id']}/comments", json={"body": "on it"}).json()
    assert c["author"] == "bob"
    assert _kinds(alice) == ["commented"] and _kinds(bob) == ["assigned"]

    assert [x["body"] for x in alice.get(f"/api/tasks/{t['id']}/comments").json()] == ["on it"]
    # Only the author (or an admin) edits; alice is admin here, so check bob vs a fresh comment.
    a = alice.post(f"/api/tasks/{t['id']}/comments", json={"body": "thanks"}).json()
    assert bob.patch(f"/api/comments/{a['id']}", json={"body": "x"}).status_code == 403
    assert bob.patch(f"/api/comments/{c['id']}", json={"body": "done soon"}).json()["edited_at"]
    assert bob.delete(f"/api/comments/{c['id']}").status_code == 204


def test_phase_and_done_events_and_mark_read(team):
    alice, bob = team("alice"), team("bob")
    t = alice.post("/api/tasks", json={"title": "t", "assignee_id": bob.me["id"]}).json()
    bob.patch(f"/api/tasks/{t['id']}", json={"phase": "review"})
    bob.patch(f"/api/tasks/{t['id']}", json={"status": "done"})
    assert _kinds(alice) == ["done", "review"]
    assert alice.post("/api/notifications/read", json={}).json()["unread"] == 0
    assert _kinds(alice) == []


def test_run_ended_notifies_watchers(team, monkeypatch):
    alice = team("alice")
    t = alice.post("/api/tasks", json={"title": "t"}).json()
    monkeypatch.setattr(taskqueue, "_was_live", {t["id"]})
    taskqueue._notify_ended(set())
    assert _kinds(alice) == ["run_ended"]


def test_local_mode_writes_no_notifications(tmp_path):
    path = tmp_path / "l.db"
    set_db_path(path)
    init_db(path)
    c = TestClient(app, base_url=BASE)
    t = c.post("/api/tasks", json={"title": "t"}).json()
    c.post(f"/api/tasks/{t['id']}/comments", json={"body": "note"})
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        assert len(collab.list_comments(conn, t["id"])) == 1
