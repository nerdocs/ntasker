"""Tests for the plugin registry (:mod:`ntasker.plugins`) and its enablement rules."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ntasker import plugins
from ntasker.agents import agent_keys, default_agent_key
from ntasker.app import app, templates
from ntasker.db import init_db, set_db_path
from ntasker.settings import validate_plugins_disabled

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return TestClient(app, base_url=BASE)


def test_builtins_registered():
    plugins.load_all()
    assert set(plugins.REGISTRY) >= {"claude", "opencode", "pi"}
    assert all(plugins.REGISTRY[n].spec.kind == "agent" for n in ("claude", "opencode", "pi"))


def test_validator_rejects_unknown_and_last_agent():
    with pytest.raises(ValueError):
        validate_plugins_disabled('["nope"]')
    with pytest.raises(ValueError):
        validate_plugins_disabled('["claude", "opencode", "pi"]')
    with pytest.raises(ValueError):
        validate_plugins_disabled("not json")
    assert json.loads(validate_plugins_disabled('["pi", "pi", " opencode "]')) == ["opencode", "pi"]


def test_disable_agent_via_setting(client):
    r = client.put("/api/settings/plugins_disabled", json={"value": '["pi"]'})
    assert r.status_code == 200, r.text
    assert "pi" not in agent_keys()
    keys = {a["key"] for a in client.get("/api/agents").json()["agents"]}
    assert keys == {"claude", "opencode"}
    assert client.get("/api/agents").json()["agents"][0]["icon"].startswith("/static/plugins/")
    # default_agent may not point at a disabled agent
    assert client.put("/api/settings/default_agent", json={"value": "pi"}).status_code == 400
    # a task may not be created for a disabled agent
    r = client.post("/api/tasks", json={"title": "t", "agent": "pi"})
    assert r.status_code == 400
    listed = {p["name"]: p["enabled"] for p in client.get("/api/plugins").json()}
    assert listed["pi"] is False and listed["claude"] is True


def test_env_wins_over_setting(client, monkeypatch):
    client.put("/api/settings/plugins_disabled", json={"value": '["pi"]'})
    monkeypatch.setenv(plugins.ENV_DISABLED, "opencode")
    assert plugins.disabled_plugins() == {"opencode"}
    assert agent_keys() == ("claude", "pi")


def test_env_never_disables_every_agent(client, monkeypatch):
    monkeypatch.setenv(plugins.ENV_DISABLED, "claude,opencode,pi")
    assert plugins.disabled_plugins() == set()


def test_default_agent_follows_enablement(client, monkeypatch):
    monkeypatch.setenv(plugins.ENV_DISABLED, "claude")
    assert default_agent_key() == "opencode"
    assert client.get("/api/agents").json()["default"] == "opencode"


def test_slot_template_resolves_under_plugins_dir(tmp_path):
    # A slot template is addressed relative to ``plugins/``; pin that the
    # shared environment searches that directory too.
    src = templates.env.loader.get_source(templates.env, "claude/__init__.py")[0]
    assert "SPEC = PluginSpec" in src


def test_index_renders_with_slots(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'window.__plugins = ["claude", "opencode", "pi"]' in r.text
