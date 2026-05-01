"""Smoke test: starts the FastAPI app via httpx ASGI transport, runs a few requests.

Does not bind a real port. Run via `make smoke` after `make install`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Use a temporary DB to avoid touching the real one.
tmp_db = Path(tempfile.mkdtemp()) / "tasks.db"
os.environ["TRACKER_DB_OVERRIDE"] = str(tmp_db)

import app as app_module  # noqa: E402

app_module.DB_PATH = tmp_db
app_module.init_db()

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app_module.app)


def assert_ok(resp, expected_status: int = 200) -> None:
    if resp.status_code != expected_status:
        print(f"FAIL {resp.request.method} {resp.request.url} -> {resp.status_code}")
        print(resp.text)
        sys.exit(1)


def main() -> int:
    # 1. GET / returns HTML.
    r = client.get("/")
    assert_ok(r)
    assert "nerdocs Tracker" in r.text, "index missing brand string"
    print("OK GET /")

    # 2. GET /api/projects returns enriched list ({name, open_count}).
    r = client.get("/api/projects")
    assert_ok(r)
    data = r.json()
    assert isinstance(data, list), f"expected list, got {type(data)}"
    assert data, "/api/projects must always include __none__ sentinel"
    assert data[0]["name"] == "__none__", f"first entry must be __none__, got {data[0]}"
    assert "open_count" in data[0]
    print(f"OK GET /api/projects ({len(data)} entries, [0]={data[0]})")

    # 3. GET /api/tags is a list (may be empty initially).
    r = client.get("/api/tags")
    assert_ok(r)
    assert isinstance(r.json(), list)
    print("OK GET /api/tags")

    # 4. POST /api/tasks creates a task with tags.
    r = client.post(
        "/api/tasks",
        json={
            "title": "Smoke test task",
            "phase": "wip",
            "project": None,
            "tags": ["frontend", "Bug"],
        },
    )
    assert_ok(r, 201)
    task = r.json()
    assert task["title"] == "Smoke test task"
    assert task["phase"] == "wip"
    assert task["status"] == "open"
    assert "source" not in task, "source field must be gone from API output"
    assert sorted(task["tags"]) == ["bug", "frontend"], f"tags wrong: {task['tags']}"
    task_id = task["id"]
    print(f"OK POST /api/tasks (id={task_id}, tags={task['tags']})")

    # 5. GET /api/tasks lists it with tags.
    r = client.get("/api/tasks?status=open&archived=false")
    assert_ok(r)
    listed = r.json()
    found = next((t for t in listed if t["id"] == task_id), None)
    assert found is not None
    assert sorted(found["tags"]) == ["bug", "frontend"]
    print("OK GET /api/tasks (filters + tags)")

    # 6. Tag-filter: ?tag=frontend returns the task.
    r = client.get("/api/tasks?tag=frontend")
    assert_ok(r)
    assert any(t["id"] == task_id for t in r.json())
    print("OK GET /api/tasks?tag=frontend")

    # 7. Tag-filter OR semantics: ?tag=frontend&tag=nonsense returns the task once.
    r = client.get("/api/tasks?tag=frontend&tag=nonsense")
    assert_ok(r)
    matching = [t for t in r.json() if t["id"] == task_id]
    assert len(matching) == 1, f"expected exactly 1 hit (DISTINCT), got {len(matching)}"
    print("OK GET /api/tasks?tag=a&tag=b (OR + dedupe)")

    # 8. Tag-filter for a nonexistent tag returns no matches.
    r = client.get("/api/tasks?tag=nonsense")
    assert_ok(r)
    assert not any(t["id"] == task_id for t in r.json())
    print("OK GET /api/tasks?tag=nonsense (empty)")

    # 9. /api/tags now reports the two tags with open_count=1.
    r = client.get("/api/tags")
    assert_ok(r)
    by_name = {t["name"]: t for t in r.json()}
    assert "frontend" in by_name and by_name["frontend"]["open_count"] == 1
    assert "bug" in by_name and by_name["bug"]["open_count"] == 1
    print("OK GET /api/tags (open_count)")

    # 10. PATCH replaces the tag set (not append).
    r = client.patch(f"/api/tasks/{task_id}", json={"tags": ["api"]})
    assert_ok(r)
    assert r.json()["tags"] == ["api"]
    print("OK PATCH /api/tasks (tags replace)")

    # 11. PATCH -> done sets completed_at.
    r = client.patch(f"/api/tasks/{task_id}", json={"status": "done"})
    assert_ok(r)
    assert r.json()["status"] == "done"
    assert r.json()["completed_at"] is not None
    print("OK PATCH status=done")

    # 12. /api/projects __none__ open_count went down (task is now done).
    r = client.get("/api/projects")
    assert_ok(r)
    none_entry = next(p for p in r.json() if p["name"] == "__none__")
    assert none_entry["open_count"] == 0
    print("OK GET /api/projects (open_count reflects done task)")

    # 13. PATCH archived.
    r = client.patch(f"/api/tasks/{task_id}", json={"archived": True})
    assert_ok(r)
    assert r.json()["archived"] is True
    print("OK PATCH archived=true")

    # 14. /api/stats with tag-filter still returns counts dict.
    r = client.get("/api/stats?tag=api")
    assert_ok(r)
    counts = r.json()
    assert set(counts.keys()) == {"open", "done", "archive"}
    print(f"OK GET /api/stats?tag=api -> {counts}")

    # 15. DELETE.
    r = client.delete(f"/api/tasks/{task_id}")
    assert_ok(r, 204)
    print("OK DELETE /api/tasks/{id}")

    # 16. After delete, tags still exist (dangling tags policy: keep, low-cost).
    r = client.get("/api/tags")
    assert_ok(r)
    print(f"OK GET /api/tags after delete ({len(r.json())} tags)")

    # 17. 404 on missing task.
    r = client.patch("/api/tasks/99999", json={"status": "done"})
    assert_ok(r, 404)
    print("OK PATCH missing -> 404")

    # 18. tasks table no longer has a `source` column.
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    finally:
        conn.close()
    assert "source" not in cols, f"source column must be gone from tasks, got {cols}"
    print(f"OK tasks columns ({cols})")

    # 19. /api/phases returns the fixed 4-entry list in the workflow order.
    r = client.get("/api/phases")
    assert_ok(r)
    phases = r.json()
    assert isinstance(phases, list) and len(phases) == 4
    assert [p["value"] for p in phases] == ["wip", "planned", "later", "__none__"]
    assert all("label" in p and "open_count" in p for p in phases)
    print(f"OK GET /api/phases ({phases})")

    # 20. Multi-value phase filter (OR + IS NULL via __none__).
    # Re-create some tasks to exercise the filter (the smoke task above is now
    # archived and was DELETEd).
    ids: list[int] = []
    for phase, title in [
        ("wip", "p-wip"), ("planned", "p-planned"),
        ("later", "p-later"), (None, "p-nophase"),
    ]:
        rr = client.post("/api/tasks", json={"title": title, "phase": phase})
        assert_ok(rr, 201)
        ids.append(rr.json()["id"])

    r = client.get("/api/tasks?phase=wip&phase=__none__&status=open&archived=false")
    assert_ok(r)
    titles = sorted(t["title"] for t in r.json())
    assert "p-wip" in titles and "p-nophase" in titles
    assert "p-planned" not in titles and "p-later" not in titles
    print(f"OK GET /api/tasks?phase=wip&phase=__none__ -> {titles}")

    # 21. Phase + tag combine with AND.
    client.patch(f"/api/tasks/{ids[0]}", json={"tags": ["frontend"]})  # p-wip + frontend
    client.patch(f"/api/tasks/{ids[1]}", json={"tags": ["frontend"]})  # p-planned + frontend
    r = client.get("/api/tasks?phase=wip&tag=frontend&status=open&archived=false")
    assert_ok(r)
    titles = sorted(t["title"] for t in r.json())
    assert titles == ["p-wip"], f"AND-combination wrong: {titles}"
    print("OK GET /api/tasks?phase=wip&tag=frontend (AND)")

    # 22. /api/stats honors phase filter.
    r = client.get("/api/stats?phase=wip")
    assert_ok(r)
    assert "open" in r.json()
    print(f"OK GET /api/stats?phase=wip -> {r.json()}")

    # 23. /api/tags/cleanup removes dangling tags.
    # Create a stray tag first by attaching+detaching via PATCH.
    rr = client.post("/api/tasks", json={"title": "tag-leak", "tags": ["dangling-zzz"]})
    leak_id = rr.json()["id"]
    client.patch(f"/api/tasks/{leak_id}", json={"tags": []})  # detach -> tag dangles
    r = client.post("/api/tags/cleanup")
    assert_ok(r)
    body = r.json()
    assert body["removed"] >= 1
    assert "dangling-zzz" in body["removed_names"]
    print(f"OK POST /api/tags/cleanup -> {body}")

    # 24. Idempotent: second call returns removed=0.
    r = client.post("/api/tags/cleanup")
    assert_ok(r)
    assert r.json()["removed"] == 0
    print("OK POST /api/tags/cleanup idempotent")

    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
