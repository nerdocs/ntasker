"""Tests for the Claude plugin's subscription limits (``/api/claude/usage`` + topbar widget)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from ntasker import plugins
from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.plugins.claude import usage

BASE = "http://127.0.0.1:8766"
PAYLOAD = {
    "five_hour": {"utilization": 11.0, "resets_at": "2026-09-25T18:50:00+00:00", "limit_dollars": None},
    "seven_day": {"utilization": 90.0, "resets_at": "2026-09-25T15:00:00+00:00"},
    "seven_day_opus": None,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    monkeypatch.setenv("NTASKER_CLAUDE_HOME", str(tmp_path / "claude-home"))
    (tmp_path / "claude-home").mkdir()
    monkeypatch.setattr(usage, "_cache", {"at": 0.0, "usage": None})
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return TestClient(app, base_url=BASE)


def _login(tmp_path, expires_in=3600):
    creds = {"claudeAiOauth": {"accessToken": "tok", "expiresAt": (time.time() + expires_in) * 1000}}
    (tmp_path / "claude-home" / ".credentials.json").write_text(json.dumps(creds))


def test_no_login_means_no_usage(client, monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "_request", lambda token: calls.append(token) or PAYLOAD)
    assert client.get("/api/claude/usage").json() == {"usage": None}
    assert calls == []  # nothing to ask without a token


def test_expired_token_means_no_usage(client, tmp_path, monkeypatch):
    _login(tmp_path, expires_in=-60)
    monkeypatch.setattr(usage, "_request", lambda token: PAYLOAD)
    assert client.get("/api/claude/usage").json() == {"usage": None}


def test_usage_windows_and_cache(client, tmp_path, monkeypatch):
    _login(tmp_path)
    calls = []
    monkeypatch.setattr(usage, "_request", lambda token: calls.append(token) or PAYLOAD)
    body = client.get("/api/claude/usage").json()
    assert body["usage"] == {
        "five_hour": {"utilization": 11.0, "resets_at": "2026-09-25T18:50:00+00:00"},
        "seven_day": {"utilization": 90.0, "resets_at": "2026-09-25T15:00:00+00:00"},
    }
    client.get("/api/claude/usage")
    assert calls == ["tok"]  # second call within CACHE_TTL is served from the cache


def test_failed_refresh_serves_stale(client, tmp_path, monkeypatch):
    _login(tmp_path)
    monkeypatch.setattr(usage, "_request", lambda token: PAYLOAD)
    first = client.get("/api/claude/usage").json()["usage"]
    monkeypatch.setattr(usage, "CACHE_TTL", 0.0)

    def boom(token):
        raise OSError("offline")

    monkeypatch.setattr(usage, "_request", boom)
    assert client.get("/api/claude/usage").json()["usage"] == first


def test_login_without_limits_means_no_usage(client, tmp_path, monkeypatch):
    _login(tmp_path)
    monkeypatch.setattr(usage, "_request", lambda token: {"five_hour": None, "seven_day": None})
    assert client.get("/api/claude/usage").json() == {"usage": None}


def test_widget_follows_the_plugin_switch(client, monkeypatch):
    assert "claude-usage" in client.get("/").text
    monkeypatch.setenv(plugins.ENV_DISABLED, "claude")
    assert "claude-usage" not in client.get("/").text
    assert client.get("/api/claude/usage").status_code == 404


def test_fresh_bypasses_the_cache_ttl(client, tmp_path, monkeypatch):
    """``?fresh=1`` refetches inside CACHE_TTL -- the topbar's post-session refresh."""
    _login(tmp_path)
    calls = []
    monkeypatch.setattr(usage, "_request", lambda token: calls.append(token) or PAYLOAD)
    client.get("/api/claude/usage")
    monkeypatch.setattr(usage, "MIN_INTERVAL", 0.0)
    client.get("/api/claude/usage?fresh=1")
    assert len(calls) == 2


def test_fresh_still_honours_the_min_interval(client, tmp_path, monkeypatch):
    """Session churn across many tabs must not turn into a request flood."""
    _login(tmp_path)
    calls = []
    monkeypatch.setattr(usage, "_request", lambda token: calls.append(token) or PAYLOAD)
    for _ in range(5):
        client.get("/api/claude/usage?fresh=1")
    assert len(calls) == 1
