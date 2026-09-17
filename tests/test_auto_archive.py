"""Tests for the auto_archive_days setting: done tasks age out into the archive."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ntasker.app import app, auto_archive_sweep
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import get_auto_archive_days, set_setting, validate_auto_archive_days

LOCAL = "127.0.0.1:8766"


@pytest.fixture
def client(tmp_path):
    set_db_path(tmp_path / "t.db")
    init_db()
    return TestClient(app, base_url=f"http://{LOCAL}", headers={"Host": LOCAL})


def _done_task(client, title, days_ago):
    tid = client.post("/api/tasks", json={"title": title}).json()["id"]
    completed = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (completed, tid)
        )
    return tid


def test_validator():
    assert validate_auto_archive_days(" 14 ") == "14"
    assert validate_auto_archive_days("0") == "0"
    for bad in ("", "-1", "1.5", "abc"):
        with pytest.raises(ValueError):
            validate_auto_archive_days(bad)


def test_default_is_30(client):
    assert get_auto_archive_days() == 30


def test_sweep_archives_only_old_done_tasks(client):
    old = _done_task(client, "old", 31)
    fresh = _done_task(client, "fresh", 29)
    open_id = client.post("/api/tasks", json={"title": "open"}).json()["id"]
    assert auto_archive_sweep() == 1
    assert client.get(f"/api/tasks/{old}").json()["archived"] is True
    assert client.get(f"/api/tasks/{fresh}").json()["archived"] is False
    assert client.get(f"/api/tasks/{open_id}").json()["archived"] is False


def test_zero_disables_sweep(client):
    old = _done_task(client, "old", 100)
    set_setting("auto_archive_days", "0")
    assert auto_archive_sweep() == 0
    assert client.get(f"/api/tasks/{old}").json()["archived"] is False


def test_put_setting_sweeps_immediately(client):
    tid = _done_task(client, "week old", 8)
    assert auto_archive_sweep() == 0
    assert client.put("/api/settings/auto_archive_days", json={"value": "7"}).status_code == 200
    assert client.get(f"/api/tasks/{tid}").json()["archived"] is True
