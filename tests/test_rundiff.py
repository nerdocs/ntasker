"""Run diff: the session's working tree vs. its start commit, via the API."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from ntasker import rundiff
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path

BASE = "http://127.0.0.1:8766"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return TestClient(app, base_url=BASE)


def test_changed_files_since_base(repo):
    base = rundiff.git_head(str(repo))
    (repo / "a.txt").write_text("a\nb\n")
    (repo / "new.py").write_text("x\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "agent commit")   # committed by the agent -> still in the diff
    (repo / "a.txt").unlink()
    result = rundiff.changed_files(str(repo), base)
    assert result["git"] is True
    by_path = {f["path"]: f for f in result["files"]}
    assert by_path["a.txt"]["status"] == "deleted" and by_path["a.txt"]["deletions"] == 1
    assert by_path["new.py"]["status"] == "untracked" and by_path["new.py"]["additions"] == 1
    assert "+x" in by_path["new.py"]["diff"]


def test_not_a_repo(tmp_path):
    assert rundiff.git_head(str(tmp_path)) is None
    assert rundiff.changed_files(str(tmp_path), None) == {"git": False, "files": []}


def test_run_diff_over_directories(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "o.txt").write_text("o\n")
    (repo / "a.txt").write_text("a\nb\n")
    result = rundiff.run_diff({str(repo): rundiff.git_head(str(repo)), str(other): None})
    assert result["git"] is True
    assert [(f["dir"], f["path"], f["status"]) for f in result["files"]] == [
        ("repo", "a.txt", "modified"),
        ("other", "o.txt", "untracked"),
    ]


def test_baselines_roundtrip(repo, tmp_path):
    raw = rundiff.baselines_for([str(repo), str(tmp_path / "plain")])
    assert rundiff.parse_baselines(raw) == {str(repo): rundiff.git_head(str(repo)), str(tmp_path / "plain"): None}
    assert rundiff.parse_baselines(None) == {} and rundiff.parse_baselines("[1]") == {}


def test_diff_endpoint(client, repo):
    tid = client.post("/api/tasks", json={"title": "t"}).json()["id"]
    assert client.get(f"/api/tasks/{tid}/diff").status_code == 404   # never ran
    assert client.get(f"/api/tasks/{tid}").json()["has_diff"] is False
    with get_conn() as conn:   # what _store_run_baselines writes at spawn
        conn.execute(
            "UPDATE tasks SET run_baselines = ? WHERE id = ?",
            (rundiff.baselines_for([str(repo)]), tid),
        )
    assert client.get(f"/api/tasks/{tid}").json()["has_diff"] is True
    assert client.get(f"/api/tasks/{tid}/diff").json() == {"git": True, "files": []}
    (repo / "a.txt").write_text("a\nb\n")
    files = client.get(f"/api/tasks/{tid}/diff").json()["files"]
    assert [(f["dir"], f["path"], f["status"], f["additions"]) for f in files] == [("repo", "a.txt", "modified", 1)]
