"""Fasttrack tasks: ``ntasker finish``, the run log (``run_outcomes``) and the seed."""

from __future__ import annotations

import io
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from ntasker import app as app_module
from ntasker import cli, locks, rundiff, taskqueue
from ntasker.app import app
from ntasker.claude_runner import queue_seed_for_task
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.settings import FASTTRACK_RULES_DEFAULT

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    for var in ("NTASKER_DIR_LOCKS", "NTASKER_QUEUE_ENABLED", "NTASKER_TASK_ID", "NTASKER_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(taskqueue, "_runnable_agents", lambda: {"claude"})
    live: set[int] = set()
    started: list[int] = []
    stopped: list[int] = []
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: list(live))
    monkeypatch.setattr(app_module, "stop_session", lambda tid: stopped.append(tid) or True)
    # No git in the endpoint / worker tests: a fixed "derived" file list.
    monkeypatch.setattr(app_module, "changed_paths", lambda b: ["derived.py"] if b else [])
    monkeypatch.setattr(taskqueue, "changed_paths", lambda b: ["derived.py"] if b else [])

    def fake_start(task_id, seed, quick=False, resume=False):
        started.append(task_id)
        live.add(task_id)
        return True

    monkeypatch.setattr(taskqueue, "start_detached_session", fake_start)
    taskqueue._running.clear()
    taskqueue.QUICK.clear()
    taskqueue.RESUME.clear()
    taskqueue._booted = True

    def add(title, project="x", fasttrack=0, fail_continue=0):
        with get_conn() as conn:
            return conn.execute(
                "INSERT INTO tasks (title, project, fasttrack, fail_continue, run_baselines, report) "
                "VALUES (?, ?, ?, ?, '{\"/d\": null}', 'old report')",
                (title, project, fasttrack, fail_continue),
            ).lastrowid

    def col(task_id, name):
        with get_conn() as conn:
            return conn.execute(f"SELECT {name} FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

    def outcomes():
        with get_conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM run_outcomes ORDER BY id").fetchall()]

    return {
        "db": path, "add": add, "col": col, "outcomes": outcomes, "live": live,
        "started": started, "stopped": stopped, "monkeypatch": monkeypatch,
        "client": TestClient(app, base_url=BASE),
        "queued": lambda: [int(r["id"]) for r in taskqueue.load_queue()],
    }


# --- schema / flags -----------------------------------------------------------


def test_schema_migrates_old_db(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    init_db(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE tasks DROP COLUMN fasttrack")
    conn.execute("ALTER TABLE tasks DROP COLUMN fail_continue")
    conn.execute("DROP TABLE run_outcomes")
    conn.commit()
    conn.close()
    init_db(path)
    init_db(path)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"fasttrack", "fail_continue"} <= cols
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'run_outcomes'").fetchone()


def test_flags_roundtrip_api(env):
    c = env["client"]
    t = c.post("/api/tasks", json={"title": "a"}).json()
    assert t["fasttrack"] is False and t["fail_continue"] is False
    t = c.post("/api/tasks", json={"title": "b", "fasttrack": True, "fail_continue": True}).json()
    assert t["fasttrack"] is True and t["fail_continue"] is True
    t = c.patch(f"/api/tasks/{t['id']}", json={"fasttrack": False}).json()
    assert t["fasttrack"] is False and t["fail_continue"] is True
    assert c.get(f"/api/tasks/{t['id']}").json()["fail_continue"] is True


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def test_changed_paths_single_and_multi_dir(tmp_path):
    repos = []
    for name in ("one", "two"):
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("a\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        repos.append(repo)
    base = {str(r): rundiff.git_head(str(r)) for r in repos}
    (repos[0] / "a.txt").write_text("changed\n")
    (repos[1] / "new.txt").write_text("n\n")
    assert rundiff.changed_paths({str(repos[0]): base[str(repos[0])]}) == ["a.txt"]
    assert rundiff.changed_paths(base) == ["one/a.txt", "two/new.txt"]
    assert rundiff.changed_paths({}) == []


# --- POST /api/tasks/{id}/outcome ---------------------------------------------


def test_finish_plain_ok_moves_to_review(env):
    a = env["add"]("A")
    r = env["client"].post(f"/api/tasks/{a}/outcome", json={"status": "ok", "report": "## done"})
    assert r.status_code == 200 and r.json()["phase"] == "review" and r.json()["outcome_id"] is None
    assert env["col"](a, "report") == "## done"
    assert env["outcomes"]() == [] and env["stopped"] == []


def test_finish_plain_failed_keeps_phase(env):
    a = env["add"]("A")
    r = env["client"].post(f"/api/tasks/{a}/outcome", json={"status": "failed", "report": "why"})
    assert r.json()["phase"] == "planned" and env["col"](a, "report") == "why"
    assert env["outcomes"]() == [] and env["stopped"] == []


def test_finish_fasttrack_ok_done_and_row(env):
    a = env["add"]("A", fasttrack=1)
    taskqueue.set_queue([a])
    r = env["client"].post(
        f"/api/tasks/{a}/outcome",
        json={"status": "ok", "summary": "did it", "commit": "abc123", "report": "## r",
              "next_tasks": ["tidy b", "  ", "docs"]},
    )
    assert r.json()["status"] == "done" and env["col"](a, "completed_at")
    assert env["stopped"] == [a]
    (row,) = env["outcomes"]()
    assert row["status"] == "ok" and row["summary"] == "did it" and row["commit_sha"] == "abc123"
    assert row["title"] == "A" and row["project"] == "x" and row["report"] == "## r"
    assert json.loads(row["files_changed"]) == ["derived.py"]
    assert json.loads(row["next_tasks"]) == ["tidy b", "docs"]
    assert r.json()["outcome_id"] == row["id"]
    taskqueue.tick()
    assert env["queued"]() == []


def test_finish_fasttrack_failed_fail_continue_dequeues(env):
    a, b = env["add"]("A", fasttrack=1, fail_continue=1), env["add"]("B")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    assert env["started"] == [a]
    r = env["client"].post(f"/api/tasks/{a}/outcome", json={"status": "failed", "summary": "nope"})
    assert r.json()["status"] == "open" and r.json()["queue_order"] is None
    assert env["stopped"] == [a] and env["queued"]() == [b]
    (row,) = env["outcomes"]()
    assert row["status"] == "failed" and row["summary"] == "nope"
    # Lane is free once the session is gone: B starts.
    env["live"].discard(a)
    taskqueue.tick()
    assert env["started"] == [a, b]


def test_finish_fasttrack_failed_blocks_lane(env):
    a, b = env["add"]("A", fasttrack=1), env["add"]("B")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    env["client"].post(f"/api/tasks/{a}/outcome", json={"status": "blocked", "report": "first line\nmore"})
    assert env["stopped"] == [] and env["queued"]() == [a, b]
    (row,) = env["outcomes"]()
    assert row["status"] == "blocked" and row["summary"] == "first line"
    taskqueue.tick()
    assert env["started"] == [a]


def test_finish_files_override_and_report_kept(env):
    a = env["add"]("A", fasttrack=1)
    env["client"].post(f"/api/tasks/{a}/outcome", json={"status": "ok", "files": ["x.py"]})
    (row,) = env["outcomes"]()
    assert json.loads(row["files_changed"]) == ["x.py"]
    assert env["col"](a, "report") == "old report" and row["report"] == "old report"
    assert row["summary"] == "ok"


def test_finish_404_and_422(env):
    a = env["add"]("A")
    assert env["client"].post("/api/tasks/9999/outcome", json={"status": "ok"}).status_code == 404
    assert env["client"].post(f"/api/tasks/{a}/outcome", json={"status": "meh"}).status_code == 422


# --- worker: ended rows -------------------------------------------------------


def test_tick_ended_inserts_outcome_once(env):
    a = env["add"]("A", fasttrack=1)
    taskqueue.set_queue([a])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    taskqueue.tick()
    (row,) = env["outcomes"]()
    assert row["status"] == "ended" and row["summary"] == taskqueue.ENDED_SUMMARY
    assert json.loads(row["files_changed"]) == ["derived.py"] and row["report"] == "old report"
    assert env["col"](a, "session_ended_at") and env["queued"]() == [a]


def test_tick_ended_fail_continue_dequeues(env):
    a, b = env["add"]("A", fasttrack=1, fail_continue=1), env["add"]("B")
    taskqueue.set_queue([a, b])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    assert env["queued"]() == [b] and env["started"] == [a, b]
    assert [o["status"] for o in env["outcomes"]()] == ["ended"]
    assert env["col"](a, "session_ended_at") is None


def test_tick_ended_plain_task_no_outcome(env):
    a = env["add"]("A")
    taskqueue.set_queue([a])
    taskqueue.tick()
    env["live"].discard(a)
    taskqueue.tick()
    assert env["outcomes"]() == [] and env["col"](a, "session_ended_at")


# --- run log endpoints ---------------------------------------------------------


def test_outcomes_list_ack_and_task_delete(env):
    a = env["add"]("A", fasttrack=1)
    c = env["client"]
    c.post(f"/api/tasks/{a}/outcome", json={"status": "failed", "next_tasks": ["n1"]})
    c.post(f"/api/tasks/{a}/outcome", json={"status": "ok", "commit": "abc"})
    items = c.get("/api/outcomes").json()
    assert [o["status"] for o in items] == ["ok", "failed"]
    assert items[0]["commit"] == "abc" and items[1]["next_tasks"] == ["n1"]
    assert items[1]["files_changed"] == ["derived.py"]
    assert c.delete(f"/api/tasks/{a}").status_code == 204
    items = c.get("/api/outcomes").json()
    assert items[0]["task_id"] is None and items[0]["title"] == "A"
    assert c.delete(f"/api/outcomes/{items[0]['id']}").status_code == 204
    assert c.delete(f"/api/outcomes/{items[0]['id']}").status_code == 404
    assert len(c.get("/api/outcomes").json()) == 1


# --- seed ---------------------------------------------------------------------


def test_seed_fasttrack_rules_only_when_flagged(env):
    plain = {"id": 5, "title": "t", "status": "open", "priority": "normal"}
    assert FASTTRACK_RULES_DEFAULT.replace("{id}", "5") not in queue_seed_for_task(plain)
    seed = queue_seed_for_task({**plain, "fasttrack": True})
    assert seed.endswith(FASTTRACK_RULES_DEFAULT.replace("{id}", "5"))
    assert seed.index("Tracker rules") < seed.index("Fasttrack (this task")


def test_seed_lists_dependency_outcomes(env):
    a, b, c = env["add"]("Up A", fasttrack=1), env["add"]("Up B"), env["add"]("Down")
    env["client"].post(
        f"/api/tasks/{a}/outcome",
        json={"status": "ok", "summary": "done A", "report": "SECRET", "next_tasks": ["refactor z"]},
    )
    env["client"].patch(f"/api/tasks/{c}", json={"depends": [a, b]})
    seed = queue_seed_for_task({"id": c, "title": "Down", "status": "open", "priority": "normal"})
    assert "## Results of tasks this one depends on" in seed
    assert f"- #{a} Up A -- ok: done A" in seed
    assert "  - suggested follow-up: refactor z" in seed
    assert f"- #{b} Up B -- (no run outcome recorded)" in seed
    assert "SECRET" not in seed
    plain = queue_seed_for_task({"id": a, "title": "Up A", "status": "open", "priority": "normal"})
    assert "Results of tasks" not in plain


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def fake_server(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def api_call(base, method, path, body=None, timeout=5.0):
        calls.append((method, path, body))
        return 200, {"id": 7, "outcome_id": 1}

    monkeypatch.setattr(cli, "_api_call", api_call)
    return calls


def test_cli_finish_posts_to_server(monkeypatch, fake_server, capsys):
    monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:8766")
    monkeypatch.setattr("sys.stdin", io.StringIO("## report\n"))
    rc = cli.main(["finish", "7", "--status", "ok", "--summary", "s", "--commit", "abc",
                   "--next", "n1", "--next", "n2"])
    assert rc == 0
    ((method, path, body),) = fake_server
    assert (method, path) == ("POST", "/api/tasks/7/outcome")
    assert body == {"status": "ok", "summary": "s", "commit": "abc", "report": "## report\n",
                    "next_tasks": ["n1", "n2"]}
    assert capsys.readouterr().out.startswith("#7 ")   # locale-dependent wording


def test_cli_finish_files_override(monkeypatch, fake_server):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["finish", "7", "--status", "failed", "--files", "a.py, b.py"]) == 0
    assert fake_server[0][2]["files"] == ["a.py", "b.py"]


def test_cli_finish_server_down_exits_1(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(cli, "_api_call", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.setenv(var, "C")
    assert cli.main(["finish", "7", "--status", "ok"]) == 1
    assert "not reachable" in capsys.readouterr().err


def test_cli_add_patch_flags(env, capsys):
    db = str(env["db"])
    assert cli.main(["--db", db, "add", "--title", "F", "--fasttrack", "--fail-continue"]) == 0
    tid = int(capsys.readouterr().out.split()[0].lstrip("#").rstrip(":"))
    assert env["col"](tid, "fasttrack") == 1 and env["col"](tid, "fail_continue") == 1
    assert cli.main(["--db", db, "patch", str(tid), "--no-fasttrack"]) == 0
    assert env["col"](tid, "fasttrack") == 0 and env["col"](tid, "fail_continue") == 1
    cli.main(["--db", db, "show", str(tid)])
    assert "Fasttrack" in capsys.readouterr().out
