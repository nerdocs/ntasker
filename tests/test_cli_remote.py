"""CLI remote mode: with a token the task commands go through the server's API.

``_api_call`` is routed into an in-process TestClient, so the real endpoints
answer -- the test checks the argument mapping end to end, against a server
DB that is a different file from the CLI's own (settings-only) DB.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from ntasker import auth, cli, db
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import set_setting


@pytest.fixture
def remote(tmp_path, monkeypatch):
    server_db = tmp_path / "server.db"
    set_db_path(server_db)
    init_db(server_db)
    with get_conn() as conn:
        auth.create_user(conn, "alice", "pw", admin=True)
        bob = auth.create_user(conn, "bob", "pw")
        _id, token = auth.create_token(conn, bob, "laptop")
    set_setting("auth_mode", "password")
    client = TestClient(app, base_url="http://127.0.0.1:8766")

    def api_call(base, method, path, body=None, timeout=5.0):
        # The CLI rebinds DB_PATH to its own file; the server answers from its DB.
        cli_db = db.DB_PATH
        set_db_path(server_db)
        try:
            headers = {"Authorization": f"Bearer {cli._api_token()}"}
            r = client.request(method, path, json=body, headers=headers)
        finally:
            set_db_path(cli_db)
        return r.status_code, (r.json() if r.content else {})

    monkeypatch.setattr(cli, "_api_call", api_call)
    monkeypatch.setenv("NTASKER_TOKEN", token)
    local_db = tmp_path / "local.db"
    return lambda *argv: cli.main(["--db", str(local_db), *argv]), server_db


def _server_task(server_db, task_id):
    set_db_path(server_db)
    with get_conn() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def test_add_patch_show_done(remote, capsys):
    run, server_db = remote
    assert run("add", "--title", "remote", "--project", "p", "--tag", "x", "--assignee", "alice") == 0
    assert capsys.readouterr().out.startswith("#1 ")
    row = _server_task(server_db, 1)
    assert (row["project"], row["assignee_id"]) == ("p", 2)  # alice
    assert row["created_by"] == 3  # bob, the token's owner

    assert run("patch", "1", "--phase", "review", "--priority", "high") == 0
    assert run("show", "1", "--json") == 0
    out = capsys.readouterr().out
    assert '"phase": "review"' in out and '"priority": "high"' in out

    assert run("tag-add", "1", "y") == 0 and run("tag-rm", "1", "x") == 0
    assert run("done", "1") == 0
    assert _server_task(server_db, 1)["status"] == "done"


def test_report_inbox_list_delete(remote, monkeypatch, capsys):
    run, server_db = remote
    run("add", "--title", "t")
    monkeypatch.setattr("sys.stdin", io.StringIO("# Report"))
    assert run("report", "1") == 0
    assert _server_task(server_db, 1)["report"] == "# Report"
    assert run("in", "a note") == 0
    assert run("list", "--json") == 0
    assert '"title": "t"' in capsys.readouterr().out
    assert run("delete", "1", "--yes") == 0
    set_db_path(server_db)
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_errors_and_worktree_refusal(remote, capsys):
    run, _server_db = remote
    assert run("show", "99") == 1
    assert run("add", "--title", "t", "--worktree", "new") == 1
    assert "worktree" in capsys.readouterr().err
