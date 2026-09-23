"""The session-discovery hook ntasker writes into the user's Claude Code settings."""

from __future__ import annotations

import json

import pytest

from ntasker import claude_assets
from ntasker.db import init_db, set_db_path
from ntasker.settings import delete_setting, set_setting

CMD = claude_assets.SESSION_HOOK_COMMAND


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".claude").mkdir()
    return tmp_path / ".claude"


def read(home):
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


def test_install_into_a_fresh_home(home):
    state = claude_assets.set_session_hook(True, home)
    assert state["installed"] is True
    hooks = read(home)["hooks"]
    for event in claude_assets.SESSION_HOOK_EVENTS:
        assert hooks[event] == [{"hooks": [{"type": "command", "command": CMD}]}]


def test_install_keeps_foreign_settings_and_hooks(home):
    (home / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}],
                    "SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    claude_assets.set_session_hook(True, home)
    data = read(home)
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine"
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert commands == ["theirs", CMD]


def test_removal_takes_only_our_entries(home):
    (home / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}),
        encoding="utf-8",
    )
    claude_assets.set_session_hook(True, home)
    claude_assets.set_session_hook(False, home)
    data = read(home)
    assert data["hooks"]["SessionStart"] == [{"hooks": [{"type": "command", "command": "theirs"}]}]
    assert "UserPromptSubmit" not in data["hooks"]
    assert claude_assets.session_hook_state(home)["installed"] is False


def test_install_is_idempotent(home):
    claude_assets.set_session_hook(True, home)
    claude_assets.set_session_hook(True, home)
    assert len(read(home)["hooks"]["SessionStart"]) == 1


def test_the_previous_file_is_backed_up(home):
    (home / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    claude_assets.set_session_hook(True, home)
    backups = list(home.glob("settings.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"model": "opus"}


def test_unparsable_settings_are_never_overwritten(home):
    (home / "settings.json").write_text("{ not json", encoding="utf-8")
    assert claude_assets.session_hook_state(home) == {
        "installed": False,
        "path": str(home / "settings.json"),
        "readable": False,
    }
    with pytest.raises(ValueError):
        claude_assets.set_session_hook(True, home)
    assert (home / "settings.json").read_text(encoding="utf-8") == "{ not json"


def test_setting_applies_and_reverts_the_hook(tmp_path, home, monkeypatch):
    """The switch in the UI is the ``session_discovery`` setting -- writing it
    is what puts the hook in place, and unsetting takes it out again."""
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    monkeypatch.setattr(claude_assets, "_settings_file", lambda h=None: home / "settings.json")
    set_setting("session_discovery", "on")
    assert claude_assets.session_hook_state(home)["installed"] is True
    delete_setting("session_discovery")
    assert claude_assets.session_hook_state(home)["installed"] is False


def test_a_failing_apply_leaves_the_setting_unwritten(tmp_path, home, monkeypatch):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    (home / "settings.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(claude_assets, "_settings_file", lambda h=None: home / "settings.json")
    with pytest.raises(ValueError):
        set_setting("session_discovery", "on")
    from ntasker.settings import get_setting

    assert get_setting("session_discovery") is None
