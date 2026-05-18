"""Smoke test: starts the FastAPI app via httpx ASGI transport, runs a few requests.

Does not bind a real port. Run via `make smoke` after `make install`.
Also exercises a couple of CLI subcommands via subprocess to catch entry-point regressions.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Use a temporary DB to avoid touching the real one.
_tmp_root = Path(tempfile.mkdtemp())
tmp_db = _tmp_root / "tasks.db"
os.environ["NTASKER_DB"] = str(tmp_db)

from ntasker import db as db_module  # noqa: E402
from ntasker.app import app  # noqa: E402

db_module.set_db_path(tmp_db)
db_module.init_db()

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def assert_ok(resp, expected_status: int = 200) -> None:
    if resp.status_code != expected_status:
        print(f"FAIL {resp.request.method} {resp.request.url} -> {resp.status_code}")
        print(resp.text)
        sys.exit(1)


def main() -> int:
    # 1. GET / returns HTML.
    r = client.get("/")
    assert_ok(r)
    assert "ntasker" in r.text, "index missing brand string"
    print("OK GET /")

    # 1b. GET /settings returns HTML. Default language = en (no Accept-Language
    # set on the TestClient). The page MUST carry the English page-title.
    r = client.get("/settings")
    assert_ok(r)
    assert "Settings" in r.text, "default settings page must be English"
    assert r.headers.get("Content-Language") == "en", (
        f"expected Content-Language=en, got {r.headers.get('Content-Language')!r}"
    )
    print("OK GET /settings (en default)")

    # 1c. With Accept-Language: de the same page must render the German title.
    r = client.get("/settings", headers={"Accept-Language": "de"})
    assert_ok(r)
    assert "Einstellungen" in r.text, "Accept-Language: de did not switch language"
    assert r.headers.get("Content-Language") == "de"
    print("OK GET /settings (Accept-Language: de)")

    # 1d. Quality-weighted Accept-Language picks the highest available.
    r = client.get("/", headers={"Accept-Language": "fr;q=0.9, de;q=0.8, en;q=0.5"})
    assert_ok(r)
    assert r.headers.get("Content-Language") == "de"
    print("OK GET / (Accept-Language q-weighting)")

    # 1e. Pinned setting overrides Accept-Language.
    pin = client.put("/api/settings/language", json={"value": "de"})
    assert_ok(pin)
    r = client.get("/", headers={"Accept-Language": "en"})
    assert_ok(r)
    assert r.headers.get("Content-Language") == "de", (
        "pinned language=de must override Accept-Language: en"
    )
    assert "Aufgaben" in r.text  # German page title in index.html
    print("OK GET / (pinned language=de overrides header)")

    # 1f. Unset pin to keep the rest of the suite hitting the auto path.
    rdel = client.delete("/api/settings/language")
    assert rdel.status_code == 204

    # 1g. Validator rejects an unknown language with HTTP 400.
    bad = client.put("/api/settings/language", json={"value": "fr"})
    assert bad.status_code == 400, f"expected 400 for invalid language, got {bad.status_code}"
    print("OK PUT /api/settings/language fr -> 400 (validator)")

    # 2. GET /api/projects returns enriched list ({name, open_count}).
    # Since v2.0 the list is derived from tasks; with an empty DB the
    # only entry is the __none__ sentinel.
    r = client.get("/api/projects")
    assert_ok(r)
    data = r.json()
    assert isinstance(data, list), f"expected list, got {type(data)}"
    assert data, "/api/projects must always include __none__ sentinel"
    assert data[0]["name"] == "__none__", f"first entry must be __none__, got {data[0]}"
    assert "open_count" in data[0]
    # v2.0: no X-Settings-Missing header anymore -- the setting is gone.
    assert "X-Settings-Missing" not in r.headers, (
        "X-Settings-Missing was removed with projects_dir in v2.0"
    )
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

    # 19. /api/phases returns the fixed 3-entry list in the workflow order.
    # v2.0: ``later`` and the ``__none__`` sentinel are gone; ``review`` is new.
    r = client.get("/api/phases")
    assert_ok(r)
    phases = r.json()
    assert isinstance(phases, list) and len(phases) == 3, f"expected 3 phases, got {phases}"
    assert [p["value"] for p in phases] == ["planned", "wip", "review"], (
        f"phase order wrong: {[p['value'] for p in phases]}"
    )
    assert all("label" in p and "open_count" in p for p in phases)
    print(f"OK GET /api/phases ({phases})")

    # 20. Multi-value phase filter (OR). phase=null now defaults server-side
    # to 'planned' (NOT NULL since v2.0); no __none__ sentinel anymore.
    ids: list[int] = []
    for phase, title in [
        ("planned", "p-planned"), ("wip", "p-wip"),
        ("review", "p-review"), (None, "p-default"),
    ]:
        rr = client.post("/api/tasks", json={"title": title, "phase": phase})
        assert_ok(rr, 201)
        body = rr.json()
        # phase=None must round-trip to the canonical default.
        if phase is None:
            assert body["phase"] == "planned", f"expected planned default, got {body['phase']}"
        ids.append(body["id"])

    r = client.get("/api/tasks?phase=planned&phase=review&status=open&archived=false")
    assert_ok(r)
    titles = sorted(t["title"] for t in r.json())
    assert "p-planned" in titles and "p-review" in titles and "p-default" in titles, (
        f"OR filter wrong: {titles}"
    )
    assert "p-wip" not in titles
    print(f"OK GET /api/tasks?phase=planned&phase=review -> {titles}")

    # 21. Phase + tag combine with AND.
    client.patch(f"/api/tasks/{ids[0]}", json={"tags": ["frontend"]})  # p-planned + frontend
    client.patch(f"/api/tasks/{ids[1]}", json={"tags": ["frontend"]})  # p-wip + frontend
    r = client.get("/api/tasks?phase=planned&tag=frontend&status=open&archived=false")
    assert_ok(r)
    titles = sorted(t["title"] for t in r.json())
    assert titles == ["p-planned"], f"AND-combination wrong: {titles}"
    print("OK GET /api/tasks?phase=planned&tag=frontend (AND)")

    # 21b. Legacy phase 'later' must be rejected with 422 (Pydantic Literal).
    bad_phase = client.post("/api/tasks", json={"title": "p-legacy-later", "phase": "later"})
    assert bad_phase.status_code == 422, (
        f"expected 422 for legacy phase=later, got {bad_phase.status_code}"
    )
    print("OK POST /api/tasks {phase: later} -> 422 (rejected)")

    # 21c. Invalid phase on PATCH returns 400, not 500.
    bad_patch = client.patch(f"/api/tasks/{ids[0]}", json={"phase": "bogus"})
    assert bad_patch.status_code in (400, 422), (
        f"expected 400/422 for bogus phase, got {bad_patch.status_code}"
    )
    # PATCH phase=null must coerce to the default (not crash on NOT NULL).
    coerce_null = client.patch(f"/api/tasks/{ids[1]}", json={"phase": None})
    assert_ok(coerce_null)
    assert coerce_null.json()["phase"] == "planned"
    print("OK PATCH phase=null -> planned (NOT NULL coercion)")

    # 22. /api/stats honors phase filter.
    r = client.get("/api/stats?phase=wip")
    assert_ok(r)
    assert "open" in r.json()
    print(f"OK GET /api/stats?phase=wip -> {r.json()}")

    # 23. /api/tags/cleanup removes dangling tags.
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

    # 24b. v1.5.0: search by numeric string also matches task id (exact).
    # Create a needle whose id we'll later look up via `?search=<id>`. The
    # title is deliberately non-numeric so the LIKE branch cannot match
    # the id-as-substring -- the only path to a hit is the new id clause.
    rr = client.post("/api/tasks", json={"title": "needle-for-id-search"})
    assert_ok(rr, 201)
    needle_id = rr.json()["id"]

    # Plain numeric search -> hit by id.
    r = client.get(f"/api/tasks?search={needle_id}")
    assert_ok(r)
    hit_ids = [t["id"] for t in r.json()]
    assert needle_id in hit_ids, (
        f"numeric search {needle_id!r} must surface task #{needle_id}, got {hit_ids}"
    )
    print(f"OK GET /api/tasks?search={needle_id} (numeric -> id match)")

    # `#<id>` form: leading hash must be stripped before the id match.
    r = client.get(f"/api/tasks?search=%23{needle_id}")  # %23 == '#'
    assert_ok(r)
    assert any(t["id"] == needle_id for t in r.json()), (
        f"search '#{needle_id}' must also surface task #{needle_id}"
    )
    print(f"OK GET /api/tasks?search=%23{needle_id} (#-prefixed -> id match)")

    # Non-numeric search must NOT trigger the id clause -- a search for
    # the needle's title still works via LIKE, but a fake textual id like
    # "needle-for-id-search" must not collide with id=<needle_id>.
    r = client.get("/api/tasks?search=needle-for-id-search")
    assert_ok(r)
    assert any(t["id"] == needle_id for t in r.json()), "title LIKE must still work"
    print("OK GET /api/tasks?search=<title> (LIKE branch still works)")

    # Non-existent numeric id returns an empty list (no traceback / 500).
    r = client.get("/api/tasks?search=999999999")
    assert_ok(r)
    assert not any(t["id"] == 999999999 for t in r.json())
    print("OK GET /api/tasks?search=<unknown-id> -> no match, no error")

    # CLI mirror: ntasker list --search <id> must surface the same task.
    proc = subprocess.run(
        ["ntasker", "list", "--search", str(needle_id), "--json"],
        capture_output=True, text=True, env={**os.environ, "NTASKER_DB": str(tmp_db)},
    )
    assert proc.returncode == 0, f"ntasker list --search failed: {proc.stderr}"
    import json as _json_id  # noqa: PLC0415
    cli_hits = [t["id"] for t in _json_id.loads(proc.stdout)]
    assert needle_id in cli_hits, (
        f"CLI search {needle_id!r} must surface task #{needle_id}, got {cli_hits}"
    )
    print(f"OK ntasker list --search {needle_id} --json (CLI id match)")

    # ------------------------------------------------------------------
    # Settings module (new in v1.0.0)
    # ------------------------------------------------------------------

    # 25. Empty settings list.
    r = client.get("/api/settings")
    assert_ok(r)
    assert r.json() == []
    print("OK GET /api/settings (empty)")

    # 26. v2.0 removed projects_dir entirely. v2.0.1 added an init_db
    # eviction step: writing the key still succeeds (it has no validator),
    # but the next init_db() boot removes the row -- there's no use in
    # keeping it around. Test both halves.
    r = client.put("/api/settings/projects_dir", json={"value": "/whatever"})
    assert_ok(r)
    assert client.get("/api/settings/projects_dir").status_code == 200
    db_module.init_db()
    assert client.get("/api/settings/projects_dir").status_code == 404, (
        "init_db() must evict the stale projects_dir setting row"
    )
    print("OK init_db evicts stale projects_dir setting (v2.0.1 cleanup)")

    # 26c. DELETE /api/tasks/<id> has no archived-only gate at the API
    # level: the modal-delete and `ntasker delete` paths rely on this.
    # List-view delete keeps the archived-only check in JS (UX choice).
    rr = client.post("/api/tasks", json={"title": "api-delete-open"})
    open_id = rr.json()["id"]
    r = client.delete(f"/api/tasks/{open_id}")
    assert_ok(r, 204)
    r = client.get(f"/api/tasks/{open_id}")
    assert r.status_code == 404, "open task must be hard-deletable via API"
    rr = client.post("/api/tasks", json={"title": "api-delete-archived"})
    arch_id = rr.json()["id"]
    client.patch(f"/api/tasks/{arch_id}", json={"archived": True})
    r = client.delete(f"/api/tasks/{arch_id}")
    assert_ok(r, 204)
    print("OK DELETE /api/tasks/<id> works on open + archived states")

    # 27-30. Projects are now derived from tasks (v2.0). Test the implicit
    # creation + garbage-collection contract:
    #   a) creating a task with project="foo" surfaces "foo" in /api/projects
    #   b) deleting the last task carrying "foo" makes it disappear
    #   c) PATCH that empties the project also triggers the GC
    # The list is ordered case-insensitively after the __none__ sentinel.

    # a) implicit creation
    rr = client.post(
        "/api/tasks", json={"title": "gc-foo-1", "project": "gc-foo", "archived": False}
    )
    assert_ok(rr, 201)
    foo_id = rr.json()["id"]
    rr = client.post(
        "/api/tasks", json={"title": "gc-foo-2", "project": "gc-foo"}
    )
    assert_ok(rr, 201)
    foo2_id = rr.json()["id"]

    r = client.get("/api/projects")
    names = [p["name"] for p in r.json()]
    assert "gc-foo" in names, f"new project must appear: {names}"
    foo_entry = next(p for p in r.json() if p["name"] == "gc-foo")
    assert foo_entry["open_count"] == 2, foo_entry
    print(f"OK POST /api/tasks project=gc-foo -> project auto-created ({foo_entry})")

    # b) delete one task -- project still present (one left)
    r = client.delete(f"/api/tasks/{foo_id}")
    # delete only works on archived; archive first then delete.
    if r.status_code != 204:
        client.patch(f"/api/tasks/{foo_id}", json={"archived": True})
        r = client.delete(f"/api/tasks/{foo_id}")
    assert_ok(r, 204)
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "gc-foo" in names, "project must still be there with one remaining task"
    print("OK GET /api/projects (project sticks while >=1 task references it)")

    # c) delete the last task -- project vanishes from the sidebar feed
    client.patch(f"/api/tasks/{foo2_id}", json={"archived": True})
    r = client.delete(f"/api/tasks/{foo2_id}")
    assert_ok(r, 204)
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "gc-foo" not in names, f"empty project must be GC'd: {names}"
    print("OK GET /api/projects (empty project auto-removed)")

    # d) PATCH that empties project also triggers GC. Create -> PATCH to
    # different project -> first project drops out if no other tasks reference it.
    rr = client.post("/api/tasks", json={"title": "gc-bar", "project": "gc-bar"})
    bar_id = rr.json()["id"]
    assert "gc-bar" in [p["name"] for p in client.get("/api/projects").json()]
    client.patch(f"/api/tasks/{bar_id}", json={"project": "gc-baz"})
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "gc-bar" not in names and "gc-baz" in names, (
        f"PATCH must move project membership and GC the empty one: {names}"
    )
    print("OK PATCH project=gc-baz GCs the now-empty gc-bar")

    # e) Empty / whitespace-only project string collapses to null server-side
    # (no phantom "" entry in the sidebar feed).
    rr = client.post("/api/tasks", json={"title": "gc-empty", "project": "   "})
    assert_ok(rr, 201)
    assert rr.json()["project"] is None, f"empty trimmed project must be NULL, got {rr.json()['project']!r}"
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "" not in names and "   " not in names, names
    print("OK POST /api/tasks project='   ' -> NULL (no phantom entry)")

    # 30b. default_view setting (v2.0): validator whitelist + template injection.
    # The server renders `window.__defaultView = "..."` in a <script> block;
    # tracker() in app.js reads it for the fresh-browser fallback.
    r = client.get("/")
    assert_ok(r)
    assert 'window.__defaultView = "list"' in r.text, (
        "default_view 'list' must be injected when unset"
    )
    print("OK GET / -> window.__defaultView = 'list' default")

    # Invalid value rejected with 400.
    bad = client.put("/api/settings/default_view", json={"value": "table"})
    assert bad.status_code == 400, f"expected 400 for invalid default_view, got {bad.status_code}"
    print("OK PUT /api/settings/default_view table -> 400 (validator)")

    # Valid value 'kanban' rounds through to the HTML.
    r = client.put("/api/settings/default_view", json={"value": "kanban"})
    assert_ok(r)
    assert r.json()["value"] == "kanban"
    r = client.get("/")
    assert_ok(r)
    assert 'window.__defaultView = "kanban"' in r.text, (
        "default_view 'kanban' must reach the rendered template"
    )
    print("OK PUT /api/settings/default_view kanban + reflected in /")

    # Clean up so the rest of the suite stays on the default.
    client.delete("/api/settings/default_view")

    # ------------------------------------------------------------------
    # CLI smoke checks (subprocess) -- verifies the entry point and a
    # round-trip through the same DB the in-process tests used.
    # ------------------------------------------------------------------

    env = {**os.environ, "NTASKER_DB": str(tmp_db)}

    # 31. ntasker --version
    proc = subprocess.run(["ntasker", "--version"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"ntasker --version failed: {proc.stderr}"
    assert "ntasker" in proc.stdout.lower()
    print(f"OK ntasker --version -> {proc.stdout.strip()}")

    # 32. ntasker list --json (must be valid JSON, even if empty-ish).
    proc = subprocess.run(
        ["ntasker", "list", "--json"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, f"ntasker list --json failed: {proc.stderr}"
    import json as _json
    parsed = _json.loads(proc.stdout)
    assert isinstance(parsed, list), "ntasker list --json must return a JSON array"
    print(f"OK ntasker list --json ({len(parsed)} tasks)")

    # 32b. ntasker delete: deliberate hard-delete from the CLI. The
    # confirmation prompt is bypassed with --yes; deletion works regardless
    # of archived state (the keyboard input itself is the safety net).
    rr_create = subprocess.run(
        ["ntasker", "add", "--title", "cli-delete-target"],
        capture_output=True, text=True, env=env,
    )
    assert rr_create.returncode == 0, rr_create.stderr
    # Pull the id back via list.
    rr_list = subprocess.run(
        ["ntasker", "list", "--json"], capture_output=True, text=True, env=env,
    )
    target_id = next(
        t["id"] for t in _json.loads(rr_list.stdout) if t["title"] == "cli-delete-target"
    )
    rr_del = subprocess.run(
        ["ntasker", "delete", str(target_id), "--yes"],
        capture_output=True, text=True, env=env,
    )
    assert rr_del.returncode == 0, rr_del.stderr
    rr_list2 = subprocess.run(
        ["ntasker", "list", "--json"], capture_output=True, text=True, env=env,
    )
    assert not any(t["id"] == target_id for t in _json.loads(rr_list2.stdout)), (
        f"task #{target_id} must be gone after `ntasker delete --yes`"
    )
    # Non-existent id -> exit 1 with the "not found" message.
    rr_miss = subprocess.run(
        ["ntasker", "delete", "999999", "--yes"],
        capture_output=True, text=True, env=env,
    )
    assert rr_miss.returncode == 1, rr_miss
    print("OK ntasker delete <id> --yes (round-trip + not-found path)")

    # 33. ntasker config list --json against same DB.
    proc = subprocess.run(
        ["ntasker", "config", "list", "--json"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"ntasker config list --json failed: {proc.stderr}"
    assert _json.loads(proc.stdout) == [], "config should be empty"
    print("OK ntasker config list --json")

    # 34. v2.0 phase migration: a pre-v2.0 DB with phase='later' / NULL is
    # collapsed into 'planned' on init_db. We can't simulate that on the
    # current tmp_db (NOT NULL is already enforced) -- build a separate
    # pre-v2.0 shaped DB, seed legacy rows, then point init_db at it.
    import sqlite3 as _sqlite  # noqa: PLC0415
    legacy_db = _tmp_root / "legacy.db"
    with _sqlite.connect(legacy_db) as _conn:
        # Replicate the v1.x schema exactly: phase is nullable, no default.
        _conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                phase TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT,
                archived INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO tasks (title, phase) VALUES ('legacy-later', 'later');
            INSERT INTO tasks (title, phase) VALUES ('legacy-null', NULL);
            INSERT INTO tasks (title, phase) VALUES ('legacy-wip', 'wip');
            """
        )
    # Run the migration against the legacy file. Idempotent.
    db_module.set_db_path(legacy_db)
    db_module.init_db()
    with _sqlite.connect(legacy_db) as _conn:
        rows = _conn.execute("SELECT title, phase FROM tasks ORDER BY id").fetchall()
    migrated = {title: phase for title, phase in rows}
    assert migrated == {
        "legacy-later": "planned",
        "legacy-null": "planned",
        "legacy-wip": "wip",
    }, migrated
    print(f"OK init_db phase migration -> {migrated}")
    # Restore the main tmp_db binding so any later test still talks to it.
    db_module.set_db_path(tmp_db)

    # ------------------------------------------------------------------
    # Regression: AlpineJS x-show MUST NOT live on an element whose
    # *static* class attribute carries a Bootstrap display utility
    # (`d-flex`, `d-block`, `d-grid`, `d-inline*`). Bootstrap sets those
    # with `!important`, which beats Alpine's inline `style="display:
    # none"`. The Bootstrap modal pattern intentionally uses `:class` to
    # *toggle* d-block when shown -- that's fine and explicitly excluded.
    # v2.0 removed the projects_dir banner that originally motivated this
    # check; we keep it generic so a future banner can't regress.
    # ------------------------------------------------------------------
    import re

    r = client.get("/")
    assert_ok(r)
    html = r.text
    matches = re.findall(r"<[^>]*x-show=\"[^\"]+\"[^>]*>", html)
    assert matches, "no x-show elements found in /; template structure changed"
    # Only static `class="..."` counts; Alpine's `:class="..."` (a
    # reactive bind) does NOT trigger Bootstrap's !important since the
    # binding is evaluated per state.
    static_class_re = re.compile(r"(?<![:\w])class=\"([^\"]*)\"")
    bad = []
    for tag in matches:
        for static_classes in static_class_re.findall(tag):
            if re.search(r"\b(d-flex|d-block|d-grid|d-inline[a-z-]*)\b", static_classes):
                bad.append(tag)
                break
    assert not bad, (
        "AlpineJS x-show must NOT sit on an element with a Bootstrap display utility "
        f"({{d-flex,d-block,d-grid,d-inline*}}) in its static class; offending tag(s): {bad}"
    )
    print(f"OK index template hygiene ({len(matches)} x-show wrappers, none on static d-*)")

    # Same trap in /settings: any `x-show` on a `d-*` element is a bug.
    r = client.get("/settings")
    assert_ok(r)
    settings_html = r.text
    open_tags = re.findall(r"<[^>]*x-show=\"[^\"]+\"[^>]*>", settings_html)
    bad = [tag for tag in open_tags if re.search(
        r"\bclass=\"[^\"]*\b(d-flex|d-block|d-grid|d-inline[a-z-]*)\b", tag
    )]
    assert not bad, (
        "AlpineJS x-show on a Bootstrap display utility in /settings; "
        f"offending tag(s): {bad}"
    )
    print(f"OK settings template hygiene (no x-show on d-* utility, {len(open_tags)} x-show tags scanned)")

    # v2.0: the X-Settings-Missing: projects_dir header path is gone for good.
    # The header must not appear under any circumstance, with or without
    # the legacy ENV var set. We test both branches here.
    r = client.get("/api/projects")
    assert_ok(r)
    assert "X-Settings-Missing" not in r.headers, (
        "X-Settings-Missing must not be set anywhere since v2.0"
    )
    os.environ["NTASKER_PROJECTS_DIR"] = "/anything-or-nothing"
    try:
        r = client.get("/api/projects")
        assert_ok(r)
        assert "X-Settings-Missing" not in r.headers, (
            "stale NTASKER_PROJECTS_DIR ENV must be silently ignored in v2.0+"
        )
        print("OK GET /api/projects has no X-Settings-Missing (with or without ENV)")
    finally:
        del os.environ["NTASKER_PROJECTS_DIR"]

    # ------------------------------------------------------------------
    # Claude Code asset installer (new in v1.1.0)
    # ------------------------------------------------------------------

    from ntasker import claude_assets as _ca  # noqa: E402, PLC0415

    # 36. Packaged assets exist + readable.
    skill_md = _ca.read_skill_md()
    assert "ntasker Skill" in skill_md, "SKILL.md must contain heading"
    template = _ca.read_command_template()
    assert "{COMMAND_NAME}" in template and "{HELPER_PATH}" in template, (
        "task.md.template must keep both placeholders"
    )
    helper = _ca.read_helper_py()
    assert "_ntasker_loader" in helper or "ntasker_loader" in helper
    print(f"OK packaged assets readable (skill={len(skill_md)}, template={len(template)}, helper={len(helper)})")

    # 37. render_command substitutes both placeholders for the default name.
    rendered = _ca.render_command(template, "task", "~/.claude/commands/_ntasker_loader.py")
    assert "{COMMAND_NAME}" not in rendered and "{HELPER_PATH}" not in rendered
    assert "Task #$ARGUMENTS" in rendered
    assert "/task" in rendered
    assert "_ntasker_loader.py" in rendered
    print("OK render_command substitutes both placeholders (default 'task')")

    # 37b. Custom command name renders correctly into header + body.
    rendered_foo = _ca.render_command(template, "foo", "~/.claude/commands/_ntasker_loader.py")
    assert "/foo" in rendered_foo
    print("OK render_command --command-name=foo writes /foo into header")

    # 37c. Skill asset is generic -- no user-specific routing or paths.
    skill_lower = skill_md.lower()
    forbidden_skill = [
        "nerdocs tracker",
        "nerdocs/projekte",
        "/home/christian",
        "feedback_tracker_",
        "feedback_no_git_commits",
        "feedback_doc_writing_style",
        "friday",
        "hermine",
        "kader",
    ]
    for needle in forbidden_skill:
        assert needle not in skill_lower, (
            f"SKILL.md must be generic -- found user-specific token {needle!r}"
        )
    # Legacy alias deliberately kept in description as a trigger word.
    assert "nerdocs-tracker" in skill_md, (
        "SKILL.md should keep `nerdocs-tracker` as legacy trigger alias"
    )
    print("OK SKILL.md is generic (no user-specific routing) + keeps legacy alias")

    # 37d. Rendered command template is generic -- no persona names.
    rendered_lower = rendered.lower()
    forbidden_template = [
        "friday",
        "hermine",
        "percy",
        "alastor",
        "bill",
        "fudge",
        "dolores",
        "arthur",
        "albus",
        "neville",
        "kader",
        "christians inbox",
        "/home/christian",
        "coding_regeln",
        "zero_trust",
    ]
    for needle in forbidden_template:
        assert needle not in rendered_lower, (
            f"task.md.template must be generic -- found user-specific token {needle!r}"
        )
    print("OK rendered task.md is generic (no persona names, no user paths)")

    # 37d2. Rendered command template enforces the v1.5.0 review-handoff
    # contract: the agent never sets status=done. It may move the task to
    # phase=review on completion; status=done stays user-only.
    import re as _re_check  # noqa: PLC0415
    assert _re_check.search(r"[Nn]ever mark `?status:\s*done`?", rendered), (
        "task.md.template must explicitly forbid marking status: done autonomously"
    )
    assert "autonomously" in rendered, (
        "task.md.template must use the word 'autonomously' in the prohibition"
    )
    # The agent's autonomous write is `phase: review` -- must be the
    # explicit completion action, not status=done.
    assert "phase" in rendered and "review" in rendered, (
        "task.md.template must instruct the agent to move to phase=review on completion"
    )
    # Bare `ntasker done` instructions for the agent are gone -- closing
    # the task is the user's job now.
    assert "ntasker done" not in rendered, (
        "task.md.template must not tell the agent to call `ntasker done` -- "
        "since v1.5.0 status=done is user-only and never appears in agent steps"
    )
    # The old bare "On completion: mark the task as done." wording must be gone.
    assert "On completion:** mark the task as done" not in rendered, (
        "task.md.template still carries the pre-1.2.1 'mark the task as done' "
        "wording"
    )
    print("OK task.md enforces review-handoff (no autonomous done writes)")

    # 37d3. Bash-comment trap: every `$ARGUMENTS` in a Bash context (the
    # `!`-backtick line, the rendered `ntasker done ...` invocation, the
    # `curl ... /api/tasks/...` URL) MUST be double-quoted. Otherwise bash
    # interprets a `#`-prefixed task id as a comment start and the helper
    # sees zero arguments. v1.2.2 fix.
    # Forbidden: trailing-unquoted forms (i.e. $ARGUMENTS NOT followed by a
    # closing double-quote). The curl URL puts $ARGUMENTS inside a quoted
    # string, so `/api/tasks/$ARGUMENTS"` is fine; bare `$ARGUMENTS` at
    # end-of-token is the trap.
    bash_unquoted_traps = [
        "_ntasker_loader.py $ARGUMENTS`",  # ends backtick line
        "_ntasker_loader.py $ARGUMENTS\n",
        "ntasker patch $ARGUMENTS\n",
        "/api/tasks/$ARGUMENTS \\",  # curl URL without closing quote
        "/api/tasks/$ARGUMENTS\n",
    ]
    for needle in bash_unquoted_traps:
        assert needle not in rendered, (
            "task.md.template still has unquoted $ARGUMENTS in a Bash context "
            f"(found {needle!r}); /task #<id> would be eaten by bash comment parsing"
        )
    # Positive check: the quoted forms are present.
    assert '_ntasker_loader.py "$ARGUMENTS"' in rendered, (
        "task.md.template missing quoted helper invocation"
    )
    assert 'ntasker patch "$ARGUMENTS"' in rendered, (
        "task.md.template missing quoted `ntasker patch` invocation for the "
        "review-handoff step"
    )
    assert '"http://127.0.0.1:8766/api/tasks/$ARGUMENTS"' in rendered, (
        "task.md.template missing quoted curl URL"
    )
    print("OK task.md quotes $ARGUMENTS in all Bash contexts (#<id> survives)")

    # 37d4. End-to-end: simulate `bash -c` with a #-prefixed argument and
    # ensure the id reaches the wrapped command. We strip the slash-command
    # frontmatter and replace the helper invocation with a tiny echo so the
    # test stays self-contained -- this checks the *quoting*, not the loader.
    import shlex as _shlex  # noqa: PLC0415
    # Pull the `!`-backtick line out of the template body and strip the leading "!".
    backtick_line = next(
        (ln for ln in rendered.splitlines() if ln.startswith("!`") and ln.endswith("`")),
        None,
    )
    assert backtick_line is not None, "rendered template missing `!`-backtick bash line"
    bash_cmd = backtick_line[2:-1]  # strip leading "!`" and trailing "`"
    # Replace the helper path with a stub that echoes its $1 verbatim.
    bash_cmd_stub = bash_cmd.replace(
        'python3 ~/.claude/commands/_ntasker_loader.py',
        'printf %s',
    )
    # Set ARGUMENTS to "#49" and run the stub through bash.
    proc = subprocess.run(
        ["bash", "-c", f'ARGUMENTS="#49"; {bash_cmd_stub}'],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"bash stub failed: {proc.stderr}"
    assert proc.stdout.strip() == "#49", (
        f"#-prefix swallowed by bash comment parsing -- got {proc.stdout!r}, "
        "expected '#49'. Quoting in task.md.template is broken."
    )
    print("OK bash-c simulation: '#49' survives the rendered backtick line")

    # 37e. Loader accepts both "187" and "#187" -- and rejects garbage.
    import importlib.util  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    helper_src = _ca.read_helper_py()
    with tempfile.TemporaryDirectory() as _tmp:
        _loader_path = _Path(_tmp) / "_ntasker_loader.py"
        _loader_path.write_text(helper_src, encoding="utf-8")
        _spec = importlib.util.spec_from_file_location("_ntasker_loader", str(_loader_path))
        assert _spec and _spec.loader
        _loader = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_loader)
    import re as _re  # noqa: PLC0415

    # Internal: the validation pattern accepts both forms.
    assert _re.fullmatch(r"#?\d+", "187"), "loader regex must match '187'"
    assert _re.fullmatch(r"#?\d+", "#187"), "loader regex must match '#187'"
    assert not _re.fullmatch(r"#?\d+", "##187"), "loader regex must reject '##187'"
    assert not _re.fullmatch(r"#?\d+", "abc"), "loader regex must reject 'abc'"

    # End-to-end: feed an unreachable id through main() with both prefixes;
    # main() should validate and proceed to "not found" (exit 1), not bail
    # on validation (exit 2). Guarantees both forms hit load_via_*.
    # ``try_autostart`` is also mocked so the test never actually spawns a
    # background server -- v1.4.0 added a lazy-spawn step between server
    # and CLI fallback.
    _orig_load_server = _loader.load_via_server
    _orig_load_cli = _loader.load_via_cli
    _orig_autostart = _loader.try_autostart
    _loader.load_via_server = lambda tid: None  # type: ignore[assignment]
    _loader.load_via_cli = lambda tid: None  # type: ignore[assignment]
    _loader.try_autostart = lambda: False  # type: ignore[assignment]
    try:
        rc_plain = _loader.main(["_ntasker_loader.py", "999999999"])
        rc_hash = _loader.main(["_ntasker_loader.py", "#999999999"])
        rc_bad = _loader.main(["_ntasker_loader.py", "##99"])
    finally:
        _loader.load_via_server = _orig_load_server
        _loader.load_via_cli = _orig_load_cli
        _loader.try_autostart = _orig_autostart
    assert rc_plain == 1, f"plain id should reach not-found path, got rc={rc_plain}"
    assert rc_hash == 1, f"#-prefixed id should reach not-found path, got rc={rc_hash}"
    assert rc_bad == 2, f"double-# id should fail validation (rc=2), got rc={rc_bad}"
    print("OK loader accepts both '187' and '#187' (rejects '##187')")

    # 37e2. New v1.4.0 lazy-spawn path: when load_via_server initially fails
    # AND try_autostart returns True, main() must re-probe the server. We
    # verify the call sequence: first server miss -> autostart success ->
    # second server hit returns the task -> main() prints + exits 0.
    _calls: list[str] = []

    def _server_seq(tid: str) -> dict | None:
        _calls.append(f"server({tid})")
        # First call: miss. Second call (after autostart): hit.
        return None if _calls.count(f"server({tid})") == 1 else {
            "id": int(tid), "title": "autostart-test", "status": "open",
            "phase": None, "priority": "normal", "tags": [],
            "archived": False, "created_at": "2026-01-01T00:00:00",
            "description": "",
        }

    def _autostart_ok() -> bool:
        _calls.append("autostart")
        return True

    _loader.load_via_server = _server_seq  # type: ignore[assignment]
    _loader.try_autostart = _autostart_ok  # type: ignore[assignment]
    _loader.load_via_cli = lambda tid: None  # type: ignore[assignment]
    try:
        rc_spawn = _loader.main(["_ntasker_loader.py", "77"])
    finally:
        _loader.load_via_server = _orig_load_server
        _loader.load_via_cli = _orig_load_cli
        _loader.try_autostart = _orig_autostart
    assert rc_spawn == 0, f"lazy-spawn path should return 0, got rc={rc_spawn}"
    assert _calls == ["server(77)", "autostart", "server(77)"], (
        f"unexpected call sequence: {_calls}"
    )
    print("OK loader lazy-autostart: server-miss -> spawn -> server-hit")

    # 37f. Click-to-copy puts "/task #<id>" on the clipboard, not just "#<id>".
    _app_js_path = _Path(__file__).parent / "src" / "ntasker" / "static" / "app.js"
    _app_js = _app_js_path.read_text(encoding="utf-8")
    assert "`/task #${id}`" in _app_js, (
        "copyId must place the ready-to-paste slash-command on clipboard"
    )
    # Old "#${id}" alone should no longer be the clipboard payload inside copyId.
    _copy_id_block = _app_js.split("async copyId(id) {", 1)[1].split("},", 1)[0]
    assert "`#${id}`" not in _copy_id_block, (
        "copyId still copies plain '#<id>' -- expected '/task #<id>'"
    )
    print("OK copyId clipboard payload is '/task #<id>' (slash-command ready)")

    # 38. validate_command_name rejects path traversal / injection.
    for bad in ["../etc", "with/slash", "dot.s", "spa ce", "", "name;rm"]:
        try:
            _ca.validate_command_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"validate_command_name should have rejected {bad!r}")
    assert _ca.validate_command_name("task") == "task"
    assert _ca.validate_command_name("my-cmd_2") == "my-cmd_2"
    print("OK validate_command_name rejects path traversal + injection")

    # 39. Install into a fresh test home: writes 3 files.
    test_home = _tmp_root / "claude-home-fresh"
    plan = _ca.expected_files(test_home, "task")
    assert len(plan) == 3
    assert {p.label for p in plan} == {"skill", "command", "helper"}
    result = _ca.install_assets(test_home, "task")
    assert result.success, f"install must succeed on fresh home: {result.actions}"
    assert all(a.action == "write" for a in result.actions), [a.action for a in result.actions]
    for af in plan:
        assert af.path.exists(), f"missing file after install: {af.path}"
    print(f"OK install_assets on fresh {test_home} -> 3 writes")

    # 40. --check after install: status.installed=True, drift=False (CLI exit 0).
    status = _ca.scan_status(test_home, command_name="task")
    assert status.installed is True and status.drift is False
    print("OK scan_status after install -> installed=True, drift=False")

    # 41. CLI subprocess: install-claude-assets --check -> exit 0.
    proc = subprocess.run(
        ["ntasker", "install-claude-assets", "--check", "--claude-home", str(test_home)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"--check after install must exit 0, got {proc.returncode}: {proc.stderr}"
    print("OK CLI install-claude-assets --check -> exit 0")

    # 42. Drift detection: modify SKILL.md manually -> --check -> exit 1.
    skill_path = test_home / "skills" / "ntasker" / "SKILL.md"
    original = skill_path.read_text(encoding="utf-8")
    skill_path.write_text(original + "\n# DRIFT MARKER\n", encoding="utf-8")
    proc = subprocess.run(
        ["ntasker", "install-claude-assets", "--check", "--claude-home", str(test_home)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1, f"drift must -> exit 1, got {proc.returncode}"
    print("OK CLI --check exits 1 on drift")

    # 43. Without --force, install aborts (exit 3).
    proc = subprocess.run(
        ["ntasker", "install-claude-assets", "--claude-home", str(test_home)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 3, f"abort on drift without --force, got {proc.returncode}"
    assert "BLOCKED" in (proc.stdout + proc.stderr)
    print("OK install without --force on drift -> exit 3 (BLOCKED)")

    # 44. With --force: drift gets backed up + overwritten.
    pre_files = sorted(p.name for p in skill_path.parent.iterdir())
    proc = subprocess.run(
        ["ntasker", "install-claude-assets", "--force", "--claude-home", str(test_home)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"--force install must succeed, got {proc.returncode}: {proc.stderr}"
    post_files = sorted(p.name for p in skill_path.parent.iterdir())
    backups = [n for n in post_files if n.startswith("SKILL.md.bak.") and n not in pre_files]
    assert backups, f"--force must create a timestamped backup; pre={pre_files}, post={post_files}"
    # Restored content == packaged.
    assert skill_path.read_text(encoding="utf-8") == _ca.read_skill_md()
    print(f"OK --force creates timestamped backup ({backups[0]}) and restores file")

    # 45. --check without install at all -> exit 2.
    empty_home = _tmp_root / "claude-home-empty"
    proc = subprocess.run(
        ["ntasker", "install-claude-assets", "--check", "--claude-home", str(empty_home)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2, f"missing install must -> exit 2, got {proc.returncode}"
    assert "MISSING" in proc.stdout
    print("OK CLI --check on empty home -> exit 2")

    # 46. --dry-run does not touch the filesystem.
    dry_home = _tmp_root / "claude-home-dry"
    proc = subprocess.run(
        ["ntasker", "install-claude-assets", "--dry-run", "--claude-home", str(dry_home)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "[dry-run]" in proc.stdout, "dry-run output must be marked"
    assert not (dry_home / "skills" / "ntasker" / "SKILL.md").exists(), (
        "dry-run must not create files"
    )
    assert not (dry_home / "commands").exists(), "dry-run must not create dirs"
    print("OK --dry-run prints actions without touching filesystem")

    # 47. --command-name=foo writes foo.md (not task.md).
    foo_home = _tmp_root / "claude-home-foo"
    proc = subprocess.run(
        [
            "ntasker", "install-claude-assets",
            "--command-name", "foo", "--claude-home", str(foo_home),
        ],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert (foo_home / "commands" / "foo.md").exists()
    assert not (foo_home / "commands" / "task.md").exists()
    # Helper file name stays the same regardless of slash command name.
    assert (foo_home / "commands" / "_ntasker_loader.py").exists()
    foo_md = (foo_home / "commands" / "foo.md").read_text(encoding="utf-8")
    assert "/foo" in foo_md
    print("OK --command-name=foo writes foo.md and keeps helper name")

    # 48. Bad command name rejected with exit 2.
    proc = subprocess.run(
        [
            "ntasker", "install-claude-assets",
            "--command-name", "../escape", "--claude-home", str(_tmp_root / "x"),
        ],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2, "path-traversal command name must be rejected"
    print("OK path-traversal --command-name rejected with exit 2")

    # 49. /api/claude-assets/status returns the expected JSON shape.
    os.environ["NTASKER_CLAUDE_HOME"] = str(test_home)
    try:
        # Re-install so test_home is clean (no DRIFT MARKER) and we know exact state.
        _ca.install_assets(test_home, "task", force=True)
        r = client.get("/api/claude-assets/status")
        assert_ok(r)
        body = r.json()
        assert set(body.keys()) >= {
            "installed", "drift", "package_version", "claude_home", "files"
        }
        assert body["installed"] is True
        assert body["drift"] is False
        assert isinstance(body["files"], list) and len(body["files"]) == 3
        assert all("expected_hash" in f and f["expected_hash"].startswith("sha256:")
                   for f in body["files"])
        print(f"OK GET /api/claude-assets/status -> installed=True drift=False ({len(body['files'])} files)")
    finally:
        del os.environ["NTASKER_CLAUDE_HOME"]

    # 49b. /healthz: DB-free liveness probe used by `serve --detach` and external
    # supervisors. Must return 200 with {"ok": True, "version": <pkg>} -- no DB
    # access, so a half-broken install still answers quickly.
    r = client.get("/healthz")
    assert_ok(r)
    body = r.json()
    assert body.get("ok") is True, f"/healthz must return ok=true, got {body}"
    assert body.get("version"), f"/healthz must include version, got {body}"
    from ntasker import __version__ as _pkg_version  # noqa: PLC0415
    assert body["version"] == _pkg_version, (
        f"/healthz version drift: app={body['version']!r} != pkg={_pkg_version!r}"
    )
    print(f"OK GET /healthz -> {body}")

    # 49c. `ntasker serve --detach` end-to-end: pick a free port, spawn a
    # detached child, probe /healthz, then shut it down. Verifies the
    # cross-platform spawn path AND idempotency (second --detach exits 0
    # without spawning a second server).
    import signal as _signal  # noqa: PLC0415
    import socket as _socket  # noqa: PLC0415
    import time as _time  # noqa: PLC0415
    import urllib.request as _urlreq  # noqa: PLC0415
    import json as _json2  # noqa: PLC0415

    def _free_port() -> int:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _healthz_get(port: int, timeout: float = 0.5) -> dict | None:
        try:
            with _urlreq.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return _json2.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    detach_port = _free_port()
    detach_db = _tmp_root / "detach.db"
    # Pin the CLI to English so the test assertions stay locale-stable
    # regardless of the host's LANG (the i18n resolver honours LANG when
    # the DB has no language setting, which is the case for a fresh DB).
    detach_env = {
        **os.environ,
        "NTASKER_DB": str(detach_db),
        "LANG": "C",
        "LC_ALL": "C",
    }

    # First call: must spawn the server and wait for /healthz.
    proc = subprocess.run(
        ["ntasker", "serve", "--detach", "--port", str(detach_port)],
        capture_output=True, text=True, env=detach_env, timeout=10,
    )
    assert proc.returncode == 0, (
        f"first --detach must succeed, got rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "started detached" in proc.stdout, (
        f"first --detach output should mention 'started detached', got {proc.stdout!r}"
    )

    # Extract the PID from the success line for clean teardown later.
    import re as _re_pid  # noqa: PLC0415
    m = _re_pid.search(r"pid (\d+)", proc.stdout)
    assert m, f"could not parse pid from --detach output: {proc.stdout!r}"
    detached_pid = int(m.group(1))

    try:
        # /healthz must answer (the CLI waited for this, so 1 retry is enough).
        body = _healthz_get(detach_port, timeout=2.0)
        assert body and body.get("ok") is True, (
            f"detached server not answering /healthz on port {detach_port}, got {body!r}"
        )
        print(f"OK ntasker serve --detach (port={detach_port}, pid={detached_pid}) -> /healthz ok")

        # Second call: must be idempotent (no second spawn, exits 0).
        proc2 = subprocess.run(
            ["ntasker", "serve", "--detach", "--port", str(detach_port)],
            capture_output=True, text=True, env=detach_env, timeout=5,
        )
        assert proc2.returncode == 0, (
            f"idempotent --detach must succeed, got rc={proc2.returncode}\n"
            f"stdout={proc2.stdout}\nstderr={proc2.stderr}"
        )
        assert "already running" in proc2.stdout, (
            f"idempotent --detach should say 'already running', got {proc2.stdout!r}"
        )
        print("OK ntasker serve --detach (second call) -> idempotent 'already running'")

        # --detach + --reload must be rejected with exit 2 (no spawn).
        proc3 = subprocess.run(
            ["ntasker", "serve", "--detach", "--reload", "--port", str(_free_port())],
            capture_output=True, text=True, env=detach_env, timeout=5,
        )
        assert proc3.returncode == 2, (
            f"--detach + --reload must be rejected, got rc={proc3.returncode}"
        )
        assert "mutually exclusive" in (proc3.stdout + proc3.stderr)
        print("OK ntasker serve --detach --reload -> rejected (exit 2)")

        # 49d. `ntasker stop`: ask the running detached server to die via
        # POST /shutdown. After the call, /healthz must stop answering.
        # Then run `ntasker stop` again -- idempotent on an already-stopped
        # server, must exit 0 with a friendly "not running" message.
        proc_stop = subprocess.run(
            ["ntasker", "stop", "--port", str(detach_port)],
            capture_output=True, text=True, env=detach_env, timeout=10,
        )
        assert proc_stop.returncode == 0, (
            f"ntasker stop must succeed, got rc={proc_stop.returncode}\n"
            f"stdout={proc_stop.stdout}\nstderr={proc_stop.stderr}"
        )
        assert "stopped" in proc_stop.stdout, (
            f"ntasker stop output should mention 'stopped', got {proc_stop.stdout!r}"
        )
        # The server is gone -- /healthz must refuse the connection. Give
        # the OS a few moments to release the port.
        for _ in range(20):
            if _healthz_get(detach_port, timeout=0.2) is None:
                break
            _time.sleep(0.05)
        else:
            raise AssertionError(
                f"/healthz still answering on port {detach_port} after ntasker stop"
            )
        print(f"OK ntasker stop (port={detach_port}) -> server gone, /healthz dead")

        # Idempotent: stop on an already-stopped server is exit 0 + note.
        proc_stop2 = subprocess.run(
            ["ntasker", "stop", "--port", str(detach_port)],
            capture_output=True, text=True, env=detach_env, timeout=5,
        )
        assert proc_stop2.returncode == 0, (
            f"idempotent stop must succeed, got rc={proc_stop2.returncode}"
        )
        assert "no server running" in proc_stop2.stdout, (
            f"idempotent stop should say 'no server running', got {proc_stop2.stdout!r}"
        )
        print("OK ntasker stop (already stopped) -> idempotent 'no server running'")

        # 49e. Diagnostic: a foreign listener (anything bound to the port
        # that does NOT speak /healthz) must trigger the "something is
        # listening but does not answer /healthz" path and exit 1 -- NOT
        # the misleading "no server running" branch.
        foreign_port = _free_port()
        foreign_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        foreign_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        foreign_sock.bind(("127.0.0.1", foreign_port))
        foreign_sock.listen(1)
        try:
            proc_stop3 = subprocess.run(
                ["ntasker", "stop", "--port", str(foreign_port)],
                capture_output=True, text=True, env=detach_env, timeout=5,
            )
            assert proc_stop3.returncode == 1, (
                f"foreign listener should exit 1, got rc={proc_stop3.returncode}\n"
                f"stdout={proc_stop3.stdout}\nstderr={proc_stop3.stderr}"
            )
            assert "does not" in proc_stop3.stderr, (
                f"foreign listener stop should explain the situation, "
                f"got stderr={proc_stop3.stderr!r}"
            )
            assert "no server running" not in proc_stop3.stdout, (
                "must NOT report 'no server running' when the port is bound"
            )
            print(
                f"OK ntasker stop (foreign listener on :{foreign_port}) "
                "-> diagnostic exit 1"
            )
        finally:
            foreign_sock.close()

        # Mark the detached child as already-gone so the teardown skips
        # the SIGTERM (it would just race with our successful stop).
        detached_pid = -1

    finally:
        # Clean teardown: signal the detached child and wait briefly.
        # If the stop test above already killed the process, detached_pid
        # was set to -1 -- skip the signal in that case.
        if detached_pid > 0:
            try:
                os.kill(detached_pid, _signal.SIGTERM)
                for _ in range(20):
                    try:
                        os.kill(detached_pid, 0)  # probe
                    except ProcessLookupError:
                        break
                    _time.sleep(0.05)
            except ProcessLookupError:
                pass  # already gone

    # 50. boot_drift_warning returns None on a clean install.
    os.environ["NTASKER_CLAUDE_HOME"] = str(test_home)
    try:
        _ca.install_assets(test_home, "task", force=True)
        assert _ca.boot_drift_warning() is None
        # Now drift the file -> warning surfaces.
        skill_path = test_home / "skills" / "ntasker" / "SKILL.md"
        skill_path.write_text("DRIFT", encoding="utf-8")
        warn = _ca.boot_drift_warning()
        assert warn is not None and "out of date" in warn
        print("OK boot_drift_warning fires only on installed+drift state")
    finally:
        del os.environ["NTASKER_CLAUDE_HOME"]

    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
