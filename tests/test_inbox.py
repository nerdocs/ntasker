"""Inbox proposals: schema, invisibility to task consumers, and the start guards."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, taskqueue
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.delenv("NTASKER_QUEUE_ENABLED", raising=False)
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: [])
    monkeypatch.setattr(taskqueue, "_runnable_agents", lambda: {"claude"})
    taskqueue._running.clear()
    taskqueue._booted = True
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=BASE)


def _columns(path, table):
    with sqlite3.connect(path) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _propose(title="p", project="x"):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, project, proposed, triage) VALUES (?, ?, 1, ?)",
            (title, project, '{"raw": "note"}'),
        )
        return int(cur.lastrowid)


# --- schema ---------------------------------------------------------------

def test_init_db_twice_is_idempotent(tmp_path):
    path = tmp_path / "fresh.db"
    init_db(path)
    init_db(path)
    assert {"proposed", "triage"} <= set(_columns(path, "tasks"))
    assert {"inbox", "project_summaries", "triage_examples"} <= _tables(path)


def test_init_db_migrates_pre_inbox_database(tmp_path):
    path = tmp_path / "old.db"
    init_db(path)
    with sqlite3.connect(path) as conn:   # roll the file back to the pre-inbox shape
        conn.execute("ALTER TABLE tasks DROP COLUMN proposed")
        conn.execute("ALTER TABLE tasks DROP COLUMN triage")
        for table in ("inbox", "project_summaries", "triage_examples"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("INSERT INTO tasks (title) VALUES ('legacy')")
    init_db(path)

    assert {"proposed", "triage"} <= set(_columns(path, "tasks"))
    assert {"inbox", "project_summaries", "triage_examples"} <= _tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT proposed, triage FROM tasks").fetchone() == (0, None)


def test_task_json_carries_proposed_and_triage(client):
    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert t["proposed"] is False and t["triage"] is None
    pid = _propose()
    p = client.get(f"/api/tasks/{pid}").json()
    assert p["proposed"] is True and p["triage"] == {"raw": "note"}


# --- invisibility -----------------------------------------------------------

def test_proposal_is_absent_from_task_lists_and_counts(client):
    client.post("/api/tasks", json={"title": "real", "project": "x"})
    pid = _propose(project="x")
    ids = [t["id"] for t in client.get("/api/tasks").json()]
    assert pid not in ids and len(ids) == 1
    assert client.get("/api/tasks", params={"status": "open"}).json()[0]["proposed"] is False
    stats = client.get("/api/stats").json()
    assert stats["open"] == 1 and stats["inbox"] == 1
    x = next(p for p in client.get("/api/projects").json() if p["name"] == "x")
    assert x["open_count"] == 1
    assert client.get("/api/phases").json()[0]["open_count"] == 1
    normal = next(p for p in client.get("/api/priorities").json() if p["value"] == "normal")
    assert normal["open_count"] == 1


def test_stats_inbox_counts_pending_and_failed_rows(client):
    with get_conn() as conn:
        conn.execute("INSERT INTO inbox (text, status) VALUES ('a', 'pending')")
        conn.execute("INSERT INTO inbox (text, status, error) VALUES ('b', 'failed', 'x')")
        conn.execute("INSERT INTO inbox (text, status) VALUES ('c', 'triaged')")
    assert client.get("/api/stats").json()["inbox"] == 2


# --- guards -----------------------------------------------------------------

def test_proposal_cannot_be_queued(client):
    pid = _propose()
    assert client.put("/api/queue", json={"ids": [pid]}).json()["items"] == []
    r = client.post("/api/queue/run", json={"id": pid})
    assert r.status_code == 409
    assert client.get(f"/api/tasks/{pid}").json()["phase"] == "planned"


def test_worker_retires_proposal_flagged_behind_its_back(client, monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(
        taskqueue, "start_detached_session", lambda tid, seed, **kw: started.append(tid) or True
    )
    a = client.post("/api/tasks", json={"title": "a"}).json()
    client.put("/api/queue", json={"ids": [a["id"]]})
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET proposed = 1 WHERE id = ?", (a["id"],))
    taskqueue.tick()
    assert started == []
    assert taskqueue.load_queue() == []


def test_spawn_refuses_proposal(client):
    pid = _propose()
    with pytest.raises(claude_runner.DraftTaskError):
        claude_runner._start_session(pid)
    assert claude_runner.start_detached_session(pid, "seed") is False


# --- API: inbox rows ---------------------------------------------------------

def test_inbox_post_and_get_shape(client):
    r = client.post("/api/inbox", json={"text": "  a raw note  ", "source": "ui"})
    assert r.status_code == 201
    item = r.json()
    assert item["text"] == "a raw note" and item["status"] == "pending" and item["source"] == "ui"
    assert client.post("/api/inbox", json={"text": "   "}).status_code == 400
    assert client.post("/api/inbox", json={"text": "default source"}).json()["source"] == "api"
    body = client.get("/api/inbox").json()
    assert [i["text"] for i in body["items"]] == ["a raw note", "default source"]
    assert body["tasks"] == []
    pid = _propose(title="prop")
    body = client.get("/api/inbox").json()
    assert [t["id"] for t in body["tasks"]] == [pid] and body["tasks"][0]["proposed"] is True


def test_inbox_retry_only_from_failed(client):
    item = client.post("/api/inbox", json={"text": "x"}).json()
    assert client.post(f"/api/inbox/{item['id']}/retry").status_code == 409
    with get_conn() as conn:
        conn.execute("UPDATE inbox SET status='failed', error='boom' WHERE id=?", (item["id"],))
    r = client.post(f"/api/inbox/{item['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["error"] is None
    assert client.post("/api/inbox/999/retry").status_code == 404


def test_inbox_delete(client):
    item = client.post("/api/inbox", json={"text": "x"}).json()
    assert client.delete(f"/api/inbox/{item['id']}").status_code == 204
    assert client.delete(f"/api/inbox/{item['id']}").status_code == 404
    assert client.get("/api/inbox").json()["items"] == []


# --- API: accept / discard --------------------------------------------------

def _propose_full(project="x"):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, project, proposed, triage) VALUES ('p', ?, 1, ?)",
            (project, '{"raw": "the note", "project": %s}' % ('null' if project is None else f'"{project}"')),
        )
        tid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO inbox (text, status, task_id) VALUES ('the note', 'triaged', ?)", (tid,)
        )
        return tid


def _examples():
    with get_conn() as conn:
        return [tuple(r) for r in conn.execute("SELECT text, project FROM triage_examples")]


def test_accept_unchanged(client):
    pid = _propose_full("x")
    t = client.post(f"/api/tasks/{pid}/accept", json={}).json()
    assert t["proposed"] is False and t["project"] == "x"
    assert _examples() == []
    assert client.get("/api/inbox").json()["tasks"] == []
    assert [x["id"] for x in client.get("/api/tasks").json()] == [pid]


def test_accept_with_other_project_records_example(client):
    pid = _propose_full("x")
    t = client.post(f"/api/tasks/{pid}/accept", json={"project": "y"}).json()
    assert t["project"] == "y" and t["proposed"] is False
    assert _examples() == [("the note", "y")]


def test_accept_cross_project_records_null_example(client):
    pid = _propose_full("x")
    t = client.post(f"/api/tasks/{pid}/accept", json={"project": None}).json()
    assert t["project"] is None
    assert _examples() == [("the note", None)]


def test_accept_with_locks(client):
    pid = _propose_full("x")
    t = client.post(f"/api/tasks/{pid}/accept", json={"project": "x", "locks": ["y", "x", " ", "z"]}).json()
    assert t["project"] == "x" and t["locks"] == ["y", "z"]
    assert _examples() == []


def test_accept_non_proposal_is_409(client):

    t = client.post("/api/tasks", json={"title": "t"}).json()
    assert client.post(f"/api/tasks/{t['id']}/accept", json={}).status_code == 409
    assert client.post("/api/tasks/999/accept", json={}).status_code == 404


def test_discard_proposal_keeps_inbox_row(client):
    pid = _propose_full("x")
    assert client.delete(f"/api/tasks/{pid}").status_code == 204
    with get_conn() as conn:
        row = conn.execute("SELECT status, task_id FROM inbox").fetchone()
    assert row["status"] == "triaged" and row["task_id"] is None
    assert client.get("/api/inbox").json() == {"items": [], "tasks": []}


# --- API: project summaries --------------------------------------------------

def test_project_summary_put_and_listing(client):
    client.post("/api/tasks", json={"title": "t", "project": "x"})
    r = client.put("/api/projects/summary", json={"project": "x", "summary": " A tracker. "})
    assert r.json() == {"project": "x", "summary": "A tracker."}
    x = next(p for p in client.get("/api/projects").json() if p["name"] == "x")
    assert x["summary"] == "A tracker."
    client.put("/api/projects/summary", json={"project": "x", "summary": ""})
    x = next(p for p in client.get("/api/projects").json() if p["name"] == "x")
    assert x["summary"] is None
    assert client.put("/api/projects/summary", json={"project": " ", "summary": "s"}).status_code == 400


def test_project_summary_regenerate(client, monkeypatch, tmp_path):
    from ntasker import locks, triage

    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    assert client.post("/api/projects/summary/regenerate", json={"project": "nodir"}).status_code == 400
    (tmp_path / "x").mkdir()
    monkeypatch.setattr(triage, "summarize_project", lambda name: f"fresh {name}")
    r = client.post("/api/projects/summary/regenerate", json={"project": "x"})
    assert r.json() == {"project": "x", "summary": "fresh x"}

    def boom(name):
        raise triage.TriageError("claude CLI not found")

    monkeypatch.setattr(triage, "summarize_project", boom)
    r = client.post("/api/projects/summary/regenerate", json={"project": "x"})
    assert r.status_code == 502 and "claude CLI not found" in r.json()["detail"]


# --- CLI ------------------------------------------------------------------------

def test_cli_in_with_arg_and_stdin(db, capsys, monkeypatch):
    import io

    from ntasker import cli

    assert cli.main(["--db", str(db), "in", "ntasker: from arg"]) == 0
    assert "#1" in capsys.readouterr().out   # locale-dependent text
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    assert cli.main(["--db", str(db), "in"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    assert cli.main(["--db", str(db), "in", "-"]) == 2
    with get_conn() as conn:
        rows = conn.execute("SELECT text, source, status FROM inbox ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("ntasker: from arg", "cli", "pending"),
        ("from stdin", "cli", "pending"),
    ]


def test_cli_project_summary_set_show_regenerate(db, capsys, monkeypatch):
    from ntasker import cli, triage

    assert cli.main(["--db", str(db), "project", "summary", "x", "--set", "Stored text"]) == 0
    assert cli.main(["--db", str(db), "project", "summary", "x"]) == 0
    assert capsys.readouterr().out.strip().endswith("Stored text")
    monkeypatch.setattr(triage, "summarize_project", lambda name: f"generated {name}")
    assert cli.main(["--db", str(db), "project", "summary", "x", "--regenerate"]) == 0
    assert capsys.readouterr().out.strip() == "generated x"
    assert cli.main(["--db", str(db), "project", "summary", "x", "--set", ""]) == 0
    assert cli.main(["--db", str(db), "project", "summary", "y"]) == 0   # missing -> generated
    assert capsys.readouterr().out.strip().endswith("generated y")
