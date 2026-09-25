"""The allow rule ntasker writes into the user's Claude Code settings."""

from __future__ import annotations

import json

import pytest

from ntasker import claude_assets
from ntasker.db import init_db, set_db_path
from ntasker.settings import delete_setting, set_setting

RULE = claude_assets.PERMISSION_RULE


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".claude").mkdir()
    return tmp_path / ".claude"


def read(home):
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


def test_install_into_a_fresh_home(home):
    state = claude_assets.set_permission_rule(True, home)
    assert state["installed"] is True
    assert read(home)["permissions"] == {"allow": [RULE]}


def test_install_keeps_foreign_settings_and_rules(home):
    (home / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "permissions": {
                    "allow": ["Bash(pytest *)"],
                    "deny": ["Read(./secrets/**)"],
                    "defaultMode": "auto",
                },
            }
        ),
        encoding="utf-8",
    )
    claude_assets.set_permission_rule(True, home)
    data = read(home)
    assert data["model"] == "opus"
    assert data["permissions"]["allow"] == ["Bash(pytest *)", RULE]
    assert data["permissions"]["deny"] == ["Read(./secrets/**)"]
    assert data["permissions"]["defaultMode"] == "auto"


def test_removal_takes_only_our_entry(home):
    (home / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(pytest *)"], "defaultMode": "auto"}}),
        encoding="utf-8",
    )
    claude_assets.set_permission_rule(True, home)
    claude_assets.set_permission_rule(False, home)
    data = read(home)
    assert data["permissions"] == {"allow": ["Bash(pytest *)"], "defaultMode": "auto"}
    assert claude_assets.permission_rule_state(home)["installed"] is False


def test_removal_drops_the_block_it_created(home):
    claude_assets.set_permission_rule(True, home)
    claude_assets.set_permission_rule(False, home)
    assert "permissions" not in read(home)


def test_install_is_idempotent(home):
    claude_assets.set_permission_rule(True, home)
    claude_assets.set_permission_rule(True, home)
    assert read(home)["permissions"]["allow"] == [RULE]


def test_the_previous_file_is_backed_up(home):
    (home / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    claude_assets.set_permission_rule(True, home)
    backups = list(home.glob("settings.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"model": "opus"}


def test_unparsable_settings_are_never_overwritten(home):
    (home / "settings.json").write_text("{ not json", encoding="utf-8")
    assert claude_assets.permission_rule_state(home) == {
        "installed": False,
        "path": str(home / "settings.json"),
        "readable": False,
    }
    with pytest.raises(ValueError):
        claude_assets.set_permission_rule(True, home)
    assert (home / "settings.json").read_text(encoding="utf-8") == "{ not json"


def test_the_two_switches_do_not_disturb_each_other(home):
    """Both write the same file -- neither may drop what the other put there."""
    claude_assets.set_session_hook(True, home)
    claude_assets.set_permission_rule(True, home)
    data = read(home)
    assert data["permissions"]["allow"] == [RULE]
    assert claude_assets.session_hook_state(home)["installed"] is True

    claude_assets.set_permission_rule(False, home)
    assert claude_assets.session_hook_state(home)["installed"] is True
    assert claude_assets.permission_rule_state(home)["installed"] is False


def test_setting_applies_and_reverts_the_rule(tmp_path, home, monkeypatch):
    """The switch in the UI is the ``claude_permissions`` setting -- writing it
    is what puts the rule in place, and unsetting takes it out again."""
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    monkeypatch.setattr(claude_assets, "_settings_file", lambda h=None: home / "settings.json")
    set_setting("claude_permissions", "on")
    assert claude_assets.permission_rule_state(home)["installed"] is True
    delete_setting("claude_permissions")
    assert claude_assets.permission_rule_state(home)["installed"] is False


def test_a_failing_apply_leaves_the_setting_unwritten(tmp_path, home, monkeypatch):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    (home / "settings.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(claude_assets, "_settings_file", lambda h=None: home / "settings.json")
    with pytest.raises(ValueError):
        set_setting("claude_permissions", "on")
    from ntasker.settings import get_setting

    assert get_setting("claude_permissions") is None


def test_state_endpoint_reports_the_file(tmp_path, home, monkeypatch):
    """The settings page re-reads this after the switch rewrote the file."""
    from fastapi.testclient import TestClient

    from ntasker.app import app

    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    monkeypatch.setattr(claude_assets, "_settings_file", lambda h=None: home / "settings.json")
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.get("/api/claude/permission-rule").json()["installed"] is False
    claude_assets.set_permission_rule(True, home)
    body = client.get("/api/claude/permission-rule").json()
    assert body == {"installed": True, "path": str(home / "settings.json"), "readable": True}
