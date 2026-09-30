"""Tests for ``POST /api/tags/cleanup`` (sidebar "clean up tags" action)."""

from fastapi.testclient import TestClient

from ntasker.app import app
from ntasker.db import init_db, set_db_path

BASE = "http://127.0.0.1:8766"


def test_cleanup_removes_only_tags_without_any_task(tmp_path):
    db = tmp_path / "tasks.db"
    set_db_path(db)
    init_db(db)
    with TestClient(app, base_url=BASE) as client:
        open_t = client.post("/api/tasks", json={"title": "o", "tags": ["keep-open"]}).json()
        done_t = client.post("/api/tasks", json={"title": "d", "tags": ["keep-done"]}).json()
        client.patch(f"/api/tasks/{done_t['id']}", json={"status": "done"})
        gone = client.post("/api/tasks", json={"title": "g", "tags": ["orphan"]}).json()
        # Stripping the tag from its only task leaves "orphan" dangling.
        client.patch(f"/api/tasks/{gone['id']}", json={"tags": []})
        assert open_t["id"]

        r = client.post("/api/tags/cleanup")
        assert r.status_code == 200
        assert r.json() == {"removed": 1, "removed_names": ["orphan"]}
        names = {t["name"] for t in client.get("/api/tags").json()}
        assert names == {"keep-open", "keep-done"}

        # Idempotent: a second run finds nothing.
        assert client.post("/api/tags/cleanup").json() == {"removed": 0, "removed_names": []}
