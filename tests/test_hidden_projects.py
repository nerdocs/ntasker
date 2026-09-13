"""Tests for the hidden_projects setting -> table migration and API."""

import json
import sqlite3

from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import init_db, set_db_path

LOCAL = "127.0.0.1:8766"


def _setup_setting_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('hidden_projects', ?)",
        (json.dumps(["alpha", " beta ", ""]),),
    )
    conn.commit()
    conn.close()


def _hidden_projects(path):
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT project FROM hidden_projects").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _setting_row(path):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM settings WHERE key = 'hidden_projects'").fetchone()
    conn.close()
    return row


def test_setting_folds_into_table_and_is_dropped(tmp_path):
    path = tmp_path / "tasks.db"
    _setup_setting_db(path)

    init_db(path)

    assert _hidden_projects(path) == {"alpha", "beta"}
    assert _setting_row(path) is None


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "tasks.db"
    _setup_setting_db(path)

    init_db(path)
    init_db(path)

    assert _hidden_projects(path) == {"alpha", "beta"}


def test_hidden_projects_api(tmp_path):
    path = tmp_path / "tasks.db"
    set_db_path(path)

    with TestClient(app, base_url=f"http://{LOCAL}") as client:
        resp = client.post("/api/tasks", json={"title": "t", "project": "x"})
        assert resp.status_code == 201

        resp = client.put("/api/projects/hidden", json={"project": "x", "hidden": True})
        assert resp.status_code == 200

        resp = client.get("/api/projects")
        assert resp.status_code == 200
        projects = {p["name"]: p for p in resp.json()}
        assert projects["x"]["hidden"] is True

        resp = client.put("/api/projects/hidden", json={"project": "x", "hidden": False})
        assert resp.status_code == 200

        resp = client.get("/api/projects")
        projects = {p["name"]: p for p in resp.json()}
        assert projects["x"]["hidden"] is False

        resp = client.put(
            "/api/projects/hidden", json={"project": "__none__", "hidden": True}
        )
        assert resp.status_code == 400
