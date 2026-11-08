"""Server side of the worker link: re-adoption after a dropped link or restart."""

from __future__ import annotations

import base64

import pytest

from ntasker import claude_runner, workers
from ntasker.claude_runner import SESSIONS, TermSession
from ntasker.db import get_conn, init_db, set_db_path


class _Conn(workers.WorkerConn):
    """A WorkerConn without socket or loop -- records what it would send."""

    def __init__(self, user_id=2, name="laptop"):  # noqa: D107 -- test double
        self.user_id, self.name, self.hostname = user_id, name, ""
        self.connected_at = 0.0
        self.pending = {}
        self.sent: list[dict] = []

    def send(self, msg):
        self.sent.append(msg)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    yield path
    SESSIONS.clear()


def test_reconnect_takes_runs_back_and_ends_the_unreported(db):
    old, new = _Conn(), _Conn()
    SESSIONS[1] = TermSession(task_id=1, worker=old)
    SESSIONS[2] = TermSession(task_id=2, worker=old)
    workers._adopt(new, [{"task_id": 1, "alive": True, "buffer": base64.b64encode(b"tail").decode()}])
    assert SESSIONS[1].worker is new and bytes(SESSIONS[1].buffer) == b"tail"
    assert 2 not in SESSIONS  # not reported -> ended (nobody watching -> dropped)


def test_server_restart_registers_reported_runs(db):
    conn = _Conn()
    workers._adopt(conn, [{"task_id": 7, "alive": True, "cwd": "/w/p", "buffer": ""}])
    assert SESSIONS[7].worker is conn and SESSIONS[7].alive and SESSIONS[7].cwd == "/w/p"


def test_other_workers_runs_are_left_alone(db):
    mine, other = _Conn(name="laptop"), _Conn(name="desktop")
    SESSIONS[3] = TermSession(task_id=3, worker=other)
    workers._adopt(mine, [])
    assert SESSIONS[3].alive


def test_start_and_started_record_the_session(db):
    with get_conn() as conn:
        task_id = conn.execute("INSERT INTO tasks (title) VALUES ('t')").lastrowid
    conn = _Conn()
    spec = claude_runner.spawn_spec(task_id, seed="go", remote=True)
    assert spec["dir_locks"] is False
    conn.start(spec)
    assert conn.sent[-1]["type"] == "start" and SESSIONS[task_id].worker is conn
    workers._dispatch(conn, {"type": "started", "task_id": task_id, "cwd": "/w/p", "baselines": '{"/w/p": "abc"}'})
    with get_conn() as db_conn:
        row = db_conn.execute("SELECT session_id, session_cwd, run_baselines FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["session_id"] == spec["session_id"] and row["session_cwd"] == "/w/p"
    assert row["run_baselines"] == '{"/w/p": "abc"}'
    workers._dispatch(conn, {"type": "output", "task_id": task_id, "data": base64.b64encode(b"hi").decode()})
    assert bytes(SESSIONS[task_id].buffer) == b"hi"
    workers._dispatch(conn, {"type": "exit", "task_id": task_id, "code": 0})
    assert task_id not in SESSIONS


def test_io_goes_through_the_worker(db):
    conn = _Conn()
    sess = TermSession(task_id=5, worker=conn)
    claude_runner._resize(sess, 30, 100)
    claude_runner._stop(sess)
    import asyncio  # noqa: PLC0415

    asyncio.run(claude_runner._write_pty(sess, b"x"))
    assert [m["type"] for m in conn.sent] == ["resize", "stop", "input"]


def test_transcript_and_diff_mirror_round_trip(db, tmp_path, monkeypatch):
    from ntasker import auth, worker as worker_mod  # noqa: PLC0415
    from ntasker.transcript import find_transcript  # noqa: PLC0415

    sid = "11111111-2222-3333-4444-555555555555"
    with get_conn() as conn:
        auth.create_user(conn, "alice", "pw")  # id 2
        task_id = conn.execute("INSERT INTO tasks (title, assignee_id) VALUES ('t', 2)").lastrowid
    src = tmp_path / "home" / "projects" / "x" / f"{sid}.jsonl"
    src.parent.mkdir(parents=True)
    src.write_bytes(b'{"a": 1}\n')

    w = worker_mod.Worker("http://s", "tok", "laptop")
    sent: list[dict] = []
    w._out = object()  # "connected"
    w._send = sent.append
    monkeypatch.setattr(worker_mod, "resolve_home", lambda spec: tmp_path / "home")
    monkeypatch.setattr(worker_mod, "run_diff", lambda baselines: {"git": True, "files": [{"path": "a"}]})
    w.runs[task_id] = {"agent": "claude", "session_id": sid, "baselines": '{"/x": "abc"}',
                       "offset": 0, "diff": None, "diff_at": 0.0}
    w._sync(task_id)
    with src.open("ab") as fh:
        fh.write(b'{"b": 2}\n')
    w._sync(task_id, final=True)
    assert [m["type"] for m in sent] == ["transcript", "diff", "transcript"]

    conn = _Conn(user_id=2)
    for msg in sent:
        workers._dispatch(conn, msg)
    mirrored = find_transcript(workers.mirror_home(task_id), sid)
    assert mirrored.read_bytes() == src.read_bytes()
    assert workers.stored_diff(task_id) == {"git": True, "files": [{"path": "a"}]}
    # Someone else's worker cannot write into this task's mirror.
    workers._dispatch(_Conn(user_id=99), {"type": "diff", "task_id": task_id, "diff": {"git": False, "files": []}})
    assert workers.stored_diff(task_id)["git"] is True


def test_service_unit_for_a_worker_lives_beside_the_server():
    from ntasker import service  # noqa: PLC0415

    worker = service._systemd_units("127.0.0.1", 8766, None, False, worker=True)[0]
    server = service._systemd_units("127.0.0.1", 8766, None, False)[0]
    assert worker.path.name == "ntasker-worker.service" and server.path.name == "ntasker.service"
    assert "-m ntasker worker" in worker.content and "--port" not in worker.content
    plist = service._launchd_units("127.0.0.1", 8766, None, False, worker=True)[0]
    assert plist.path.name == "at.nerdocs.ntasker-worker.plist"


def test_service_unit_carries_the_allowed_host():
    from ntasker import service  # noqa: PLC0415

    unit = service._systemd_units("127.0.0.1", 8766, None, False, allowed_host="tasks.example.org")[0]
    assert "Environment=NTASKER_ALLOWED_HOSTS=tasks.example.org\nExecStart=" in unit.content
    assert "Environment=" not in service._systemd_units("127.0.0.1", 8766, None, False)[0].content
    plist = service._launchd_units("127.0.0.1", 8766, None, False, allowed_host="tasks.example.org")[0]
    assert "NTASKER_ALLOWED_HOSTS" in plist.content


def test_flagged_run_that_is_live_again_is_not_resumed(db, monkeypatch):
    """A worker's run outlives a server restart: once the worker hands it back,
    the shutdown's "ended" flag goes and the queue does not resume it later."""
    from ntasker import taskqueue  # noqa: PLC0415

    with get_conn() as conn:
        task_id = conn.execute(
            "INSERT INTO tasks (title, queue_order, session_id, session_ended_at) "
            "VALUES ('t', 1, '11111111-2222-3333-4444-555555555555', '2026-01-01T00:00:00')"
        ).lastrowid
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: [task_id])
    monkeypatch.setattr(taskqueue, "external_session_ids", lambda: [])
    monkeypatch.setattr(taskqueue, "_booted", False)
    taskqueue.tick()
    with get_conn() as conn:
        assert conn.execute("SELECT session_ended_at FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] is None
    assert task_id not in taskqueue.RESUME
    taskqueue._running.clear()
