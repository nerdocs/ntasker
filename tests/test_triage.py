"""Inbox triage: prefix parsing, result validation, the tick, the claude -p call shape."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ntasker import locks, triage
from ntasker.db import get_conn, init_db, load_tags_for, set_db_path
from ntasker.settings import get_triage_enabled, set_setting

NAMES = ["ntasker", "medux/online", "Thrito"]

GOOD = {
    "title": "Add a --json flag to ntasker in",
    "prompt": "Add `--json` to `ntasker in`.\n\nPrint the stored row as JSON.",
    "project": "ntasker",
    "candidates": [{"project": "ntasker", "reason": "the CLI lives there"}],
    "priority": "normal",
    "tags": ["Feature", "cli"],
    "confidence": 0.9,
    "question": None,
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.delenv("NTASKER_TRIAGE_ENABLED", raising=False)
    monkeypatch.delenv("NTASKER_TRIAGE_MODEL", raising=False)
    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(triage, "catalog", lambda: [(n, f"{n} summary") for n in NAMES])
    triage._summary_failed.clear()
    return path


def _inbox(text, source="cli"):
    with get_conn() as conn:
        return int(conn.execute(
            "INSERT INTO inbox (text, source) VALUES (?, ?)", (text, source)
        ).lastrowid)


def _row(table, rid):
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()


# --- split_prefix -----------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ntasker: fix the thing", ("ntasker", "fix the thing")),
        ("#ntasker fix the thing", ("ntasker", "fix the thing")),
        ("NTASKER: case does not matter", ("ntasker", "case does not matter")),
        ("medux/online: slash names work", ("medux/online", "slash names work")),
        ("thrito: folded", ("Thrito", "folded")),
        ("nothing: here", (None, "nothing: here")),
        ("#42 is not a project", (None, "#42 is not a project")),
        ("plain note", (None, "plain note")),
    ],
)
def test_split_prefix(text, expected):
    assert triage.split_prefix(text, NAMES) == expected


# --- parse_result -----------------------------------------------------------

def test_parse_result_good():
    out = triage.parse_result(GOOD, NAMES)
    assert out["project"] == "ntasker" and out["tags"] == ["feature", "cli"]
    assert out["confidence"] == 0.9 and out["question"] is None and out["prefix"] is False


def test_parse_result_missing_prompt():
    with pytest.raises(triage.TriageError, match="prompt"):
        triage.parse_result({**GOOD, "prompt": None}, NAMES)


def test_parse_result_not_a_dict():
    with pytest.raises(triage.TriageError):
        triage.parse_result(["nope"], NAMES)


def test_parse_result_unknown_project_becomes_null_and_candidates_filtered():
    out = triage.parse_result(
        {**GOOD, "project": "ghost", "candidates": [{"project": "ghost", "reason": "x"}]}, NAMES
    )
    assert out["project"] is None and out["candidates"] == []


def test_parse_result_bad_priority_defaults_and_confidence_clamps():
    out = triage.parse_result({**GOOD, "priority": "urgent", "confidence": 7}, NAMES)
    assert out["priority"] == "normal" and out["confidence"] == 1.0


def test_parse_result_cuts_candidates_to_three():
    cands = [{"project": n, "reason": ""} for n in NAMES] + [{"project": "ntasker", "reason": ""}]
    assert len(triage.parse_result({**GOOD, "candidates": cands}, NAMES)["candidates"]) == 3


def test_parse_result_empty_title_from_prompt():
    out = triage.parse_result({**GOOD, "title": " "}, NAMES)
    assert out["title"] == "Add `--json` to `ntasker in`."


# --- tick -------------------------------------------------------------------

def test_tick_creates_proposed_task(db, monkeypatch):
    calls: list[tuple[list[str], str]] = []

    def fake(argv, stdin):
        calls.append((argv, stdin))
        return dict(GOOD)

    monkeypatch.setattr(triage, "run_claude", fake)
    monkeypatch.setattr(triage, "_argv", lambda system, schema: ["claude", system])
    iid = _inbox("ntasker: add a --json flag")
    triage.tick()
    inbox = _row("inbox", iid)
    assert inbox["status"] == "triaged" and inbox["task_id"] and inbox["error"] is None
    task = _row("tasks", inbox["task_id"])
    assert task["proposed"] == 1 and task["project"] == "ntasker"
    assert task["description"].endswith("## Original\n\nntasker: add a --json flag")
    with get_conn() as conn:
        assert load_tags_for(conn, task["id"]) == ["cli", "feature"]
    stored = json.loads(task["triage"])
    assert stored["raw"] == "ntasker: add a --json flag" and stored["prefix"] is True
    assert stored["candidates"][0] == {"project": "ntasker", "reason": "prefix"}
    # the prefix is stripped before the call, the catalog is in the system prompt
    assert calls[0][1] == "add a --json flag" and "medux/online summary" in calls[0][0][1]


def test_tick_marks_failure_and_keeps_task_count(db, monkeypatch):
    def boom(argv, stdin):
        raise triage.TriageError("timeout after 120s")

    monkeypatch.setattr(triage, "run_claude", boom)
    monkeypatch.setattr(triage, "_argv", lambda system, schema: ["claude"])
    iid = _inbox("something")
    triage.tick()
    row = _row("inbox", iid)
    assert row["status"] == "failed" and row["error"] == "timeout after 120s"
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_tick_without_pending_rows_is_a_noop(db, monkeypatch):
    monkeypatch.setattr(triage, "run_claude", lambda *a: pytest.fail("must not be called"))
    triage.tick()


def test_missing_summary_does_not_block_the_tick(db, monkeypatch, tmp_path):
    """A project without a summary goes into the prompt by name -- no extra call."""
    (tmp_path / "ntasker").mkdir()
    monkeypatch.setattr(triage, "catalog", lambda: [("ntasker", None), ("Thrito", "t summary")])
    monkeypatch.setattr(triage, "_argv", lambda system, schema: ["claude", system])
    calls: list[str] = []

    def fake(argv, stdin):
        calls.append(argv[1])
        return dict(GOOD)

    monkeypatch.setattr(triage, "run_claude", fake)
    _inbox("note")
    triage.tick()
    assert len(calls) == 1
    assert "- ntasker: (no summary)" in calls[0] and "- Thrito: t summary" in calls[0]


def _stored_catalog() -> list[tuple[str, str | None]]:
    """A two-project catalog that reflects what is stored -- like the real one."""
    with get_conn() as conn:
        stored = {
            r["project"]: r["summary"]
            for r in conn.execute("SELECT project, summary FROM project_summaries")
        }
    return [(n, stored.get(n)) for n in ("ntasker", "Thrito")]


def test_summary_tick_fills_one_missing_summary(db, monkeypatch, tmp_path):
    (tmp_path / "ntasker").mkdir()
    (tmp_path / "Thrito").mkdir()
    monkeypatch.setattr(triage, "catalog", lambda: [("ntasker", None), ("Thrito", None)])
    monkeypatch.setattr(triage, "_argv", lambda system, schema: ["claude", system])
    calls: list[str] = []

    def fake(argv, stdin):
        calls.append(stdin)
        return {"summary": "  ntasker is a tracker.  "}

    monkeypatch.setattr(triage, "run_claude", fake)
    assert triage.summary_tick() is True
    assert len(calls) == 1 and calls[0].startswith("Project name: ntasker")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT summary FROM project_summaries WHERE project = 'ntasker'"
        ).fetchone()
    assert row["summary"] == "ntasker is a tracker."


def test_summary_tick_without_gaps_is_a_noop(db, monkeypatch):
    monkeypatch.setattr(triage, "run_claude", lambda *a: pytest.fail("must not be called"))
    assert triage.summary_tick() is False


def test_summary_tick_skips_a_failing_project(db, monkeypatch, tmp_path):
    """A project whose call fails is not retried in this process -- no hot loop."""
    (tmp_path / "ntasker").mkdir()
    (tmp_path / "Thrito").mkdir()
    monkeypatch.setattr(triage, "catalog", _stored_catalog)
    monkeypatch.setattr(triage, "_argv", lambda system, schema: ["claude", system])
    calls: list[str] = []

    def fake(argv, stdin):
        calls.append(stdin)
        if stdin.startswith("Project name: ntasker"):
            raise triage.TriageError("nope")
        return {"summary": "Thrito is a thing."}

    monkeypatch.setattr(triage, "run_claude", fake)
    assert triage.summary_tick() is True
    assert triage.summary_tick() is True
    assert [c.splitlines()[0] for c in calls] == ["Project name: ntasker", "Project name: Thrito"]
    assert triage.summary_tick() is False


def test_summary_progress_counts_the_catalog(db, monkeypatch):
    monkeypatch.setattr(triage, "catalog", lambda: [("a", "s"), ("b", None), ("c", None)])
    assert triage.summary_progress() == {"done": 1, "total": 3}


def test_examples_land_in_prompt(db, monkeypatch):
    with get_conn() as conn:
        conn.execute("INSERT INTO triage_examples (text, project) VALUES ('old one', 'Thrito')")
        conn.execute("INSERT INTO triage_examples (text, project) VALUES ('new one', NULL)")
    seen: list[str] = []
    monkeypatch.setattr(triage, "_argv", lambda system, schema: seen.append(system) or ["c"])
    monkeypatch.setattr(triage, "run_claude", lambda argv, stdin: dict(GOOD))
    _inbox("note")
    triage.tick()
    assert seen[0].index('"old one" -> Thrito') < seen[0].index('"new one" -> cross-project')


def test_triage_enabled_setting(db):
    assert get_triage_enabled() is True
    set_setting("triage_enabled", "off")
    assert get_triage_enabled() is False


# --- end to end with a fake binary -------------------------------------------

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_OUT/argv"
env > "$FAKE_OUT/env"
cat > "$FAKE_OUT/stdin"
cat "$FAKE_OUT/reply"
"""


def test_end_to_end_fake_binary(db, monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    (out / "reply").write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ignored", "structured_output": GOOD,
    }))
    monkeypatch.setenv("NTASKER_CLAUDE_BIN", str(exe))
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("CLAUDECODE", "1")
    iid = _inbox("ntasker: add a --json flag")
    triage.tick()
    assert _row("inbox", iid)["status"] == "triaged"
    argv = (out / "argv").read_text().split("\n")
    assert "-p" in argv and "--json-schema" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--model") + 1] == "haiku"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["project"]["enum"] == [*NAMES, None]
    assert (out / "stdin").read_text() == "add a --json flag"
    env = dict(line.split("=", 1) for line in (out / "env").read_text().splitlines() if "=" in line)
    assert "CLAUDECODE" not in env and env["NTASKER_TASK_ID"] == "inbox"
    assert Path(env["PWD"]).resolve() == Path(os.path.realpath(triage.tempfile.gettempdir()))


def test_schema_with_empty_catalog_is_valid():
    schema = triage._schema([])
    assert schema["properties"]["project"]["enum"] == [None]
    assert schema["properties"]["candidates"]["maxItems"] == 0
    assert "enum" not in schema["properties"]["candidates"]["items"]["properties"]["project"]
