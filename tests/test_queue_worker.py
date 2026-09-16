"""Queue worker start conditions: lanes, directory locks, the git-clean gate."""

from __future__ import annotations

import subprocess

import pytest

from ntasker import locks, taskqueue
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import set_setting


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    for var in ("NTASKER_DIR_LOCKS", "NTASKER_REQUIRE_CLEAN", "NTASKER_QUEUE_ENABLED"):
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
