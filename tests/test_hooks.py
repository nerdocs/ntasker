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
    assert _run(monkeypatch, ["waiting"], {}) == 0
    assert _run(monkeypatch, ["running"], {}) == 0
    assert calls == [
        ("POST", "/api/claude/sessions/7/state", {"waiting": True}),
        ("POST", "/api/claude/sessions/7/state", {"waiting": False}),
    ]


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
        assert {"Stop", "Notification", "UserPromptSubmit", "PostToolUse"} <= set(hooks)
        assert ("PreToolUse" in hooks) is with_locks


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
