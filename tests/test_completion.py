"""Shell completion: generated scripts, install/uninstall, API."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from ntasker import cli, completion
from ntasker.app import app
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME + user-data dir so nothing touches the real rc files."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setattr(completion.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        completion.platformdirs, "user_data_dir", lambda name: str(tmp_path / "data")
    )
    return tmp_path


def _bash_complete(script: str, *words: str) -> list[str]:
    """Run the generated bash function on ``words`` (last one is the word being completed)."""
    quoted = " ".join(f"'{w}'" for w in words)
    code = (
        f"source /dev/stdin <<'NT'\n{script}\nNT\n"
        f"COMP_WORDS=({quoted}); COMP_CWORD=$(( ${{#COMP_WORDS[@]}} - 1 )); "
        'COMPREPLY=(); _ntasker; printf "%s\\n" "${COMPREPLY[@]}"'
    )
    out = subprocess.run(["bash", "-c", code], capture_output=True, text=True, check=True)
    return out.stdout.split()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_bash_script_completes_subcommands_options_and_choices():
    script = completion.render("bash")
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert _bash_complete(script, "ntasker", "sel") == ["self-update"]
    assert _bash_complete(script, "ntasker", "config", "s") == ["set"]
    assert "--port" in _bash_complete(script, "ntasker", "start", "--")  # alias of serve
    assert _bash_complete(script, "ntasker", "list", "--status", "") == ["open", "done"]
    assert _bash_complete(script, "ntasker", "agent", "install", "") == ["claude", "opencode", "pi"]
    assert _bash_complete(script, "ntasker", "--db", "x.db", "que") == ["queue"]


def test_zsh_script_has_one_function_per_command():
    script = completion.render("zsh")
    assert script.startswith("#compdef ntasker")
    assert "_ntasker_cmd_ntasker_config_set() {" in script
    assert "serve|start) _ntasker_cmd_ntasker_serve ;;" in script
    assert "'1:shell:(bash zsh)'" in script
    if shutil.which("zsh"):
        subprocess.run(["zsh", "-n"], input=script, text=True, check=True)


def test_install_and_uninstall_round_trip(home):
    rc = home / ".bashrc"
    rc.write_text("export FOO=1")  # no trailing newline on purpose
    assert not completion.is_installed("bash")

    assert completion.install("bash") == rc
    assert completion.is_installed("bash")
    assert completion.script_path("bash").read_text() == completion.render("bash")
    text = rc.read_text()
    assert text.startswith("export FOO=1\n")
    assert text.count("# >>> ntasker completion >>>") == 1
    completion.install("bash")  # idempotent
    assert rc.read_text() == text

    assert completion.uninstall("bash")
    assert rc.read_text() == "export FOO=1\n"
    assert not completion.script_path("bash").exists()
    assert not completion.uninstall("bash")


def test_zsh_rc_honours_zdotdir(home, monkeypatch):
    monkeypatch.setenv("ZDOTDIR", str(home / "zdot"))
    assert completion.rc_path("zsh") == home / "zdot" / ".zshrc"


def test_cli_prints_script(home, capsys):
    assert cli.main(["completion", "zsh"]) == 0
    assert capsys.readouterr().out.startswith("#compdef ntasker")
    assert cli.main(["completion", "bash", "--install"]) == 0
    assert completion.is_installed("bash")


def test_api_status_install_remove(home, tmp_path):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    client = TestClient(app, base_url=BASE)
    assert client.get("/api/completion").json()["zsh"] == {
        "installed": False,
        "rc": "~/.zshrc",
    }
    assert client.post("/api/completion/zsh").status_code == 200
    assert client.get("/api/completion").json()["zsh"]["installed"] is True
    assert client.delete("/api/completion/zsh").json()["removed"] is True
    assert client.post("/api/completion/fish").status_code == 404
