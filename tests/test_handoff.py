"""Queue end-of-run rules: only ``done`` retires an entry, and nothing is ever killed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import cli, locks, taskqueue
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    for var in ("NTASKER_DIR_LOCKS", "NTASKER_QUEUE_ENABLED", "NTASKER_TASK_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(taskqueue, "_runnable_agents", lambda: {"claude"})
    live: set[int] = set()
    started: list[int] = []
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: list(live))

    resumed: list[int] = []

    def fake_start(task_id, seed, quick=False, resume=False):
        started.append(task_id)
        if resume:
            resumed.append(task_id)
        live.add(task_id)
        return True

    monkeypatch.setattr(taskqueue, "start_detached_session", fake_start)
    taskqueue._running.clear()
    taskqueue.QUICK.clear()
    taskqueue.RESUME.clear()
    taskqueue._booted = True

    def add(title, project):
        with get_conn() as conn:
            return conn.execute(
                "INSERT INTO tasks (title, project) VALUES (?, ?)", (title, project)
            ).lastrowid

    def col(task_id, name):
        with get_conn() as conn:
            return conn.execute(f"SELECT {name} FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

    def queued():
        return [int(r["id"]) for r in taskqueue.load_queue()]

    return {
        "db": path, "add": add, "col": col, "queued": queued,
        "live": live, "started": started, "resumed": resumed, "monkeypatch": monkeypatch,
    }


def test_in_session_review_handoff_does_not_retire(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    assert env["started"] == [a]
    env["monkeypatch"].setenv("NTASKER_TASK_ID", str(a))
    assert cli.main(["--db", str(env["db"]), "patch", str(a), "--phase", "review"]) == 0
    taskqueue.tick()
    # a waits in review with its session alive; b stays behind it
    assert env["queued"]() == [a, b] and env["started"] == [a] and a in env["live"]


def test_done_retires_without_killing_and_frees_lane(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    taskqueue.tick()
    # a's session is still alive, yet b starts: a done task's session holds no lane
    assert env["queued"]() == [b] and a in env["live"] and env["started"] == [a, b]


def test_done_session_holds_no_dir_locks(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "y")
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET locks = '[\"y\"]' WHERE id = ?", (a,))
    env["monkeypatch"].setenv("NTASKER_DIR_LOCKS", "on")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    assert env["started"] == [a]          # b waits: a holds y
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    taskqueue.tick()
    assert env["started"] == [a, b] and a in env["live"]


def test_ended_session_flags_entry_and_blocks_lane(env):
    a, b, c = env["add"]("A", "x"), env["add"]("B", "x"), env["add"]("C", "y")
    taskqueue.set_queue([a, b, c])
    taskqueue.tick()
    assert env["started"] == [a, c]
    env["live"].discard(a)
    taskqueue.tick()
    assert env["queued"]() == [a, b, c] and env["col"](a, "session_ended_at")
    assert taskqueue.skipped(taskqueue.load_queue(), env["live"])[a]["reason"] == "ended"
    assert env["started"] == [a, c]           # b waits behind the flagged a
    # survives a restart (in-memory state gone)
    taskqueue._running.clear()
    taskqueue.tick()
    assert env["started"] == [a, c]
    # run again clears the flag and restarts
    taskqueue.enqueue(a)
    assert env["col"](a, "session_ended_at") is None
    taskqueue.tick()
    assert env["started"][-1] == a


def test_remove_clears_flag_and_frees_lane(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    assert env["col"](a, "session_ended_at")
    taskqueue.set_queue([b])
    assert env["col"](a, "session_ended_at") is None
    taskqueue.tick()
    assert env["started"] == [a, b]


def test_reorder_keeps_ended_flag(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    taskqueue.set_queue([b, a])   # a drag: never restarts an ended entry
    assert env["col"](a, "session_ended_at")
    taskqueue.tick()
    assert env["started"] == [a]


def test_archived_retires(env):
    a = env["add"]("A", "x")
    taskqueue.set_queue([a])
    taskqueue.tick()
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (a,))
    taskqueue.tick()
    assert env["queued"]() == [] and a in env["live"]


def test_queue_add_top_clears_flag(env):
    a = env["add"]("A", "x")
    taskqueue.set_queue([a])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    assert env["col"](a, "session_ended_at")
    assert cli.main(["--db", str(env["db"]), "queue", "add", str(a), "--top"]) == 0
    assert env["col"](a, "session_ended_at") is None


def test_api_done_leaves_session_alive(env):
    client = TestClient(app, base_url=BASE)
    a = env["add"]("A", "x")
    taskqueue.set_queue([a])
    taskqueue.tick()
    r = client.patch(f"/api/tasks/{a}", json={"status": "done"})
    assert r.status_code == 200 and a in env["live"]


def test_queue_run_route_and_skipped_ended(env):
    client = TestClient(app, base_url=BASE)
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET session_ended_at = 'now' WHERE id = ?", (a,))
    client.put("/api/settings/dir_locks", json={"value": "off"})
    body = client.get("/api/queue").json()
    assert body["skipped"][str(a)]["reason"] == "ended"
    r = client.post("/api/queue/run", json={"id": b})   # already queued: keeps its place
    assert r.status_code == 200 and [t["id"] for t in r.json()["items"]] == [a, b]
    r = client.post("/api/queue/run", json={"id": a})   # run again: clears the flag in place
    assert [t["id"] for t in r.json()["items"]] == [a, b] and r.json()["skipped"] == {}


def _ended_with_session(env, task_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET session_ended_at = 'now', session_id = 'sid', phase = 'review' "
            "WHERE id = ?", (task_id,),
        )


def test_resume_reopens_ended_entry_in_place(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    _ended_with_session(env, a)
    assert taskqueue.request_resume(b) is False      # never ran -> nothing to resume
    assert taskqueue.request_resume(a) is True
    assert a not in taskqueue.skipped(taskqueue.load_queue(), env["live"])
    taskqueue.tick()
    assert env["resumed"] == [a] and env["queued"]() == [a, b]
    assert env["col"](a, "session_ended_at") is None
    assert env["col"](a, "phase") == "review"        # a resume keeps the phase
    assert not taskqueue.RESUME


def test_restart_resumes_flagged_entries_once(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "y")
    taskqueue.set_queue([a, b])
    _ended_with_session(env, a)
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET session_ended_at = 'now' WHERE id = ?", (b,))
    taskqueue._booted = False
    taskqueue.tick()
    assert env["resumed"] == [a]                     # b has no session -> stays flagged
    assert env["col"](b, "session_ended_at")
    # a stopped session later is not resumed on its own
    env["live"].discard(a)
    taskqueue.tick()
    assert env["col"](a, "session_ended_at") and env["resumed"] == [a]


def test_resume_skips_require_clean(env):
    a = env["add"]("A", "x")
    taskqueue.set_queue([a])
    _ended_with_session(env, a)
    taskqueue.clear_ended([a])                       # "run again" -> a fresh start
    env["monkeypatch"].setenv("NTASKER_DIR_LOCKS", "on")
    env["monkeypatch"].setenv("NTASKER_REQUIRE_CLEAN", "on")
    env["monkeypatch"].setattr(locks, "dirty_dir", lambda dirs: next(iter(dirs)))
    taskqueue.tick()
    assert env["started"] == []                      # ... which honours the gate
    assert taskqueue.skipped(taskqueue.load_queue(), env["live"])[a]["reason"] == "dirty"
    taskqueue.request_resume(a)
    assert a not in taskqueue.skipped(taskqueue.load_queue(), env["live"])
    taskqueue.tick()
    assert env["resumed"] == [a]


def test_shutdown_flags_running_entries(env):
    a = env["add"]("A", "x")
    taskqueue.set_queue([a])
    taskqueue.tick()
    assert env["started"] == [a]
    taskqueue.flag_running_ended()
    assert env["col"](a, "session_ended_at")


def test_queue_resume_route(env):
    client = TestClient(app, base_url=BASE)
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    _ended_with_session(env, a)
    assert client.post("/api/queue/resume", json={"id": b}).status_code == 409
    r = client.post("/api/queue/resume", json={"id": a})
    assert r.status_code == 200 and r.json()["skipped"] == {}
