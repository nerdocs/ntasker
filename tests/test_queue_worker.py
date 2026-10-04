"""Queue worker start conditions: lanes, directory locks, the git-clean gate."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from ntasker import locks, taskqueue
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import set_setting


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    for var in (
        "NTASKER_DIR_LOCKS",
        "NTASKER_REQUIRE_CLEAN",
        "NTASKER_QUEUE_ENABLED",
        "NTASKER_QUICKTASKS_BYPASS_LANES",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(taskqueue, "_runnable_agents", lambda: {"claude"})
    live: set[int] = set()
    started: list[int] = []
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: list(live))
    external: set[int] = set()
    monkeypatch.setattr(taskqueue, "external_session_ids", lambda: list(external))

    def fake_start(task_id, seed, quick=False, resume=False):
        started.append(task_id)
        live.add(task_id)
        return True

    monkeypatch.setattr(taskqueue, "start_detached_session", fake_start)
    taskqueue._running.clear()
    taskqueue.QUICK.clear()
    taskqueue.RESUME.clear()
    taskqueue.LANELESS.clear()
    taskqueue._booted = True

    def add(title, project, locks_=()):
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, project, locks) VALUES (?, ?, ?)",
                (title, project, locks.dump(list(locks_))),
            )
            return cur.lastrowid

    return {"add": add, "live": live, "external": external, "started": started, "tmp": tmp_path}


def test_external_session_occupies_lane_and_dirs(env):
    """A terminal `/task` run blocks its project lane and the dirs it holds,
    but is no queue run: it never gets flagged as ended."""
    ext = env["add"]("E", "x", ["y"])
    a = env["add"]("A", "x")
    b = env["add"]("B", "y")
    c = env["add"]("C", "z")
    env["external"].add(ext)
    taskqueue.set_queue([a, b, c])
    taskqueue.tick()
    assert env["started"] == [c]
    skipped = taskqueue.skipped(taskqueue.load_queue(), env["live"])
    assert skipped[b] == {"reason": "lock", "project": "y", "holder": ext}
    env["external"].discard(ext)
    taskqueue.tick()
    assert env["started"] == [c, a, b]


def test_locks_block_across_lanes(env):
    a = env["add"]("A", "x", ["y"])
    b = env["add"]("B", "y")
    c = env["add"]("C", "z", ["y"])
    d = env["add"]("D", "w")
    taskqueue.set_queue([a, b, c, d])
    taskqueue.tick()
    assert env["started"] == [a, d]
    skipped = taskqueue.skipped(taskqueue.load_queue(), env["live"])
    assert skipped[b] == {"reason": "lock", "project": "y", "holder": a}
    assert skipped[c] == {"reason": "lock", "project": "y", "holder": a}
    assert a not in skipped and d not in skipped
    # A ends -> B (head of y) starts; C still waits for B's y.
    env["live"].discard(a)
    taskqueue.tick()
    assert env["started"] == [a, d, b]


def test_finish_releases_locks_and_unblocks_waiting_task(env):
    """A plain `finish` keeps A's own lane busy -- its session lives on for the
    review -- but hands back the repo it only borrowed, so B starts."""
    a = env["add"]("A", "x", ["y"])
    b = env["add"]("B", "y")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    assert env["started"] == [a]
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.post(f"/api/tasks/{a}/outcome", json={"status": "ok"}).json()["locks"] == []
    taskqueue.tick()
    assert env["started"] == [a, b]
    assert taskqueue.skipped(taskqueue.load_queue(), env["live"]) == {}


def test_dir_locks_off_falls_back_to_lanes(env):
    set_setting("dir_locks", "off")
    a = env["add"]("A", "x", ["y"])
    b = env["add"]("B", "y")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    assert env["started"] == [a, b]
    assert taskqueue.skipped(taskqueue.load_queue(), env["live"]) == {}


def test_require_clean_gate(env):
    set_setting("require_clean", "on")
    repo = env["tmp"] / "y"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("x")
    b = env["add"]("B", "y")
    taskqueue.set_queue([b])
    taskqueue.tick()
    assert env["started"] == []
    assert taskqueue.skipped(taskqueue.load_queue(), env["live"])[b] == {
        "reason": "dirty", "project": "y", "holder": None,
    }
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        check=True,
    )
    taskqueue.tick()
    assert env["started"] == [b]


def test_dirty_dir_ignores_non_repos(env):
    d = env["tmp"] / "plain"
    d.mkdir()
    assert locks.dirty_dir({str(d), str(env["tmp"] / "missing")}) is None


def test_laneless_quicktask_neither_waits_for_nor_occupies_the_lane(env):
    """A Quicktask outside the lanes starts next to a live session in its
    project and lets the lane's next task start next to it; ended, it neither
    restarts nor blocks the lane."""
    running = env["add"]("R", "x")
    quick = env["add"]("Q", "x")
    nxt = env["add"]("N", "x")
    env["live"].add(running)
    taskqueue.set_queue([running, quick, nxt])
    taskqueue.LANELESS.add(quick)
    taskqueue.tick()
    assert env["started"] == [quick]
    env["live"].discard(running)
    taskqueue.tick()   # running ended -> lane free although the Quicktask is live
    taskqueue.tick()   # next tick: running is flagged ended and stays; N is blocked by it
    assert env["started"] == [quick]
    taskqueue.set_queue([quick, nxt])
    taskqueue.tick()
    assert env["started"] == [quick, nxt]
    env["live"].discard(quick)
    taskqueue.tick()   # the Quicktask's session ended: flagged, not restarted
    taskqueue.tick()
    assert env["started"] == [quick, nxt]
    assert taskqueue.skipped(taskqueue.load_queue(), env["live"])[quick]["reason"] == "ended"


def test_laneless_quicktask_ignores_dir_locks(env):
    holder = env["add"]("H", "x", ["y"])
    quick = env["add"]("Q", "y")
    env["live"].add(holder)
    taskqueue.set_queue([holder, quick])
    taskqueue.LANELESS.add(quick)
    taskqueue.tick()
    assert env["started"] == [quick]
    assert quick not in taskqueue.skipped(taskqueue.load_queue(), env["live"])


def _queue_ids():
    return [int(r["id"]) for r in taskqueue.load_queue()]


def test_adopted_external_run_joins_queue_at_front(env):
    """A live terminal session attached to a task is queued at once, as its run."""
    a = env["add"]("a", "p")
    b = env["add"]("b", "p")
    taskqueue.enqueue(a)
    env["external"].add(b)
    taskqueue.adopt_running(b)
    assert _queue_ids() == [b, a]
    with get_conn() as conn:
        assert conn.execute("SELECT phase FROM tasks WHERE id = ?", (b,)).fetchone()[0] == "wip"
    taskqueue.tick()
    assert env["started"] == []   # neither re-spawned nor `a` started next to it


def test_adopted_external_run_ending_flags_entry(env):
    """Its process exiting without done blocks the lane like a queued run."""
    a = env["add"]("a", "p")
    b = env["add"]("b", "p")
    taskqueue.enqueue(a)
    env["external"].add(b)
    taskqueue.adopt_running(b)
    taskqueue.tick()
    env["external"].discard(b)
    taskqueue.tick()
    with get_conn() as conn:
        ended = conn.execute("SELECT session_ended_at FROM tasks WHERE id = ?", (b,)).fetchone()[0]
    assert ended is not None
    assert env["started"] == []


def test_adopted_external_run_leaves_queue_on_shutdown(env):
    """A restart must not resume a conversation its terminal still runs."""
    b = env["add"]("b", "p")
    env["external"].add(b)
    taskqueue.adopt_running(b)
    taskqueue.flag_running_ended()
    assert _queue_ids() == []


def test_adopt_running_skips_drafts(env):
    d = env["add"]("d", "p")
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET draft = 1 WHERE id = ?", (d,))
    taskqueue.adopt_running(d)
    assert _queue_ids() == []
