"""`ntasker run` -- the board's run button, typed in a terminal."""

from __future__ import annotations

import pytest

from ntasker import cli
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.delenv("NTASKER_URL", raising=False)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.setenv(var, "C")
    return str(path)


def _add(db: str, title: str, **flags: bool) -> int:
    from ntasker.db import get_conn

    args = ["--db", db, "add", "--title", title, "--project", "p"]
    args += [f"--{name}" for name, on in flags.items() if on]
    assert cli.main(args) == 0
    with get_conn() as conn:
        return int(conn.execute("SELECT MAX(id) AS id FROM tasks").fetchone()["id"])


@pytest.fixture
def fake_server(monkeypatch):
    """Answer /api/queue/run with a one-entry queue, recording the calls."""
    calls: list[tuple[str, str, dict | None]] = []

    def api_call(base, method, path, body=None, timeout=5.0):
        calls.append((method, path, body))
        return 200, {
            "enabled": True,
            "items": [{"id": body["id"], "project": "p"}],
            "skipped": {},
        }

    monkeypatch.setattr(cli, "_api_call", api_call)
    return calls


def test_run_posts_the_run_button_and_prints_the_url(env, fake_server, capsys):
    tid = _add(env, "t")
    assert cli.main(["--db", env, "run", str(tid)]) == 0
    assert fake_server == [("POST", "/api/queue/run", {"id": tid})]
    assert f"{BASE}/#/run/{tid}" in capsys.readouterr().out


def test_run_takes_several_ids(env, fake_server):
    a, b = _add(env, "a"), _add(env, "b")
    assert cli.main(["--db", env, "run", str(a), f"#{b}"]) == 0
    assert [c[2]["id"] for c in fake_server] == [a, b]


def test_run_refuses_a_draft_before_touching_the_server(env, fake_server, capsys):
    tid = _add(env, "d", draft=True)
    assert cli.main(["--db", env, "run", str(tid)]) == 2
    assert fake_server == []
    assert "draft" in capsys.readouterr().err


def test_run_reports_the_lane_position(env, monkeypatch, capsys):
    tid = _add(env, "t")
    monkeypatch.setattr(
        cli,
        "_api_call",
        lambda *a, **k: (
            200,
            {
                "enabled": False,
                "items": [
                    {"id": 90, "project": "other"},
                    {"id": 91, "project": "p"},
                    {"id": tid, "project": "p"},
                ],
            },
        ),
    )
    assert cli.main(["--db", env, "run", str(tid)]) == 0
    out = capsys.readouterr()
    assert "2." in out.out          # second in its own lane, not third overall
    assert "paused" in out.err      # a paused queue starts nothing


def test_run_without_a_server_exits_1(env, monkeypatch, capsys):
    tid = _add(env, "t")

    def boom(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(cli, "_api_call", boom)
    assert cli.main(["--db", env, "run", str(tid)]) == 1
    assert "not reachable" in capsys.readouterr().err
