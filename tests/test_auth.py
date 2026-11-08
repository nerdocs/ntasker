"""Server mode (``auth_mode = password``): login, sessions, tokens, admin gates."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ntasker import auth, cli
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import set_setting

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("NTASKER_AUTH_MODE", raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    auth._attempts.clear()
    return path


@pytest.fixture
def server(db):
    """Auth on, with an admin ``alice`` and a plain user ``bob``."""
    with get_conn() as conn:
        auth.create_user(conn, "alice", "pw-alice", admin=True)
        auth.create_user(conn, "bob", "pw-bob")
    set_setting("auth_mode", "password")
    return db


def _client() -> TestClient:
    return TestClient(app, base_url=BASE)


def _login(name: str, password: str) -> TestClient:
    c = _client()
    r = c.post("/login", data={"username": name, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def test_local_mode_needs_no_login(db):
    assert _client().get("/api/me").json()["username"] == "local"
    assert _client().get("/login", follow_redirects=False).status_code == 303


def test_auth_mode_refused_without_password_admin(db):
    with pytest.raises(ValueError):
        set_setting("auth_mode", "password")


def test_unauthenticated_requests_are_refused(server):
    c = _client()
    assert c.get("/api/tasks").status_code == 401
    assert c.post("/api/tasks", json={"title": "x"}).status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/healthz").status_code == 200
    with pytest.raises(WebSocketDisconnect) as exc, c.websocket_connect("/ws/claude/1"):
        pass
    assert exc.value.code == 1008


def test_login_cookie_and_logout(server):
    assert _client().post("/login", data={"username": "bob", "password": "nope"}).status_code == 401
    c = _login("bob", "pw-bob")
    assert c.get("/api/me").json()["username"] == "bob"
    t = c.post("/api/tasks", json={"title": "mine"}).json()
    assert t["created_by"] == t["assignee_id"] == c.get("/api/me").json()["id"]
    c.post("/logout", follow_redirects=False)
    assert c.get("/api/me").status_code == 401


def test_bearer_token_and_revoke(server):
    with get_conn() as conn:
        bob = auth.find_user(conn, "bob")
        token_id, token = auth.create_token(conn, bob["id"], "laptop")
    headers = {"Authorization": f"Bearer {token}"}
    assert _client().get("/api/me", headers=headers).json()["username"] == "bob"
    with get_conn() as conn:
        assert auth.list_tokens(conn, bob["id"])[0]["last_used_at"] is not None
        auth.revoke_token(conn, token_id)
    assert _client().get("/api/me", headers=headers).status_code == 401


def test_disabled_user_loses_access(server):
    c = _login("bob", "pw-bob")
    with get_conn() as conn:
        auth.set_disabled(conn, auth.find_user(conn, "bob")["id"], True)
    assert c.get("/api/me").status_code == 401


def test_login_rate_limit(server):
    c = _client()
    codes = [c.post("/login", data={"username": "bob", "password": "x"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_admin_gates(server):
    bob = _login("bob", "pw-bob")
    assert bob.put("/api/settings/dir_locks", json={"value": "off"}).status_code == 403
    alice = _login("alice", "pw-alice")
    assert alice.put("/api/settings/dir_locks", json={"value": "off"}).status_code == 200


def test_password_hash_roundtrip():
    stored = auth.hash_password("s3cret")
    assert auth.verify_password("s3cret", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("s3cret", None)


def test_cli_user_and_token(db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("pw\n"))
    assert cli.main(["--db", str(db), "user", "add", "carol", "--admin"]) == 0
    assert cli.main(["--db", str(db), "token", "create", "carol", "--name", "laptop"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    set_setting("auth_mode", "password")
    assert _client().get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()["username"] == "carol"


def test_serve_refuses_lan_bind_without_auth(db, capsys):
    assert cli.main(["--db", str(db), "serve", "--host", "192.0.2.1"]) == 2
    assert "--allow-no-auth" in capsys.readouterr().err
