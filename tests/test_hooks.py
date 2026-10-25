"""Claude Code hooks: `ntasker hook ...`, the explicit session state, --settings."""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from ntasker import cli, claude_runner
from ntasker.agents import get_spec
from ntasker.claude_assets import hooks_settings_path
from ntasker.db import init_db, set_db_path


@pytest.fixture
def fake_server(monkeypatch):
    """Capture `_api_call` traffic and answer from a canned table."""
    calls: list[tuple[str, str, dict | None]] = []
    answers: dict[str, dict] = {}

    def api_call(base, method, path, body=None, timeout=5.0):
        calls.append((method, path, body))
        key = path.split("?")[0]
        return 200, answers.get(key, {"ok": True})

    monkeypatch.setattr(cli, "_api_call", api_call)
    return calls, answers


def _run(monkeypatch, argv, payload, env=True):
    if env:
        monkeypatch.setenv("NTASKER_TASK_ID", "7")
        monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:8766")
    else:
        monkeypatch.delenv("NTASKER_TASK_ID", raising=False)
        monkeypatch.delenv("NTASKER_URL", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return cli.main(["hook", *argv])


def test_pretooluse_blocks_edit_outside(monkeypatch, capsys, fake_server):
    calls, answers = fake_server
    answers["/api/locks/check"] = {"allowed": False, "project": "other", "holder": 3}
    rc = _run(monkeypatch, ["pretooluse"], {"tool_name": "Edit", "tool_input": {"file_path": "/p/other/a.py"}})
    assert rc == 2
    err = capsys.readouterr().err
    assert "ntasker lock add 7 other" in err and "/p/other/a.py" in err
    assert calls[0][0] == "GET" and "task=7" in calls[0][1] and "path=%2Fp%2Fother%2Fa.py" in calls[0][1]


def test_pretooluse_allows_inside_and_non_project(monkeypatch, fake_server):
    calls, answers = fake_server
    answers["/api/locks/check"] = {"allowed": True, "project": "mine", "holder": None}
    assert _run(monkeypatch, ["pretooluse"], {"tool_name": "Write", "tool_input": {"file_path": "/p/mine/a.py"}}) == 0


def test_pretooluse_bash_cd_and_plain_commands(monkeypatch, fake_server):
    calls, answers = fake_server
    answers["/api/locks/check"] = {"allowed": False, "project": "other", "holder": None}
    rc = _run(monkeypatch, ["pretooluse"], {"tool_name": "Bash", "cwd": "/p/mine", "tool_input": {"command": "cd ../other && ls"}})
    assert rc == 2 and "path=%2Fp%2Fmine%2F..%2Fother" in calls[-1][1]
    n = len(calls)
    assert _run(monkeypatch, ["pretooluse"], {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) == 0
    assert _run(monkeypatch, ["pretooluse"], {"tool_name": "Bash", "tool_input": {"command": "cd -"}}) == 0
    assert len(calls) == n  # nothing to check -> no request


def test_hooks_without_env_are_noops(monkeypatch, fake_server):
    calls, _ = fake_server
    assert _run(monkeypatch, ["pretooluse"], {"tool_name": "Edit", "tool_input": {"file_path": "/x"}}, env=False) == 0
    assert _run(monkeypatch, ["waiting"], {}, env=False) == 0
    assert calls == []


def test_pretooluse_server_down_passes(monkeypatch):
    def boom(*a, **kw):
        raise OSError("down")

    monkeypatch.setattr(cli, "_api_call", boom)
    assert _run(monkeypatch, ["pretooluse"], {"tool_name": "Edit", "tool_input": {"file_path": "/x"}}) == 0


def test_state_hooks_post(monkeypatch, fake_server):
    calls, _ = fake_server
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    assert _run(monkeypatch, ["waiting"], {}) == 0
    assert _run(monkeypatch, ["running"], {}) == 0
    assert _run(monkeypatch, ["waiting"], {"hook_event_name": "Stop"}) == 0
    assert calls == [
        ("POST", "/api/claude/sessions/7/state", {"waiting": True, "turn_over": False, "socket": None}),
        ("POST", "/api/claude/sessions/7/state", {"waiting": False, "turn_over": False, "socket": None}),
        ("POST", "/api/claude/sessions/7/state", {"waiting": True, "turn_over": True, "socket": None}),
    ]


def test_running_hook_ignores_subagents(monkeypatch, fake_server):
    """A (background) subagent's step must not flag the waiting main session running."""
    calls, _ = fake_server
    assert _run(monkeypatch, ["running"], {"agent_id": "a1", "tool_name": "Bash"}) == 0
    assert calls == []


def test_ask_hook_posts_the_questions(monkeypatch, fake_server):
    calls, _ = fake_server
    qs = [{"question": "Which?", "options": [{"label": "A"}], "multiSelect": False}]
    assert _run(monkeypatch, ["ask"], {"tool_name": "AskUserQuestion", "tool_use_id": "q1",
                                       "tool_input": {"questions": qs}}) == 0
    assert calls == [("POST", "/api/claude/sessions/7/question", {"id": "q1", "questions": qs})]
    assert _run(monkeypatch, ["ask"], {"tool_name": "AskUserQuestion", "tool_input": {}}) == 0
    assert len(calls) == 1   # nothing to report


def test_clear_hook_rebinds_the_new_session(monkeypatch, fake_server):
    calls, _ = fake_server
    sid = "0b6c1f7e-5a3d-4c2b-9e8f-1a2b3c4d5e6f"
    assert _run(monkeypatch, ["clear"], {"hook_event_name": "SessionStart", "source": "clear",
                                         "session_id": sid, "cwd": "/p/mine"}) == 0
    assert calls == [("POST", "/api/claude/sessions/7/rebind", {"session_id": sid, "cwd": "/p/mine"})]
    assert _run(monkeypatch, ["clear"], {"source": "clear"}) == 0
    assert len(calls) == 1   # no session id -> nothing to report


class _Proc:
    pid = os.getpid()


def test_session_states_prefers_hook_flag():
    claude_runner.SESSIONS.clear()
    sess = claude_runner.TermSession(task_id=1, proc=_Proc(), master_fd=-1)  # type: ignore[arg-type]
    claude_runner.SESSIONS[1] = sess
    try:
        sess.last_output = time.monotonic()
        assert claude_runner.session_states()[1] == "running"
        assert claude_runner.set_hook_state(1, True) is True
        assert claude_runner.session_states()[1] == "waiting"   # despite fresh output
        claude_runner.set_hook_state(1, False)
        sess.last_output = time.monotonic() - 3600
        assert claude_runner.session_states()[1] == "running"   # despite long silence
        assert claude_runner.set_hook_state(99, True) is False
    finally:
        claude_runner.SESSIONS.clear()


def test_open_question_lives_until_the_session_runs_again():
    claude_runner.SESSIONS.clear()
    sess = claude_runner.TermSession(task_id=1, proc=_Proc(), master_fd=-1)  # type: ignore[arg-type]
    claude_runner.SESSIONS[1] = sess
    try:
        sess.hook_waiting = False
        q = {"id": "q", "questions": [{"question": "Which?", "header": "", "options": [], "multi": False}]}
        assert claude_runner.set_question(1, q) is True
        assert claude_runner.session_states()[1] == "waiting"   # the dialog is up
        assert claude_runner.open_questions() == {1: q}
        claude_runner.set_hook_state(1, True)                    # the permission notification
        assert claude_runner.open_questions() == {1: q}
        claude_runner.set_hook_state(1, False)                   # answered: PostToolUse
        assert claude_runner.open_questions() == {}
        assert claude_runner.set_question(99, q) is False
    finally:
        claude_runner.SESSIONS.clear()


def test_idle_only_once_the_turn_is_over():
    """A permission dialog or question waits too, but only a ``Stop`` (or an Esc) is idle."""
    claude_runner.SESSIONS.clear()
    sess = claude_runner.TermSession(task_id=1, proc=_Proc(), master_fd=-1)  # type: ignore[arg-type]
    claude_runner.SESSIONS[1] = sess

    def idle():
        return claude_runner.idle_session_ids(claude_runner.session_states())

    try:
        sess.last_output = time.monotonic() - 3600
        assert idle() == [1]                                     # no hooks: silence decides
        claude_runner.set_hook_state(1, True)                    # permission notification
        assert idle() == []
        claude_runner.set_hook_state(1, True, turn_over=True)    # Stop
        assert idle() == [1]
        claude_runner.set_question(1, {"id": "q", "questions": []})
        assert idle() == []
        claude_runner.set_hook_state(1, False)                   # working again
        claude_runner._note_interrupt(sess, "\x1b[A")            # an arrow key
        assert idle() == []
        claude_runner._note_interrupt(sess, "\x1b")              # Esc interrupts the turn
        assert idle() == [1]
        claude_runner.set_hook_state(1, True)
        claude_runner._note_interrupt(sess, "\x1b")              # Esc into a dialog: no turn end
        assert idle() == []
    finally:
        claude_runner.SESSIONS.clear()


def test_build_spawn_settings_flag(tmp_path):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    claude = get_spec("claude")
    args = claude.build_spawn("seed", settings_path="/h/locks.json")
    assert args[args.index("--settings") + 1] == "/h/locks.json"
    pi = get_spec("pi")
    assert "--settings" not in pi.build_spawn("seed", settings_path="/h/locks.json")


def test_hooks_files_exist():
    for with_locks in (True, False):
        path = hooks_settings_path(with_locks)
        assert os.path.isfile(path)
        hooks = json.load(open(path))["hooks"]
        assert {"Stop", "Notification", "UserPromptSubmit", "PostToolUse", "PreToolUse"} <= set(hooks)
        matchers = [h.get("matcher") for h in hooks["PreToolUse"]]
        assert "AskUserQuestion" in matchers
        assert [h["matcher"] for h in hooks["SessionStart"]] == ["clear"]
        assert any(m and "Edit" in m for m in matchers) is with_locks


def test_clean_env_carries_task_markers(monkeypatch):
    monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:9999")
    env = claude_runner._clean_env(get_spec("claude"), 42)
    assert env["NTASKER_TASK_ID"] == "42" and env["NTASKER_URL"] == "http://127.0.0.1:9999"
    monkeypatch.delenv("NTASKER_URL")
    assert claude_runner._clean_env(get_spec("claude"), 1)["NTASKER_URL"] == "http://127.0.0.1:8766"


def test_clean_env_prefers_own_ntasker(monkeypatch, tmp_path):
    (tmp_path / "ntasker").write_text("")
    monkeypatch.setattr(claude_runner.sys, "executable", str(tmp_path / "python"))
    env = claude_runner._clean_env(get_spec("claude"), 1)
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path)


def test_statusline_is_wired():
    for with_locks in (False, True):
        with open(hooks_settings_path(with_locks)) as f:
            assert json.load(f)["statusLine"]["command"] == "ntasker hook statusline"


def test_hook_statusline_reports_window_and_runs_the_users(fake_server, monkeypatch, tmp_path, capsys):
    calls, _answers = fake_server
    home, project = tmp_path / "claude", tmp_path / "proj"
    home.mkdir()
    (project / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("NTASKER_TASK_ID", "7")
    monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:8766")
    (home / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo user"}}))
    payload = {"cwd": str(project), "workspace": {"project_dir": str(project)},
               "context_window": {"context_window_size": 1_000_000}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "statusline"]) == 0
    assert calls == [("POST", "/api/claude/sessions/7/context-window", {"size": 1_000_000})]
    assert capsys.readouterr().out == "user\n"
    # Project settings outrank the user's; the user's statusline sees the same input.
    (project / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"command": "cat"}}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "statusline"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_hook_statusline_without_window_or_own_statusline(fake_server, monkeypatch, tmp_path, capsys):
    calls, _answers = fake_server
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NTASKER_TASK_ID", "7")
    monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:8766")
    # ntasker's own statusline in the user's settings must not run itself again.
    (tmp_path / "settings.json").write_text(json.dumps({"statusLine": {"command": "ntasker hook statusline"}}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    assert cli.main(["hook", "statusline"]) == 0
    assert calls == [] and capsys.readouterr().out == ""
