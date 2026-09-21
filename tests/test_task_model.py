"""Per-task ``model``: overrides the agent's ``<key>_model`` setting for that task's runs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import agents, cli
from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.settings import set_setting

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("NTASKER_CLAUDE_MODEL", raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=BASE)


def test_model_roundtrip_and_clear(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert t["model"] is None
    t = client.post("/api/tasks", json={"title": "t", "model": " opus "}).json()
    assert t["model"] == "opus"
    assert client.patch(f"/api/tasks/{t['id']}", json={"model": "sonnet"}).json()["model"] == "sonnet"
    assert client.patch(f"/api/tasks/{t['id']}", json={"model": ""}).json()["model"] is None


def test_agents_feed_lists_model_suggestions(client):
    feed = client.get("/api/agents").json()
    by_key = {a["key"]: a for a in feed["agents"]}
    assert "opus" in by_key["claude"]["model_suggestions"]


def test_task_model_wins_over_setting(db):
    spec = agents.get_spec("claude")
    set_setting("claude_model", "sonnet")
    argv = spec.build_spawn("/task 1", model="opus")
    assert argv[argv.index("--model") + 1] == "opus"
    argv = spec.build_spawn("/task 1", model=None)
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_cli_model_flags(db, client, capsys):
    ntasker = ["--db", str(db)]
    assert cli.main([*ntasker, "add", "--title", "m", "--model", "opus"]) == 0
    tid = int(capsys.readouterr().out.strip().split()[0].lstrip("#"))
    assert client.get(f"/api/tasks/{tid}").json()["model"] == "opus"
    assert cli.main([*ntasker, "patch", str(tid), "--model", "haiku"]) == 0
    assert client.get(f"/api/tasks/{tid}").json()["model"] == "haiku"
    assert cli.main([*ntasker, "show", str(tid)]) == 0
    assert "haiku" in capsys.readouterr().out
    assert cli.main([*ntasker, "patch", str(tid), "--model", ""]) == 0
    assert client.get(f"/api/tasks/{tid}").json()["model"] is None
