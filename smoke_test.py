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

    # 1b. GET /settings returns HTML.
    r = client.get("/settings")
    assert_ok(r)
    assert "Einstellungen" in r.text
    print("OK GET /settings")

    # 2. GET /api/projects returns enriched list ({name, open_count}).
    r = client.get("/api/projects")
    assert_ok(r)
    data = r.json()
    assert isinstance(data, list), f"expected list, got {type(data)}"
    assert data, "/api/projects must always include __none__ sentinel"
    assert data[0]["name"] == "__none__", f"first entry must be __none__, got {data[0]}"
    assert "open_count" in data[0]
    # projects_dir not configured -> X-Settings-Missing header present.
    assert "projects_dir" in r.headers.get("X-Settings-Missing", ""), (
        "projects_dir nicht konfiguriert -> Header muss vorhanden sein"
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

    # 19. /api/phases returns the fixed 4-entry list in the workflow order.
    r = client.get("/api/phases")
    assert_ok(r)
    phases = r.json()
    assert isinstance(phases, list) and len(phases) == 4
    assert [p["value"] for p in phases] == ["wip", "planned", "later", "__none__"]
    assert all("label" in p and "open_count" in p for p in phases)
    print(f"OK GET /api/phases ({phases})")

    # 20. Multi-value phase filter (OR + IS NULL via __none__).
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

    # ------------------------------------------------------------------
    # Settings module (new in v1.0.0)
    # ------------------------------------------------------------------

    # 25. Empty settings list.
    r = client.get("/api/settings")
    assert_ok(r)
    assert r.json() == []
    print("OK GET /api/settings (empty)")

    # 26. Bad value rejected with 400 (and the validator's message).
    r = client.put("/api/settings/projects_dir", json={"value": "/does/not/exist/xyz"})
    assert_ok(r, 400)
    assert "projects_dir" in r.json()["detail"]
    print("OK PUT /api/settings/projects_dir bad -> 400")

    # 27. Good value accepted.
    good_dir = str(_tmp_root)  # the temp dir itself is a valid readable dir.
    r = client.put("/api/settings/projects_dir", json={"value": good_dir})
    assert_ok(r)
    assert r.json()["value"] == good_dir
    print("OK PUT /api/settings/projects_dir good")

    # 28. /api/projects no longer flags the missing setting once it is set.
    r = client.get("/api/projects")
    assert_ok(r)
    assert "X-Settings-Missing" not in r.headers
    print("OK GET /api/projects (header gone after configure)")

    # 29. GET single setting.
    r = client.get("/api/settings/projects_dir")
    assert_ok(r)
    assert r.json()["value"] == good_dir
    print("OK GET /api/settings/projects_dir")

    # 30. DELETE setting.
    r = client.delete("/api/settings/projects_dir")
    assert_ok(r, 204)
    r = client.get("/api/settings/projects_dir")
    assert_ok(r, 404)
    print("OK DELETE /api/settings/projects_dir + 404 on re-get")

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

    # 33. ntasker config list --json against same DB.
    proc = subprocess.run(
        ["ntasker", "config", "list", "--json"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"ntasker config list --json failed: {proc.stderr}"
    assert _json.loads(proc.stdout) == [], "config should be empty"
    print("OK ntasker config list --json")

    # ------------------------------------------------------------------
    # Regression: AlpineJS x-show MUST NOT live on an element that also
    # carries a Bootstrap display utility (`d-flex`, `d-block`, `d-grid`,
    # `d-inline*`). Bootstrap sets those with `!important`, which beats
    # the inline `style="display: none"` Alpine writes to hide the node
    # -- the result is a banner that shows even though the bound state
    # is `false`. This bit the projects_dir banner pre-1.0.0 and we keep
    # it pinned so the trap cannot resurface in templates.
    # ------------------------------------------------------------------
    import re

    # The banner wrapper must be `<div x-show="projectsDirMissing" x-cloak>`,
    # i.e. no `class="..."` carrying a Bootstrap `d-*` utility.
    r = client.get("/")
    assert_ok(r)
    html = r.text
    # Locate the projectsDirMissing element and grab its full opening tag.
    matches = re.findall(r"<[^>]*x-show=\"projectsDirMissing\"[^>]*>", html)
    assert matches, "projectsDirMissing element not found in /"
    bad = [tag for tag in matches if re.search(
        r"\bclass=\"[^\"]*\b(d-flex|d-block|d-grid|d-inline[a-z-]*)\b", tag
    )]
    assert not bad, (
        "AlpineJS x-show must NOT sit on an element with a Bootstrap display utility "
        f"({{d-flex,d-block,d-grid,d-inline*}}); offending tag(s): {bad}"
    )
    print(f"OK banner template hygiene (x-show wrapper has no d-* utility) -> {matches[0]}")

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

    # Header-side regression: while projects_dir is set, /api/projects must
    # NOT carry X-Settings-Missing (covers DB and ENV branches).
    # 34. DB branch already covered by step 28 above; here we add ENV.
    os.environ["NTASKER_PROJECTS_DIR"] = str(_tmp_root)
    try:
        r = client.get("/api/projects")
        assert_ok(r)
        assert "X-Settings-Missing" not in r.headers, (
            "ENV-Override NTASKER_PROJECTS_DIR muss den Header unterdruecken"
        )
        print("OK GET /api/projects (no X-Settings-Missing when ENV is set)")
    finally:
        del os.environ["NTASKER_PROJECTS_DIR"]

    # 35. With neither DB nor ENV set the header MUST come back. This is
    # the inverse of 28+34 and pins the validator's "not configured" branch.
    r = client.get("/api/projects")
    assert_ok(r)
    assert "projects_dir" in r.headers.get("X-Settings-Missing", ""), (
        "Ohne DB- und ENV-Konfiguration MUSS X-Settings-Missing: projects_dir gesetzt sein"
    )
    print("OK GET /api/projects (header back when no DB + no ENV)")

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

    # 37d2. Rendered command template enforces ask-before-done (v1.2.1).
    # The /task workflow must NOT instruct the agent to autonomously mark the
    # task as done -- it must ask the user first and only proceed on explicit OK.
    assert ("Ask" in rendered) or ("ask" in rendered), (
        "task.md.template must contain an ask-before-done step ('Ask' / 'ask')"
    )
    # Explicit prohibition wording must be present.
    import re as _re_check  # noqa: PLC0415
    assert _re_check.search(r"[Nn]ever mark `?status:\s*done`?", rendered), (
        "task.md.template must explicitly forbid marking status: done autonomously"
    )
    assert "autonomously" in rendered, (
        "task.md.template must use the word 'autonomously' in the prohibition"
    )
    # If a `ntasker done` line is rendered, it must sit in a section that also
    # talks about asking / explicit user OK -- not as a bare "do it" instruction.
    if "ntasker done" in rendered:
        assert ("Ask the user" in rendered) or ("explicit user OK" in rendered), (
            "task.md.template renders `ntasker done` but is missing the "
            "ask-first wording ('Ask the user' / 'explicit user OK')"
        )
    # The old bare "On completion: mark the task as done." wording must be gone.
    assert "On completion:** mark the task as done" not in rendered, (
        "task.md.template still carries the pre-1.2.1 'mark the task as done' "
        "wording without an ask-first gate"
    )
    print("OK task.md asks before marking done (no autonomous status writes)")

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
    _orig_load_server = _loader.load_via_server
    _orig_load_cli = _loader.load_via_cli
    _loader.load_via_server = lambda tid: None  # type: ignore[assignment]
    _loader.load_via_cli = lambda tid: None  # type: ignore[assignment]
    try:
        rc_plain = _loader.main(["_ntasker_loader.py", "999999999"])
        rc_hash = _loader.main(["_ntasker_loader.py", "#999999999"])
        rc_bad = _loader.main(["_ntasker_loader.py", "##99"])
    finally:
        _loader.load_via_server = _orig_load_server
        _loader.load_via_cli = _orig_load_cli
    assert rc_plain == 1, f"plain id should reach not-found path, got rc={rc_plain}"
    assert rc_hash == 1, f"#-prefixed id should reach not-found path, got rc={rc_hash}"
    assert rc_bad == 2, f"double-# id should fail validation (rc=2), got rc={rc_bad}"
    print("OK loader accepts both '187' and '#187' (rejects '##187')")

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
