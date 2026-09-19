"""The agent's final report: API + CLI semantics, seed ordering."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from ntasker import cli
from ntasker.app import app
from ntasker.claude_runner import queue_seed_for_task
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=BASE)


def test_patch_report_sets_and_clears(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert t["report"] is None and t["report_at"] is None
    t = client.patch(f"/api/tasks/{t['id']}", json={"report": "# done\n\n- x"}).json()
    assert t["report"] == "# done\n\n- x" and t["report_at"]
    t = client.patch(f"/api/tasks/{t['id']}", json={"title": "u"}).json()
    assert t["report"] == "# done\n\n- x"   # untouched by other fields
    t = client.patch(f"/api/tasks/{t['id']}", json={"report": ""}).json()
    assert t["report"] is None and t["report_at"] is None


def test_cli_report_stdin_file_and_patch(db, client, monkeypatch, tmp_path):
    tid = client.post("/api/tasks", json={"title": "t"}).json()["id"]
    monkeypatch.setattr("sys.stdin", io.StringIO("## via stdin\n"))
    assert cli.main(["--db", str(db), "report", str(tid)]) == 0
    assert client.get(f"/api/tasks/{tid}").json()["report"] == "## via stdin"
    f = tmp_path / "r.md"
    f.write_text("## via file\n")
    assert cli.main(["--db", str(db), "report", str(tid), "--file", str(f)]) == 0
    assert client.get(f"/api/tasks/{tid}").json()["report"] == "## via file"
    assert cli.main(["--db", str(db), "patch", str(tid), "--report", ""]) == 0
    assert client.get(f"/api/tasks/{tid}").json()["report_at"] is None
    assert cli.main(["--db", str(db), "patch", str(tid), "--report", "via patch"]) == 0
    assert client.get(f"/api/tasks/{tid}").json()["report"] == "via patch"


def test_cli_report_unknown_task(db, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert cli.main(["--db", str(db), "report", "9999"]) == 1


def test_seed_orders_report_before_handoff(db):
    seed = queue_seed_for_task({"id": 5, "title": "t", "status": "open", "priority": "normal"})
    assert seed.index("ntasker report 5") < seed.index("ntasker patch 5 --phase review")


def test_seed_uses_agent_run_rules(db):
    from ntasker.settings import RUN_RULES_DEFAULT, delete_setting, set_setting

    task = {"id": 7, "title": "t", "status": "open", "priority": "normal", "agent": "claude"}
    assert RUN_RULES_DEFAULT.replace("{id}", "7") in queue_seed_for_task(task)
    set_setting("claude_run_rules", "Custom rules for #{id}.")
    seed = queue_seed_for_task(task)
    assert seed.endswith("Custom rules for #7.")
    assert "Tracker rules" not in seed
    # Another agent's rules stay untouched.
    assert "Tracker rules" in queue_seed_for_task({**task, "agent": "pi"})
    delete_setting("claude_run_rules")
    assert "Tracker rules" in queue_seed_for_task(task)


def test_run_rules_rejects_empty(db):
    from ntasker.settings import set_setting

    with pytest.raises(ValueError):
        set_setting("claude_run_rules", "   ")
