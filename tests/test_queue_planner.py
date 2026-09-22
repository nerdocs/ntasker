"""The queue planner: an LLM orders the queue, the queue still executes it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner
from ntasker.app import app
from ntasker.claude_runner import PLANNER_TASK_ID, planner_seed
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    claude_runner.SESSIONS.clear()
    monkeypatch.setattr(claude_runner, "terminal_available", lambda spec: (True, None))
    return TestClient(app, base_url=BASE)


class _FakeSession:
    """Just enough of a TermSession for the registry: alive, hooks reported."""

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.alive = True
        self.hook_waiting = False


@pytest.fixture
def spawned(monkeypatch):
    """Record every spawn instead of forking a real agent."""
    calls: list[tuple[int, str | None]] = []

    def fake_start(task_id, *, seed=None, resume=False, quick=False):
        calls.append((task_id, seed))
        sess = _FakeSession(task_id)
        claude_runner.SESSIONS[task_id] = sess
        return sess

    monkeypatch.setattr(claude_runner, "_start_session", fake_start)
    return calls


def test_seed_forbids_creating_closing_and_archiving():
    seed = planner_seed()
    assert "Never create a task" in seed
    assert "never set one to `done`" in seed
    assert "never archive or delete one" in seed


def test_seed_names_the_endpoints_it_may_use():
    seed = planner_seed()
    assert "GET /api/tasks?status=open&archived=false" in seed
    assert "PUT /api/queue" in seed
    assert "PUT /api/settings/queue_enabled" in seed


def test_plan_starts_a_session_with_the_planner_seed(client, spawned):
    r = client.post("/api/queue/plan", json={})
    assert r.status_code == 201, r.text
    assert r.json() == {"id": PLANNER_TASK_ID}
    assert [tid for tid, _seed in spawned] == [PLANNER_TASK_ID]
    assert spawned[0][1] == planner_seed()


def test_second_planner_is_refused(client, spawned):
    assert client.post("/api/queue/plan", json={}).status_code == 201
    r = client.post("/api/queue/plan", json={})
    assert r.status_code == 409
    assert len(spawned) == 1


def test_planner_session_is_labelled_in_the_session_list(client, spawned):
    client.post("/api/queue/plan", json={})
    d = client.get("/api/claude/sessions").json()
    assert PLANNER_TASK_ID in d["active"]
    assert d["titles"][str(PLANNER_TASK_ID)]


def test_planner_occupies_no_project_lane(client, spawned):
    """It has no task row, so it blocks nothing: the queue keeps starting tasks."""
    from ntasker import taskqueue

    client.post("/api/tasks", json={"title": "t", "project": "p"}).json()
    client.post("/api/queue/plan", json={})
    with taskqueue.get_conn() as conn:
        assert taskqueue._busy_buckets({PLANNER_TASK_ID}, conn) == set()
