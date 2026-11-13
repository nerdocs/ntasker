"""``ntasker connect``: one-time codes that connect a computer to a team server."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from ntasker import auth, cli
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import get_setting, set_setting

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
    set_setting("auth_mode", "password")
    c = TestClient(app, base_url=BASE)
    c.post("/login", data={"username": "alice", "password": "pw"}, follow_redirects=False)
    return c


def _code(url: str, secret: str) -> str:
    """The code exactly as settings.js builds it."""
    raw = base64.urlsafe_b64encode(json.dumps({"u": url, "c": secret}).encode()).decode().rstrip("=")
    return cli.CONNECT_CODE_PREFIX + raw


def test_code_is_spent_once(team):
    secret = team.post("/api/pairing-codes").json()["secret"]
    anon = TestClient(app, base_url=BASE)
    r = anon.post("/api/pair", json={"code": secret, "name": "laptop"})
    assert r.status_code == 200 and r.json()["username"] == "alice"
    token = r.json()["token"]
    assert anon.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()["username"] == "alice"
    assert anon.post("/api/pair", json={"code": secret, "name": "again"}).status_code == 400
    with get_conn() as conn:
        assert [t["name"] for t in auth.list_tokens(conn)] == ["laptop"]
        # Only the digest is stored.
        assert conn.execute("SELECT COUNT(*) FROM pairing_codes WHERE code_hash = ?", (secret,)).fetchone()[0] == 0


def test_expired_and_unknown_codes_fail_alike(team):
    secret = team.post("/api/pairing-codes").json()["secret"]
    with get_conn() as conn:
        conn.execute("UPDATE pairing_codes SET expires_at = datetime('now', '-1 second')")
    anon = TestClient(app, base_url=BASE)
    expired = anon.post("/api/pair", json={"code": secret, "name": "x"})
    unknown = anon.post("/api/pair", json={"code": "nope", "name": "x"})
    assert expired.status_code == unknown.status_code == 400
    assert expired.json() == unknown.json()


def test_pairing_is_rate_limited_and_needs_a_team_server(team, tmp_path):
    anon = TestClient(app, base_url=BASE)
    codes = [anon.post("/api/pair", json={"code": "x", "name": "x"}).status_code for _ in range(11)]
    assert codes[-1] == 429
    assert TestClient(app, base_url=BASE).post("/api/pairing-codes").status_code == 401
    set_setting("auth_mode", "off")
    assert anon.post("/api/pair", json={"code": "x", "name": "x"}).status_code == 404


def test_parse_connect_code_round_trip():
    assert cli.parse_connect_code(_code("https://tasks.example.org/", "s3cret")) == ("https://tasks.example.org", "s3cret")
    for bad in ("hello", "ntc1-!!!", _code("ftp://x", "s")):
        with pytest.raises((ValueError, KeyError, TypeError)):
            cli.parse_connect_code(bad)


def test_connect_refuses_plain_http_to_another_host(tmp_path, capsys):
    db = tmp_path / "w.db"
    assert cli.main(["--db", str(db), "connect", _code("http://tasks.example.org", "s")]) == 2
    assert "--insecure" in capsys.readouterr().err


def test_connect_stores_the_connection(tmp_path, monkeypatch):
    calls = []

    def api_call(base, method, path, body=None, timeout=5.0):
        calls.append((base, method, path, body))
        return 200, {"token": "nt_new", "username": "alice"}

    monkeypatch.setattr(cli, "_api_call", api_call)
    db = tmp_path / "w.db"
    rc = cli.main(["--db", str(db), "connect", _code("https://tasks.example.org", "s3cret"),
                   "--name", "laptop", "--no-service"])
    assert rc == 0
    assert calls == [("https://tasks.example.org", "POST", "/api/pair", {"code": "s3cret", "name": "laptop"})]
    assert (get_setting("server_url"), get_setting("server_token"), get_setting("worker_name")) == (
        "https://tasks.example.org", "nt_new", "laptop")
