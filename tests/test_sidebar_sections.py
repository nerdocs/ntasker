"""Tests for the sidebar_sections setting: fold state per sidebar section."""

import json

import pytest
from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.settings import SIDEBAR_SECTIONS, get_sidebar_sections, validate_sidebar_sections

LOCAL = "127.0.0.1:8766"


@pytest.fixture
def client(tmp_path):
    set_db_path(tmp_path / "t.db")
    init_db()
    return TestClient(app, base_url=f"http://{LOCAL}", headers={"Host": LOCAL})


def test_defaults_open(client):
    assert get_sidebar_sections() == {name: True for name in SIDEBAR_SECTIONS}


def test_validator_drops_unknown_and_rejects_non_bool():
    assert json.loads(validate_sidebar_sections('{"tags": false, "bogus": true}')) == {"tags": False}
    with pytest.raises(ValueError):
        validate_sidebar_sections('{"tags": "no"}')
    with pytest.raises(ValueError):
        validate_sidebar_sections("[]")


def test_put_persists_and_index_renders_state(client):
    r = client.put(
        "/api/settings/sidebar_sections",
        json={"value": json.dumps({"phases": False})},
    )
    assert r.status_code == 200
    state = get_sidebar_sections()
    assert state["phases"] is False
    assert state["projects"] is True
    html = client.get("/").text
    assert '"phases": false' in html
