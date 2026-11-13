"""Project membership: who sees and edits which tasks on a team server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ntasker import auth
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
        auth.create_user(conn, "admin", "pw", admin=True)
        auth.create_user(conn, "alice", "pw")
        auth.create_user(conn, "bob", "pw")
    set_setting("auth_mode", "password")

    def login(name):
        c = TestClient(app, base_url=BASE)
        assert c.post("/login", data={"username": name, "password": "pw"}, follow_redirects=False).status_code == 303
        return c

    return login


def test_non_member_sees_nothing_of_a_project(team):
    alice, bob, admin = team("alice"), team("bob"), team("admin")
    secret = alice.post("/api/tasks", json={"title": "secret", "project": "a-proj", "tags": ["x"]}).json()
    shared = alice.post("/api/tasks", json={"title": "open to all"}).json()  # no project

    assert [t["title"] for t in bob.get("/api/tasks").json()] == ["open to all"]
    assert bob.get(f"/api/tasks/{secret['id']}").status_code == 404
    assert bob.patch(f"/api/tasks/{secret['id']}", json={"title": "x"}).status_code == 404
    assert bob.get(f"/api/tasks/{shared['id']}").status_code == 200
    assert "a-proj" not in [p["name"] for p in bob.get("/api/projects").json()]
    assert bob.get("/api/stats").json()["open"] == 1
    assert {t["name"]: t["total_count"] for t in bob.get("/api/tags").json()}["x"] == 0
    # Writing into someone else's project is refused, too.
    assert bob.post("/api/tasks", json={"title": "sneak", "project": "a-proj"}).status_code == 403

    assert {t["title"] for t in admin.get("/api/tasks").json()} == {"secret", "open to all"}
    assert "a-proj" in [p["name"] for p in alice.get("/api/projects").json()]


def test_membership_via_admin_and_assignment(team):
    alice, bob, admin = team("alice"), team("bob"), team("admin")
    t = alice.post("/api/tasks", json={"title": "t", "project": "p"}).json()
    bob_id = bob.get("/api/me").json()["id"]

    r = admin.put("/api/project-members", json={"project": "p", "user_ids": [bob_id]})
    assert r.status_code == 200
    assert bob.get(f"/api/tasks/{t['id']}").status_code == 200
    admin.put("/api/project-members", json={"project": "p", "user_ids": []})
    assert bob.get(f"/api/tasks/{t['id']}").status_code == 404
    assert bob.get("/api/project-members").status_code == 403

    # Assigning a task to bob lets him see it.
    alice.put("/api/project-members", json={"project": "p", "user_ids": []})  # not an admin: refused
    admin.put("/api/project-members", json={"project": "p", "user_ids": [alice.get("/api/me").json()["id"]]})
    alice.patch(f"/api/tasks/{t['id']}", json={"assignee_id": bob_id})
    assert bob.get(f"/api/tasks/{t['id']}").status_code == 200


def test_terminal_websocket_refused_for_non_member(team):
    alice, bob = team("alice"), team("bob")
    t = alice.post("/api/tasks", json={"title": "t", "project": "p"}).json()
    with pytest.raises(WebSocketDisconnect), bob.websocket_connect(f"/ws/claude/{t['id']}"):
        pass


def test_queue_keeps_entries_the_user_cannot_see(team):
    alice, bob, admin = team("alice"), team("bob"), team("admin")
    a1 = alice.post("/api/tasks", json={"title": "a1", "project": "pa"}).json()["id"]
    b1 = bob.post("/api/tasks", json={"title": "b1", "project": "pb"}).json()["id"]
    b2 = bob.post("/api/tasks", json={"title": "b2", "project": "pb"}).json()["id"]
    admin.put("/api/queue", json={"ids": [b1, a1, b2]})

    assert [t["id"] for t in alice.get("/api/queue").json()["items"]] == [a1]
    # Alice empties "her" queue -- bob's entries stay where they were.
    alice.put("/api/queue", json={"ids": []})
    assert [t["id"] for t in admin.get("/api/queue").json()["items"]] == [b1, b2]
    # Bob reorders his two; alice's re-added entry lands at the end.
    alice.put("/api/queue", json={"ids": [a1]})
    bob.put("/api/queue", json={"ids": [b2, b1]})
    assert [t["id"] for t in admin.get("/api/queue").json()["items"]] == [b2, b1, a1]
    assert bob.put("/api/queue", json={"ids": [a1]}).status_code == 404
