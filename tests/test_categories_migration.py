"""Tests for the v2.22 fork migration: project_categories -> project_groups."""

import json
import sqlite3

from ntasker.db import init_db


def _setup_fork_db(path):
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
        "INSERT INTO settings (key, value) VALUES ('project_groups', ?)",
        (json.dumps({"alpha": "keep"}),),
    )
    conn.execute(
        "CREATE TABLE project_categories (project TEXT PRIMARY KEY, category TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO project_categories (project, category) VALUES (?, ?)",
        [("alpha", "Coding"), ("beta", "Lab"), ("gamma", "Lab")],
    )
    conn.commit()
    conn.close()


def _project_groups(path):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM settings WHERE key = 'project_groups'").fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def _table_exists(path, name):
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    conn.close()
    return row is not None


def test_fork_table_folds_into_setting_and_is_dropped(tmp_path):
    path = tmp_path / "tasks.db"
    _setup_fork_db(path)

    init_db(path)

    assert _project_groups(path) == {"alpha": "keep", "beta": "Lab", "gamma": "Lab"}
    assert not _table_exists(path, "project_categories")


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "tasks.db"
    _setup_fork_db(path)

    init_db(path)
    init_db(path)

    assert _project_groups(path) == {"alpha": "keep", "beta": "Lab", "gamma": "Lab"}


def test_fresh_db_without_fork_table_creates_no_setting(tmp_path):
    path = tmp_path / "tasks.db"

    init_db(path)

    assert _project_groups(path) is None
