"""Settings-card install button: POST /api/agents/<key>/assets/install."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import agents
from ntasker.app import app
from ntasker.claude_assets import scan_status
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    home = tmp_path / "home"
    monkeypatch.setattr("ntasker.app.resolve_home", lambda spec: home)
    monkeypatch.setattr(agents, "resolve_home", lambda spec: home)
    return TestClient(app, base_url=BASE), home


def test_install_then_drift_needs_force(client):
    c, home = client
    spec = agents.get_spec("claude")
    r = c.post("/api/agents/claude/assets/install", json={"force": False})
    assert r.status_code == 200 and r.json()["success"]
    assert scan_status(spec, home).installed

    drifted = next(f.path for f in scan_status(spec, home).files)
    drifted.write_text("edited\n")
    assert scan_status(spec, home).drift
    r = c.post("/api/agents/claude/assets/install", json={"force": False})
    assert r.json()["success"] is False
    assert scan_status(spec, home).drift
    r = c.post("/api/agents/claude/assets/install", json={"force": True})
    assert r.json()["success"] is True
    assert not scan_status(spec, home).drift
    assert list(drifted.parent.glob(drifted.name + ".bak*"))


def test_unknown_agent(client):
    c, _ = client
    assert c.post("/api/agents/nope/assets/install", json={}).status_code == 404
