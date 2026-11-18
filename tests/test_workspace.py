"""Tests for the workspace plugin: scanners, root confinement, API, enablement."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ntasker import plugins
from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.plugins.workspace import scan

BASE = "http://127.0.0.1:8766"

AGENT_MD = '''---
name: "reviewer"
description: "Reviews diffs for correctness.\\n\\n<example>long tail</example>"
tools: Read, Grep
model: sonnet
---
# Reviewer
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return TestClient(app, base_url=BASE)


def test_scan_team_reads_claude_agent_frontmatter(tmp_path):
    (tmp_path / "reviewer.md").write_text(AGENT_MD)
    (tmp_path / "plain.md").write_text("# Just a heading\n")
    out = scan.scan_team(str(tmp_path))
    by = {m["name"]: m for m in out["members"]}
    assert out["exists"] and out["total"] == 2
    assert by["reviewer"]["role"] == "Reviews diffs for correctness."
    assert by["reviewer"]["model"] == "sonnet" and by["reviewer"]["tools"] == "Read, Grep"
    assert by["plain"]["role"] == "" and by["plain"]["title"] == "Just a heading"
    # unset -> Claude's default agents dir
    assert scan.DEFAULT_TEAM_DIR == "~/.claude/agents" and scan.scan_team(None)["configured"]


def test_scan_skills_flags_broken(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "SKILL.md").write_text("---\nname: good\ndescription: ok\n---\n")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "README.md").write_text("no skill here\n")
    out = scan.scan_skills(str(tmp_path))
    assert out["loading"] == 1 and out["broken"] == 1
    assert [s["name"] for s in out["skills"]] == ["good", "bad"]


def test_roots_and_trash(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "n.md"
    note.write_text("x")
    assert scan.within_roots(note.resolve(), [root.resolve()])
    assert not scan.within_roots((tmp_path / "other.md").resolve(), [root.resolve()])
    with pytest.raises(scan.WriteError):
        scan.delete_entry(str(root / ".." / "other.md"), [root.resolve()])
    result = scan.delete_entry(str(note), [root.resolve()])
    assert result["method"] in ("folder", "os")
    if result["method"] == "folder":
        assert Path(result["trashed_to"]).is_file()
        assert scan.TRASH_DIRNAME in result["trashed_to"]
    assert not note.exists()


def test_api_confined_to_roots(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    assert client.put("/api/settings/workspace_wiki_dir", json={"value": str(vault)}).status_code == 200
    assert client.put("/api/settings/workspace_docs_dir", json={"value": "relative"}).status_code == 400
    inv = client.get("/api/workspace").json()
    assert inv["wiki"]["exists"] and inv["wiki"]["vault"] == "vault"
    assert client.get("/api/workspace/file", params={"path": str(vault / "a.md")}).json()["text"] == "# A\n"
    assert client.get("/api/workspace/file", params={"path": str(outside)}).status_code == 403
    assert client.put("/api/workspace/file", json={"path": str(outside), "text": "x"}).status_code == 403
    r = client.post("/api/workspace/entry", json={"parent": str(vault), "name": "new"})
    assert r.status_code == 201 and r.json()["name"] == "new.md"
    assert client.post("/api/workspace/rename", json={"path": str(vault / "new.md"), "name": "b.md"}).status_code == 200
    r = client.post("/api/workspace/delete", json={"path": str(vault / "b.md")})
    assert r.status_code == 200 and not (vault / "b.md").exists()
    assert client.get("/api/workspace/browse", params={"path": str(vault)}).json()["is_root"] is True


def test_docx_export(client, monkeypatch):
    r = client.post("/api/workspace/docx", json={"text": "# Title\n\nBody\n"})
    if r.status_code == 501:
        pytest.skip("pandoc not installed")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"  # a .docx is a zip container
    monkeypatch.setattr("ntasker.plugins.workspace.routes.find_pandoc", lambda: None)
    assert client.post("/api/workspace/docx", json={"text": "x"}).status_code == 501


def test_find_pandoc_outside_path(monkeypatch, tmp_path):
    """launchd's PATH lacks /opt/homebrew/bin -- pandoc must still be found."""
    from ntasker import binpath
    from ntasker.plugins.workspace import routes

    pandoc = tmp_path / "bin" / "pandoc"
    pandoc.parent.mkdir()
    pandoc.write_text("#!/bin/sh\n", encoding="utf-8")
    pandoc.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(binpath, "TOOL_DIRS", (str(tmp_path / "nope"), str(pandoc.parent)))
    assert routes.find_pandoc() == str(pandoc)
    monkeypatch.setattr(binpath, "TOOL_DIRS", (str(tmp_path / "nope"),))
    assert routes.find_pandoc() is None


def test_scan_skills_symlinked_skill_is_read_only(tmp_path):
    root = tmp_path / "skills"
    (root / "local").mkdir(parents=True)
    (root / "local" / "SKILL.md").write_text("---\nname: local\ndescription: d\n---\n")
    outside = tmp_path / "elsewhere" / "linked"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: linked\ndescription: d\n---\n")
    (root / "linked").symlink_to(outside)
    by = {s["name"]: s for s in scan.scan_skills(str(root))["skills"]}
    assert by["local"]["loads"] and by["local"]["file"].endswith("local/SKILL.md")
    assert by["linked"]["loads"] and by["linked"]["file"] == ""


def test_page_and_enablement(client, monkeypatch):
    assert client.get("/workspace", follow_redirects=False).headers["location"] == "/workspace/skills"
    assert client.get("/workspace/skills").status_code == 200
    assert client.get("/workspace/team").status_code == 200
    assert client.get("/workspace/nope").status_code == 404
    assert 'href="/workspace"' in client.get("/").text
    monkeypatch.setenv(plugins.ENV_DISABLED, "workspace")
    assert client.get("/workspace").status_code == 404
    assert client.get("/api/workspace").status_code == 404
    assert 'href="/workspace"' not in client.get("/").text
    # task_context keeps working without the workspace plugin
    assert client.get("/api/fs/pick").status_code == 200


def test_scan_docs_reads_project_from_filename(tmp_path):
    for name in ("ntasker_Report_2026-10-11.md", "2026-06-08_notes.md", "loose.md"):
        (tmp_path / name).write_text("x")
    docs = {d["name"]: d["project"] for d in scan.scan_docs(str(tmp_path))["docs"]}
    assert docs == {
        "ntasker_Report_2026-10-11.md": "ntasker",
        "2026-06-08_notes.md": "",
        "loose.md": "",
    }


def test_run_file_bound_to_transcript(client, tmp_path, monkeypatch):
    import json

    from ntasker.db import get_conn

    sid = "11111111-2222-3333-4444-555555555555"
    report = tmp_path / "docs" / "report.md"
    report.parent.mkdir()
    report.write_text("# Report")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    home = tmp_path / "claude"
    (home / "projects" / "-cwd").mkdir(parents=True)
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": f"Wrote [it]({report})"}]}}
    (home / "projects" / "-cwd" / f"{sid}.jsonl").write_text(json.dumps(event))
    monkeypatch.setenv("NTASKER_CLAUDE_HOME", str(home))
    tid = client.post("/api/tasks", json={"title": "t", "agent": "claude"}).json()["id"]
    url = f"/api/tasks/{tid}/run-file"
    assert client.get(url, params={"path": str(report)}).status_code == 403   # no session yet
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (sid, tid))
    r = client.get(url, params={"path": str(report)})
    assert r.status_code == 200 and "# Report" in r.text
    assert client.get(url, params={"path": str(secret)}).status_code == 403
    assert client.get(url, params={"path": ""}).status_code == 403
    assert client.get("/api/tasks/999999/run-file", params={"path": str(report)}).status_code == 404
    opened = []
    monkeypatch.setattr(scan, "open_with_desktop", lambda p: opened.append(p) or {"opened": True})
    assert client.post(f"{url}/reveal", params={"path": str(report)}).status_code == 200 and opened == [report]
    assert client.post(f"{url}/reveal", params={"path": str(secret)}).status_code == 403
