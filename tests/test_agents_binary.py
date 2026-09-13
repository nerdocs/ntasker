"""Tests for the agent binary fallback over well-known install dirs."""

from pathlib import Path

from ntasker import agents


def _touch_exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def test_well_known_dirs_prefer_nvm_default_then_newest(tmp_path):
    versions = tmp_path / ".nvm" / "versions" / "node"
    for v in ("v22.23.1", "v24.14.1", "v25.0.0"):
        (versions / v / "bin").mkdir(parents=True)
    (tmp_path / ".nvm" / "alias").mkdir()
    (tmp_path / ".nvm" / "alias" / "default").write_text("24\n")

    dirs = agents.well_known_bin_dirs(tmp_path)
    nvm_dirs = [d for d in dirs if "/.nvm/" in d]
    assert [Path(d).parent.name for d in nvm_dirs] == ["v24.14.1", "v25.0.0", "v22.23.1"]
    assert dirs[0] == str(tmp_path / ".local" / "bin")


def test_resolve_binary_falls_back_to_well_known_dir(tmp_path, monkeypatch):
    exe = tmp_path / ".nvm" / "versions" / "node" / "v24.14.1" / "bin" / "claude"
    _touch_exe(exe)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("NTASKER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr("ntasker.settings.get_setting", lambda *a, **k: None)

    assert agents.resolve_binary(agents.AGENTS["claude"]) == str(exe)
