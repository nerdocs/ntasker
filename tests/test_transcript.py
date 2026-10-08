"""The run view's Conversation pane: transcript parsing + API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.transcript import conversation_for, find_transcript, last_activity, parse_transcript

BASE = "http://127.0.0.1:8766"
SID = "11111111-2222-3333-4444-555555555555"


def _ev(kind, content, **extra):
    role = "user" if kind == "user" else "assistant"
    msg = {"role": role, "content": content}
    for key in ("id", "usage", "stop_reason"):
        if key in extra:
            msg[key] = extra.pop(key)
    return json.dumps({"type": kind, "message": msg, "timestamp": "2026-09-27T10:00:00Z", **extra})


USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
         "output_tokens": 50}

EVENTS = [
    json.dumps({"type": "mode", "mode": "normal"}),
    _ev("user", "# nTasker task #7: Do it\n\nProject: p | Status: open | Priority: normal\n\n"
                "## Description\n\nplease\n\n## Tracker rules (queued run)\n\n- x"),
    _ev("assistant", [{"type": "thinking", "thinking": "hmm"}], id="m1", usage=USAGE, stop_reason="tool_use"),
    _ev("assistant", [{"type": "text", "text": "Looking."}], id="m1", usage=USAGE, stop_reason="tool_use"),
    _ev("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                       "input": {"command": "ls -la", "description": "List files"}}],
        id="m1", usage=USAGE, stop_reason="tool_use"),
    _ev("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "a b"}]),
    _ev("assistant", [{"type": "tool_use", "id": "t2", "name": "Edit",
                       "input": {"file_path": "/r/src/a.py", "old_string": "x", "new_string": "y"}}],
        id="m2", usage=USAGE, stop_reason="tool_use"),
    _ev("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]),
    _ev("user", [{"type": "text", "text": "meta"}], isMeta=True),
    _ev("assistant", [{"type": "text", "text": "Done."}], isSidechain=True),
    "{not json",
    _ev("assistant", [{"type": "text", "text": "All done."}], id="m3", usage=USAGE, stop_reason="end_turn"),
    _ev("user", "<command-name>/compact</command-name><command-args></command-args>"),
    _ev("user", "<local-command-stdout>x</local-command-stdout>"),
    _ev("user", [{"type": "text", "text": "and now?"}]),
    _ev("assistant", [{"type": "text", "text": "Checking."}], id="m4", stop_reason="tool_use"),
    _ev("assistant", [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "rm -rf build"}}],
        id="m4", stop_reason="tool_use"),
]


def test_parse_folds_events_into_turns():
    turns = parse_transcript(EVENTS)
    assert [t["prompt"] for t in turns] == [
        "please",   # heading, facts line, description heading and tracker rules cut
        "/compact",
        "and now?",
    ]
    first = turns[0]
    assert first["seed"] is True and turns[2]["seed"] is False
    assert first["done"] is True
    assert first["answer"] == "All done."            # the final text leads
    assert first["progress"] == ["Looking."]         # sidechain + thinking left out
    assert first["tools"] == [
        {"name": "Bash", "kind": "bash", "detail": "List files", "file": None},
        {"name": "Edit", "kind": "edit", "detail": "/r/src/a.py", "file": "/r/src/a.py"},
    ]
    # usage is counted once per message id, not once per content block
    assert first["usage"] == {"input": 3 * 1102, "cache_read": 3000, "cache_write": 300, "output": 150}
    assert turns[1]["done"] is True and turns[1]["answer"] == ""


def test_running_turn_has_progress_and_pending_call():
    last = parse_transcript(EVENTS)[-1]
    assert last["done"] is False and last["answer"] == ""
    assert last["progress"] == ["Checking."]
    assert last["pending"] == {"name": "Bash", "kind": "bash", "detail": "rm -rf build", "file": None}


def test_pending_question_carries_its_options():
    ask = {"questions": [{"question": "Which?", "multiSelect": False,
                          "options": [{"label": "A", "description": ""}, {"label": "B"}]}]}
    turns = parse_transcript([_ev("user", "go"),
                              _ev("assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion",
                                                 "input": ask}], id="m")])
    assert turns[0]["pending"]["kind"] == "ask"
    assert turns[0]["pending"]["questions"] == [{"question": "Which?", "options": ["A", "B"], "multi": False}]


def test_parse_cuts_configured_run_rules():
    seed = "# nTasker task #7: Do it\n\nplease\n\n## My rules\n\n- finish 7\n\n## Fasttrack\n\n- go"
    turns = parse_transcript([_ev("user", seed)], rules=("## My rules\n\n- finish 7", "## Fasttrack\n\n- go"))
    assert turns[0]["prompt"] == "please"
    # A prompt that is not a queue seed is left alone.
    turns = parse_transcript([_ev("user", "## My rules\n\n- finish 7")], rules=("## My rules\n\n- finish 7",))
    assert turns[0]["prompt"] == "## My rules\n\n- finish 7" and turns[0]["seed"] is False


def _api_error(text, error):
    return _ev("assistant", [{"type": "text", "text": text}], stop_reason="stop_sequence",
               isApiErrorMessage=True, error=error)


@pytest.mark.parametrize(("error", "text", "kind"), [
    ("rate_limit", "You've hit your session limit · resets 3pm (Europe/Vienna)", "limit"),
    ("authentication_failed", "Not logged in · Please run /login", "auth"),
    ("server_error", "API Error: Can't reach the API server", "error"),
    (None, "API Error: something new", "error"),
])
def test_parse_reports_what_blocks_the_agent(error, text, kind):
    """An API error is not an answer -- it is what stops the turn."""
    extra = {"error": error} if error else {}
    lines = [_ev("user", "go"),
             _ev("assistant", [{"type": "text", "text": text}], stop_reason="stop_sequence",
                 isApiErrorMessage=True, **extra)]
    turn = parse_transcript(lines)[-1]
    assert turn["blocker"] == {"kind": kind, "text": text, "at": "2026-09-27T10:00:00Z"}
    assert turn["answer"] == "" and turn["progress"] == [] and not turn["done"]


def test_blocker_clears_once_the_agent_works_again():
    lines = [_ev("user", "go"), _api_error("Login expired · Please run /login", "authentication_failed")]
    # output after the error: the work went on
    resumed = [*lines, _ev("assistant", [{"type": "text", "text": "Done."}], id="m9", stop_reason="end_turn")]
    turn = parse_transcript(resumed)[-1]
    assert turn["blocker"] is None and turn["answer"] == "Done."
    # a new prompt starts clean -- the old turn keeps its blocker for the record
    turns = parse_transcript([*lines, _ev("user", "again")])
    assert turns[0]["blocker"]["kind"] == "auth" and turns[1]["blocker"] is None


def test_conversation_carries_the_current_blocker(tmp_path):
    folder = tmp_path / "projects" / "-some-cwd"
    folder.mkdir(parents=True)
    path = folder / f"{SID}.jsonl"
    path.write_text("\n".join([_ev("user", "go"), _api_error("You've hit your session limit", "rate_limit")]))
    assert conversation_for(tmp_path, SID)["blocker"]["kind"] == "limit"
    path.write_text("\n".join([_ev("user", "go"), _api_error("x", "rate_limit"), _ev("user", "again")]))
    assert conversation_for(tmp_path, SID)["blocker"] is None
    assert conversation_for(tmp_path, None)["blocker"] is None


def test_find_transcript_rejects_path_tricks(tmp_path):
    proj = tmp_path / "projects" / "-some-cwd"
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text("\n".join(EVENTS))
    assert find_transcript(tmp_path, SID) == proj / f"{SID}.jsonl"
    assert find_transcript(tmp_path, "../x") is None
    assert find_transcript(tmp_path, "") is None
    assert conversation_for(tmp_path, "nope")["available"] is False
    conv = conversation_for(tmp_path, SID)
    assert len(conv["turns"]) == 3
    assert conv["usage"]["output"] == 150   # summed over the turns


def test_conversation_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-cwd"
    proj.mkdir(parents=True)
    path = proj / f"{SID}.jsonl"
    path.write_text("\n".join(EVENTS))
    first = conversation_for(tmp_path, SID)
    monkeypatch.setattr("ntasker.transcript.parse_transcript", lambda *a: pytest.fail("parsed again"))
    assert conversation_for(tmp_path, SID) is first
    monkeypatch.undo()
    path.write_text("\n".join(EVENTS[:-1]))
    assert conversation_for(tmp_path, SID)["turns"][-1]["pending"] is None


def test_last_activity_reads_the_file_end(tmp_path):
    proj = tmp_path / "projects" / "-cwd"
    proj.mkdir(parents=True)
    path = proj / f"{SID}.jsonl"
    path.write_text("\n".join(EVENTS))
    assert last_activity(tmp_path, SID) == {"name": "Bash", "kind": "bash", "detail": "rm -rf build", "file": None}
    path.write_text("\n".join(EVENTS[:-1]))
    assert last_activity(tmp_path, SID) == {"text": "Checking."}
    assert last_activity(tmp_path, "nope") is None


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
    assert d["usage"]["input"] > 0
    assert client.get("/api/tasks/9999/conversation").status_code == 404


def test_api_sessions_reports_activity(client, monkeypatch):
    tid = client.post("/api/tasks", json={"title": "t", "agent": "claude"}).json()["id"]
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (SID, tid))
    monkeypatch.setattr("ntasker.app.session_states", lambda: {tid: "running"})
    d = client.get("/api/claude/sessions").json()
    assert d["activity"] == {str(tid): {"name": "Bash", "kind": "bash", "detail": "rm -rf build", "file": None}}
