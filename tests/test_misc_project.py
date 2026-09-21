"""Misc project: settings validators, /api/projects order, child env."""

import os

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner
from ntasker.agents import get_spec
from ntasker.app import app
from ntasker.db import set_db_path
from ntasker.settings import validate_misc_project, validate_on_off

LOCAL = "127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NTASKER_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setattr("ntasker.projects.Path.home", staticmethod(lambda: tmp_path))
    for var in ("NTASKER_MISC_PROJECT", "NTASKER_MISC_NO_MEMORY"):
        monkeypatch.delenv(var, raising=False)
    set_db_path(tmp_path / "tasks.db")
    with TestClient(app, base_url=f"http://{LOCAL}") as c:
        yield c


def test_validate_misc_project():
    assert validate_misc_project("  misc ") == "misc"
    with pytest.raises(ValueError):
        validate_misc_project("   ")
    with pytest.raises(ValueError):
        validate_misc_project("__none__")
    with pytest.raises(ValueError):
        validate_misc_project("~/Projekte/misc")
    assert validate_on_off("yes") == "on"


def test_settings_api_rejects_bad_values(client):
    assert client.put("/api/settings/misc_project", json={"value": "__none__"}).status_code == 400
    assert client.put("/api/settings/misc_project", json={"value": " "}).status_code == 400
    assert client.put("/api/settings/misc_no_memory", json={"value": "maybe"}).status_code == 400
    assert client.put("/api/settings/misc_project", json={"value": "misc"}).status_code == 200
    assert client.put("/api/settings/misc_no_memory", json={"value": "1"}).json()["value"] == "on"


def test_projects_without_setting_has_no_misc_row(client):
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert names == ["__none__"]
    assert not any(p.get("misc") for p in client.get("/api/projects").json())


def test_projects_misc_row_follows_none_even_without_tasks(client):
    assert client.post("/api/tasks", json={"title": "a", "project": "alpha"}).status_code == 201
    client.put("/api/settings/misc_project", json={"value": "zzz-misc"})
    rows = client.get("/api/projects").json()
    assert [p["name"] for p in rows] == ["__none__", "zzz-misc", "alpha"]
    assert rows[1] == {
        "name": "zzz-misc",
        "open_count": 0,
        "task_count": 0,
        "hidden": False,
        "stale": False,
        "misc": True,
    }
    assert "misc" not in rows[2]


def test_projects_misc_row_counts_and_ignores_hidden(client):
    client.put("/api/settings/misc_project", json={"value": "misc"})
    assert client.post("/api/tasks", json={"title": "q", "project": "misc"}).status_code == 201
    client.put("/api/projects/hidden", json={"project": "misc", "hidden": True})
    rows = client.get("/api/projects").json()
    assert [p["name"] for p in rows] == ["__none__", "misc"]
    assert rows[1]["open_count"] == 1 and rows[1]["hidden"] is False


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.delenv("NTASKER_MISC_PROJECT", raising=False)
    monkeypatch.delenv("NTASKER_MISC_NO_MEMORY", raising=False)
    set_db_path(tmp_path / "tasks.db")
    with TestClient(app, base_url=f"http://{LOCAL}") as c:
        yield c


def _has_no_memory(spec_key, project):
    return "CLAUDE_CODE_DISABLE_AUTO_MEMORY" in claude_runner._clean_env(
        get_spec(spec_key), 1, project
    )


def test_clean_env_no_memory_only_for_misc_claude_runs(env_db, monkeypatch):
    assert not _has_no_memory("claude", "misc")
    env_db.put("/api/settings/misc_project", json={"value": "misc"})
    assert not _has_no_memory("claude", "misc")  # misc_no_memory still off
    env_db.put("/api/settings/misc_no_memory", json={"value": "on"})
    assert _has_no_memory("claude", "misc")
    assert not _has_no_memory("claude", "other")
    assert not _has_no_memory("claude", None)
    assert not _has_no_memory("opencode", "misc")
    env = claude_runner._clean_env(get_spec("claude"), 1, "misc")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    # ENV override of the name counts too.
    monkeypatch.setenv("NTASKER_MISC_PROJECT", "elsewhere")
    assert _has_no_memory("claude", "elsewhere") and not _has_no_memory("claude", "misc")
    assert os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") is None
