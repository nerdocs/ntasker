"""Tool lookup under a service manager's bare PATH (launchd, systemd)."""

from __future__ import annotations

import os

from ntasker import binpath


def _tool(d, name="tool"):
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("#!/bin/sh\n", encoding="utf-8")
    f.chmod(0o755)
    return f


def test_extend_path_appends_missing_dirs(monkeypatch, tmp_path):
    brew = tmp_path / "brew"
    brew.mkdir()
    monkeypatch.setattr(binpath, "TOOL_DIRS", ("/usr/bin", str(brew), str(tmp_path / "missing")))
    env = {"PATH": "/usr/bin:/bin"}
    assert binpath.extend_path(env) == [str(brew)]
    # Appended, so the user's own order keeps precedence; absent dirs skipped.
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin", str(brew)])
    assert binpath.extend_path(env) == []  # idempotent


def test_extend_path_without_path(monkeypatch, tmp_path):
    monkeypatch.setattr(binpath, "TOOL_DIRS", (str(tmp_path),))
    env: dict[str, str] = {}
    binpath.extend_path(env)
    assert env["PATH"] == str(tmp_path)


def test_extend_path_makes_which_find_tool(monkeypatch, tmp_path):
    """The launchd case: the tool sits in Homebrew's dir, PATH is bare."""
    import shutil

    tool = _tool(tmp_path / "homebrew" / "bin", "pandoc")
    monkeypatch.setattr(binpath, "TOOL_DIRS", (str(tool.parent),))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert shutil.which("pandoc") is None
    binpath.extend_path()
    assert shutil.which("pandoc") == str(tool)


def test_find_tool_prefers_path(monkeypatch, tmp_path):
    on_path = _tool(tmp_path / "a")
    fallback = _tool(tmp_path / "b")
    monkeypatch.setattr(binpath, "TOOL_DIRS", (str(fallback.parent),))
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert binpath.find_tool("tool") == str(on_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert binpath.find_tool("tool") == str(fallback)
    assert binpath.find_tool("nope") is None


def test_find_tool_skips_non_executable(monkeypatch, tmp_path):
    f = tmp_path / "tool"
    f.write_text("x", encoding="utf-8")
    f.chmod(0o644)
    monkeypatch.setattr(binpath, "TOOL_DIRS", (str(tmp_path),))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert binpath.find_tool("tool") is None


def test_tool_dirs_expand_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(binpath, "TOOL_DIRS", ("~/.local/bin",))
    assert binpath.tool_dirs() == [str(tmp_path / ".local" / "bin")]
