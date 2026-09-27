"""The run view's Conversation pane: transcript parsing + API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.transcript import conversation_for, find_transcript, parse_transcript

BASE = "http://127.0.0.1:8766"
SID = "11111111-2222-3333-4444-555555555555"


def _ev(kind, content, **extra):
    role = "user" if kind == "user" else "assistant"
    return json.dumps({"type": kind, "message": {"role": role, "content": content},
                       "timestamp": "2026-09-27T10:00:00Z", **extra})


EVENTS = [
    json.dumps({"type": "mode", "mode": "normal"}),
    _ev("user", "# nTasker task #7: Do it\n\n## Description\n\nplease\n\n## Tracker rules (queued run)\n\n- x"),
    _ev("assistant", [{"type": "thinking", "thinking": "hmm"}]),
    _ev("assistant", [{"type": "text", "text": "Looking."}]),
    _ev("assistant", [{"type": "tool_use", "name": "Bash",
                       "input": {"command": "ls -la", "description": "List files"}}]),
    _ev("user", [{"type": "tool_result", "content": "a b"}]),
    _ev("user", [{"type": "text", "text": "meta"}], isMeta=True),
    _ev("assistant", [{"type": "text", "text": "Done."}], isSidechain=True),
    "{not json",
    _ev("assistant", [{"type": "text", "text": "All done."}]),
    _ev("user", "<command-name>/compact</command-name><command-args></command-args>"),
    _ev("user", "<local-command-stdout>x</local-command-stdout>"),
    _ev("user", [{"type": "text", "text": "and now?"}]),
    _ev("assistant", [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}]),
]


def test_parse_folds_events_into_turns():
    turns = parse_transcript(EVENTS)
    assert [t["prompt"] for t in turns] == [
        "# nTasker task #7: Do it\n\n## Description\n\nplease",   # tracker rules cut
        "/compact",
        "and now?",
    ]
    first = turns[0]
    assert first["answer"] == "Looking.\n\nAll done."   # sidechain + thinking left out
    assert first["tools"] == [{"name": "Bash", "detail": "List files"}]
    assert turns[1]["answer"] == "" and turns[1]["tools"] == []
    assert turns[2]["tools"] == [{"name": "Read", "detail": "/a/b.py"}]
    assert turns[2]["answer_at"] == "2026-09-27T10:00:00Z"


def test_parse_cuts_configured_run_rules():
    seed = "# nTasker task #7: Do it\n\nplease\n\n## My rules\n\n- finish 7\n\n## Fasttrack\n\n- go"
    turns = parse_transcript([_ev("user", seed)], rules=("## My rules\n\n- finish 7", "## Fasttrack\n\n- go"))
    assert turns[0]["prompt"] == "# nTasker task #7: Do it\n\nplease"
    # A prompt that is not a queue seed is left alone.
    turns = parse_transcript([_ev("user", "## My rules\n\n- finish 7")], rules=("## My rules\n\n- finish 7",))
    assert turns[0]["prompt"] == "## My rules\n\n- finish 7"


def test_find_transcript_rejects_path_tricks(tmp_path):
    proj = tmp_path / "projects" / "-some-cwd"
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text("\n".join(EVENTS))
    assert find_transcript(tmp_path, SID) == proj / f"{SID}.jsonl"
    assert find_transcript(tmp_path, "../x") is None
    assert find_transcript(tmp_path, "") is None
    assert conversation_for(tmp_path, "nope")["available"] is False
    assert len(conversation_for(tmp_path, SID)["turns"]) == 3


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    home = tmp_path / "claude"
    (home / "projects" / "-cwd").mkdir(parents=True)
    (home / "projects" / "-cwd" / f"{SID}.jsonl").write_text("\n".join(EVENTS))
    monkeypatch.setenv("NTASKER_CLAUDE_HOME", str(home))
    return TestClient(app, base_url=BASE)


def test_api_conversation(client):
    tid = client.post("/api/tasks", json={"title": "t", "agent": "claude"}).json()["id"]
    d = client.get(f"/api/tasks/{tid}/conversation").json()
    assert d["supported"] is True and d["available"] is False   # never ran
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (SID, tid))
    d = client.get(f"/api/tasks/{tid}/conversation").json()
    assert d["available"] is True and len(d["turns"]) == 3 and d["updated"]
    assert client.get("/api/tasks/9999/conversation").status_code == 404
