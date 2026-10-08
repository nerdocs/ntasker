"""Tests for the "New project" dialog API (ntasker.newproject)."""

import json

import pytest
from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.settings import get_setting, set_setting

LOCAL = "127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("NTASKER_PROJECTS_BASE", raising=False)
    db = tmp_path / "tasks.db"
    set_db_path(db)
    init_db(db)
    base = tmp_path / "Code"
    (base / "CodingProjects").mkdir(parents=True)
    (base / "Forschung").mkdir()
    (base / "_archive").mkdir()
    set_setting("projects_base", str(base))
    return TestClient(app, base_url=f"http://{LOCAL}"), base


def _create(c, **overrides):
    body = {"name": "tool", "folder": "CodingProjects", "group": "Labor", "git_init": False}
    body.update(overrides)
    return c.post("/api/projects/create", json=body)


def test_folders_lists_subdirs_and_groups(client):
    c, base = client
    set_setting("project_groups", json.dumps({"x": "Heim", "y": "Labor"}))
    data = c.get("/api/projects/folders").json()
    assert data["base"] == str(base)
    assert data["folders"] == ["CodingProjects", "Forschung"]
    assert data["groups"] == ["Heim", "Labor"]


def test_create_makes_dir_group_and_sidebar_entry(client):
    c, base = client
    r = _create(c)
    assert r.status_code == 201
    body = r.json()
    assert body["project"] == "CodingProjects/tool"
    assert (base / "CodingProjects" / "tool").is_dir()
    assert json.loads(get_setting("project_groups"))["CodingProjects/tool"] == "Labor"
    names = [p["name"] for p in c.get("/api/projects").json()]
    assert "CodingProjects/tool" in names


def test_create_git_init(client):
    c, base = client
    r = _create(c, git_init=True)
    assert r.status_code == 201
    if r.json()["git"]:
        assert (base / "CodingProjects" / "tool" / ".git").is_dir()
    else:
        assert r.json()["git_error"]


def test_create_new_nested_folder(client):
    c, base = client
    r = _create(c, folder="Labor/Neu")
    assert r.status_code == 201
    assert r.json()["project"] == "Labor/Neu/tool"
    assert (base / "Labor" / "Neu" / "tool").is_dir()


def test_existing_dir_is_409(client):
    c, base = client
    (base / "CodingProjects" / "tool").mkdir()
    assert _create(c).status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": ".."},
        {"name": "a/b"},
        {"name": ".hidden"},
        {"folder": ""},
        {"folder": "../outside"},
        {"folder": "CodingProjects/../.."},
        {"group": "  "},
    ],
)
def test_invalid_input_is_400(client, overrides):
    c, base = client
    assert _create(c, **overrides).status_code == 400
    assert not (base.parent / "outside").exists()


def test_no_base_is_400(client):
    c, _base = client
    from ntasker.settings import delete_setting

    delete_setting("projects_base")
    assert _create(c).status_code == 400


def test_index_renders_new_project_modal(client):
    c, _base = client
    html = c.get("/").text
    assert 'openNewProject()' in html
    assert 'submitNewProject()' in html


def test_deleted_dir_leaves_sidebar(client):
    c, base = client
    _create(c)
    (base / "CodingProjects" / "tool").rmdir()
    names = [p["name"] for p in c.get("/api/projects").json()]
    assert "CodingProjects/tool" not in names


def test_adopted_session_in_created_dir_is_filed_under_it(client):
    """A session running in a created project (or below it) names that project,
    not the folder right under the base."""
    from ntasker.projects import name_for_dir

    c, base = client
    _create(c)
    assert name_for_dir(base / "CodingProjects" / "tool" / "src") == "CodingProjects/tool"
