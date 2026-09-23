"""Discovering terminal sessions from their transcripts, and ending a live one."""

from __future__ import annotations

import json
import os

import pytest

from ntasker import sessions
from ntasker.db import init_db, set_db_path

SID = "12345678-1234-1234-1234-123456789abc"
OTHER = "87654321-4321-4321-4321-cba987654321"


def write_transcript(home, project_dir, session_id, prompt, cwd=None):
    """Lay down a transcript the way Claude Code does: one dir per working
    directory, named after it with every ``/`` turned into ``-``."""
    slug = str(project_dir).replace("/", "-")
    folder = home / "projects" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "mode", "mode": "normal"}),
                json.dumps({"type": "assistant", "cwd": str(cwd or project_dir)}),
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-09-23T10:00:00Z",
                        "message": {"role": "user", "content": prompt},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    monkeypatch.setattr(sessions, "LIVE", {})
    return tmp_path / "claude"


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A project directory below a configured projects base, so discovery
    names it the way the rest of ntasker does."""
    base = tmp_path / "Projekte"
    repo = base / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("NTASKER_PROJECTS_BASE", str(base))
    return repo


def test_discovers_transcripts_newest_first(home, project_dir):
    write_transcript(home, project_dir, OTHER, "older question")
    newer = write_transcript(home, project_dir, SID, "newer question")
    os.utime(newer, (2_000_000_000, 2_000_000_000))
    found = sessions.discover_for_project("repo", home)
    assert [s.session_id for s in found] == [SID, OTHER]
    assert found[0].preview == "newer question"
    assert found[0].cwd == str(project_dir)


def test_empty_transcript_is_skipped(home, project_dir):
    path = write_transcript(home, project_dir, SID, "")
    path.write_text(json.dumps({"type": "mode"}) + "\n", encoding="utf-8")
    assert sessions.discover_for_project("repo", home) == []


def test_preview_strips_wrapping(home, project_dir):
    write_transcript(home, project_dir, SID, "<seed>## Task #7</seed>\n\n  do   the thing  ")
    assert sessions.discover_for_project("repo", home)[0].preview == "Task #7 do the thing"


def test_known_task_is_reported(home, project_dir):
    from ntasker.claude_runner import bind_session
    from ntasker.db import get_conn

    write_transcript(home, project_dir, SID, "q")
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO tasks (title) VALUES ('t')")
        task_id = cur.lastrowid
    bind_session(task_id, SID, str(project_dir))
    assert sessions.discover_for_project("repo", home)[0].task_id == task_id


def test_no_project_discovers_nothing(home):
    assert sessions.discover_for_project(None, home) == []


def test_live_registry_tracks_and_forgets(home):
    sessions.register_live(SID, os.getpid(), "/tmp")
    assert sessions.live_pid(SID) == os.getpid()
    sessions.register_live(OTHER, 2**30, "/tmp")  # a pid that is not running
    assert sessions.live_pid(OTHER) is None
    assert OTHER not in sessions.LIVE


def test_end_live_signals_and_waits_for_the_exit(home, monkeypatch):
    """Ending has to leave the transcript free -- the caller resumes it next."""
    killed = []
    monkeypatch.setattr(sessions.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    # Alive until the signal arrives, gone right after.
    monkeypatch.setattr(sessions, "_pid_alive", lambda pid: not killed)
    sessions.register_live(SID, os.getpid(), "/tmp")
    assert sessions.end_live(SID) is True
    assert killed == [(os.getpid(), sessions.signal.SIGTERM)]


def test_end_live_reports_a_process_that_will_not_go(home, monkeypatch):
    monkeypatch.setattr(sessions.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(sessions, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(sessions, "_END_TIMEOUT", 0.2)
    sessions.register_live(SID, os.getpid(), "/tmp")
    assert sessions.end_live(SID) is False


def test_end_live_without_a_live_process(home):
    assert sessions.end_live(SID) is False


# --- API -------------------------------------------------------------------


@pytest.fixture
def client(home):
    from fastapi.testclient import TestClient

    from ntasker.app import app

    return TestClient(app, base_url="http://127.0.0.1:8766")


def test_live_endpoint_registers_a_terminal_session(client):
    r = client.post(
        "/api/claude/sessions/live",
        json={"session_id": SID, "pid": os.getpid(), "cwd": "/tmp"},
    )
    assert r.status_code == 200
    assert sessions.live_pid(SID) == os.getpid()


def test_live_endpoint_rejects_a_bad_id(client):
    assert client.post(
        "/api/claude/sessions/live", json={"session_id": "x", "pid": 1, "cwd": "/tmp"}
    ).status_code == 422


def test_discovered_endpoint_lists_a_project(client, home, project_dir, monkeypatch):
    write_transcript(home, project_dir, SID, "what the user asked")
    # The endpoint reads the real Claude home; point discovery at the fake one.
    real = sessions.discover_for_project
    monkeypatch.setattr(sessions, "discover_for_project", lambda p, h=None: real(p, home))
    body = client.get("/api/claude/sessions/discovered?project=repo").json()
    assert [s["session_id"] for s in body["sessions"]] == [SID]
    assert body["sessions"][0]["preview"] == "what the user asked"
    assert body["sessions"][0]["live"] is False


def test_discovered_endpoint_without_a_project(client):
    assert client.get("/api/claude/sessions/discovered").json() == {"sessions": []}


def test_end_endpoint_reports_a_session_that_is_not_running(client):
    r = client.post(f"/api/claude/sessions/discovered/{SID}/end")
    assert r.status_code == 409


def test_end_endpoint_rejects_a_bad_id(client):
    assert client.post("/api/claude/sessions/discovered/nope/end").status_code == 422


def test_end_endpoint_terminates_a_live_session(client, monkeypatch):
    killed = []
    monkeypatch.setattr(sessions.os, "kill", lambda pid, sig: killed.append(sig))
    monkeypatch.setattr(sessions, "_pid_alive", lambda pid: not killed)
    sessions.register_live(SID, os.getpid(), "/tmp")
    assert client.post(f"/api/claude/sessions/discovered/{SID}/end").status_code == 200
    assert killed == [sessions.signal.SIGTERM]
