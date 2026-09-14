"""Hand-off rules: only the agent's own in-session hand-off or `done` end a run."""

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
    stopped: list[int] = []
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: list(live))
    monkeypatch.setattr(taskqueue, "stop_session", lambda tid: stopped.append(tid) or True)

    def fake_start(task_id, seed, quick=False):
        started.append(task_id)
        live.add(task_id)
        return True

    monkeypatch.setattr(taskqueue, "start_detached_session", fake_start)
    taskqueue._running.clear()
    taskqueue.QUICK.clear()

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
        "live": live, "started": started, "stopped": stopped, "monkeypatch": monkeypatch,
    }


def _handoff_in_session(env, task_id):
    env["monkeypatch"].setenv("NTASKER_TASK_ID", str(task_id))
    assert cli.main(["--db", str(env["db"]), "patch", str(task_id), "--phase", "review"]) == 0
    env["monkeypatch"].delenv("NTASKER_TASK_ID")


def test_self_handoff_retires_and_stops(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    assert env["started"] == [a]
    _handoff_in_session(env, a)
    assert env["col"](a, "handed_off_at")
    taskqueue.tick()
    assert env["queued"]() == [b] and env["stopped"] == [a]
    env["live"].discard(a)   # stop_session took effect
    taskqueue.tick()
    assert env["started"] == [a, b]
    assert env["col"](a, "handed_off_at") is None


def test_external_review_does_not_retire(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET phase = 'review' WHERE id = ?", (a,))
    assert cli.main(["--db", str(env["db"]), "patch", str(b), "--phase", "review"]) == 0  # no env
    taskqueue.tick()
    assert env["queued"]() == [a, b] and env["stopped"] == [] and env["started"] == [a]


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
    assert env["stopped"] == []
    # survives a restart (in-memory state gone)
    taskqueue._running.clear()
    taskqueue.tick()
    assert env["started"] == [a, c]
    # run again clears the flag and restarts
    taskqueue.enqueue_front(a)
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


def test_done_retires_and_stops_archived_does_not_stop(env):
    a, b = env["add"]("A", "x"), env["add"]("B", "y")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
        conn.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (b,))
    taskqueue.tick()
    assert env["queued"]() == [] and env["stopped"] == [a]


def test_queue_add_top_clears_flag(env):
    a = env["add"]("A", "x")
    taskqueue.set_queue([a])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    assert env["col"](a, "session_ended_at")
    assert cli.main(["--db", str(env["db"]), "queue", "add", str(a), "--top"]) == 0
    assert env["col"](a, "session_ended_at") is None


def test_patch_env_detection(env):
    a = env["add"]("A", "x")
    env["monkeypatch"].setenv("NTASKER_TASK_ID", "999")
    assert cli.main(["--db", str(env["db"]), "patch", str(a), "--phase", "review"]) == 0
    assert env["col"](a, "handed_off_at") is None          # wrong task id
    env["monkeypatch"].setenv("NTASKER_TASK_ID", str(a))
    assert cli.main(["--db", str(env["db"]), "patch", str(a), "--phase", "wip"]) == 0
    assert env["col"](a, "handed_off_at") is None          # not a hand-off
    assert cli.main(["--db", str(env["db"]), "patch", str(a), "--phase", "review"]) == 0
    assert env["col"](a, "handed_off_at")


def test_queue_run_route_and_skipped_ended(env):
    client = TestClient(app, base_url=BASE)
    a, b = env["add"]("A", "x"), env["add"]("B", "x")
    taskqueue.set_queue([a, b])
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET session_ended_at = 'now' WHERE id = ?", (a,))
    client.put("/api/settings/dir_locks", json={"value": "off"})
    body = client.get("/api/queue").json()
    assert body["skipped"][str(a)]["reason"] == "ended"
    r = client.post("/api/queue/run", json={"id": b})
    assert r.status_code == 200 and [t["id"] for t in r.json()["items"]] == [b, a]
    r = client.post("/api/queue/run", json={"id": a})
    assert [t["id"] for t in r.json()["items"]] == [a, b] and r.json()["skipped"] == {}
