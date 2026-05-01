"""CLI entry for ntasker.

Subcommands:

| Cmd                  | What it does                                       |
|----------------------|----------------------------------------------------|
| ``init``             | Create the schema at the resolved DB path          |
| ``serve``            | Run uvicorn (bind 127.0.0.1, port 8766 by default) |
| ``list``             | Read-only listing with filters; ``--json`` for raw |
| ``show <id>``        | Single-task detail; ``--json`` for raw             |
| ``add``              | Create a task                                      |
| ``done <id>``        | Mark a task ``done``                               |
| ``patch <id>``       | Patch arbitrary fields                             |
| ``tag-add <id> <t>`` | Append a tag                                       |
| ``tag-rm  <id> <t>`` | Remove a tag                                       |
| ``stats``            | Tab counts honoring filters                        |
| ``config``           | KV-store: list / get / set / unset                 |
| ``assets``           | Vendor-asset cache: fetch / remove / status        |

Global flags: ``--db <path>`` (highest precedence) and ``--version``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ntasker import __version__
from ntasker.assets import (
    MANIFEST,
    assets_dir,
    local_assets_complete,
    local_path_for,
    resolve_mode,
)
from ntasker.claude_assets import (
    install_assets,
    resolve_claude_home,
    scan_status,
    validate_command_name,
)
from ntasker.db import (
    get_conn,
    init_db,
    load_tags_for,
    normalize_tags,
    row_to_task,
    set_db_path,
    set_task_tags,
)
from ntasker.paths import resolve_db_path, warn_if_missing
from ntasker.settings import (
    delete_setting,
    get_setting_raw,
    list_settings,
    set_setting,
)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_json(data: Any) -> None:
    """Print JSON with stable indent. Used for ``--json`` flags."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _truncate(s: str | None, n: int = 60) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_tasks_table(tasks: list[dict]) -> None:
    """Compact human-readable listing."""
    if not tasks:
        print("(keine Tasks)")
        return
    # Headers
    print(f"{'ID':>5} {'STAT':<6} {'PR':<3} {'PH':<8} {'PROJEKT':<22} TITEL")
    print("-" * 80)
    for t in tasks:
        prio_short = {"critical": "!!!", "high": "!!", "normal": "·", "low": ".."}.get(
            t.get("priority") or "normal", "·"
        )
        ph = (t.get("phase") or "-")[:8]
        proj = (t.get("project") or "(cross)")[:22]
        title = _truncate(t.get("title") or "", 60)
        print(f"{t['id']:>5} {t['status']:<6} {prio_short:<3} {ph:<8} {proj:<22} {title}")


def _print_task_detail(t: dict) -> None:
    print(f"#{t['id']} {t['title']}")
    print(f"  Projekt:      {t.get('project') or '(cross-project)'}")
    print(f"  Status:       {t['status']}")
    print(f"  Phase:        {t.get('phase') or '-'}")
    print(f"  Prioritaet:   {t.get('priority') or 'normal'}")
    print(f"  Tags:         {', '.join(t.get('tags') or []) or '-'}")
    print(f"  Archiviert:   {bool(t.get('archived'))}")
    print(f"  Erstellt:     {t.get('created_at') or '-'}")
    if t.get("completed_at"):
        print(f"  Abgeschlossen:{t['completed_at']}")
    if t.get("description"):
        print()
        print("  --- Beschreibung ---")
        for line in (t["description"]).splitlines():
            print(f"  {line}")


# ---------------------------------------------------------------------------
# DB helpers used by CLI subcommands
# ---------------------------------------------------------------------------


def _query_tasks(args: argparse.Namespace) -> list[dict]:
    """In-process equivalent of ``GET /api/tasks`` with the same filter semantics."""
    sql = "SELECT tasks.* FROM tasks WHERE 1=1"
    params: list[object] = []

    if args.project:
        names = [p for p in args.project if p and p != "__none__"]
        include_null = "__none__" in args.project
        clauses = []
        if names:
            placeholders = ", ".join("?" for _ in names)
            clauses.append(f"project IN ({placeholders})")
            params.extend(names)
        if include_null:
            clauses.append("project IS NULL")
        if clauses:
            sql += " AND (" + " OR ".join(clauses) + ")"

    if args.tag:
        norm = normalize_tags(args.tag)
        if norm:
            placeholders = ", ".join("?" for _ in norm)
            sql += (
                f" AND tasks.id IN (SELECT tt.task_id FROM task_tags tt "
                f"JOIN tags t ON t.id = tt.tag_id "
                f"WHERE t.name IN ({placeholders}))"
            )
            params.extend(norm)

    if args.phase:
        valid = {"wip", "planned", "later"}
        names = [p for p in args.phase if p in valid]
        include_null = "__none__" in args.phase
        clauses = []
        if names:
            placeholders = ", ".join("?" for _ in names)
            clauses.append(f"phase IN ({placeholders})")
            params.extend(names)
        if include_null:
            clauses.append("phase IS NULL")
        if clauses:
            sql += " AND (" + " OR ".join(clauses) + ")"

    if args.priority:
        valid = {"critical", "high", "normal", "low"}
        names = [p for p in args.priority if p in valid]
        if names:
            placeholders = ", ".join("?" for _ in names)
            sql += f" AND priority IN ({placeholders})"
            params.extend(names)

    if args.status:
        sql += " AND status = ?"
        params.append(args.status)
    if args.archived is not None:
        sql += " AND archived = ?"
        params.append(1 if args.archived else 0)
    if args.search:
        sql += " AND (title LIKE ? OR COALESCE(description, '') LIKE ?)"
        like = f"%{args.search}%"
        params.extend([like, like])

    sql += " ORDER BY archived ASC, status ASC, created_at DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for r in rows:
            tags = load_tags_for(conn, int(r["id"]))
            out.append(row_to_task(r, tags))
    return out


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create the schema at the active DB path."""
    from ntasker import db as _db  # noqa: PLC0415  -- read module-level DB_PATH

    init_db()
    print(f"ntasker: DB initialisiert bei {_db.DB_PATH}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI app via uvicorn. Bind hardcoded to 127.0.0.1.

    Importing uvicorn lazily keeps ``ntasker --version`` fast and lets the
    CLI work in environments where uvicorn is missing (read-only ops).
    """
    try:
        import uvicorn  # noqa: PLC0415  -- lazy import on purpose
    except ImportError:
        print("ntasker: uvicorn nicht installiert. `pip install ntasker[serve]`", file=sys.stderr)
        return 2
    # Make sure schema exists -- avoid first-request 500s on a fresh DB.
    init_db()
    # Best-effort drift hint for installed Claude Code assets. Only logs
    # when assets are installed AND drifted; never blocks the boot.
    from ntasker.claude_assets import boot_drift_warning  # noqa: PLC0415

    warning = boot_drift_warning()
    if warning:
        print(warning, file=sys.stderr)
    uvicorn.run(
        "ntasker.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Read-only listing."""
    tasks = _query_tasks(args)
    if args.json:
        _print_json(tasks)
    else:
        _print_tasks_table(tasks)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Single-task detail."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if row is None:
            print(f"ntasker: Task #{args.task_id} nicht gefunden", file=sys.stderr)
            return 1
        tags = load_tags_for(conn, args.task_id)
    task = row_to_task(row, tags)
    if args.json:
        _print_json(task)
    else:
        _print_task_detail(task)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    if args.priority not in {"critical", "high", "normal", "low"}:
        print(f"ntasker: Ungueltige Prioritaet: {args.priority!r}", file=sys.stderr)
        return 2
    if args.phase is not None and args.phase not in {"wip", "planned", "later"}:
        print(f"ntasker: Ungueltige Phase: {args.phase!r}", file=sys.stderr)
        return 2
    norm_tags = normalize_tags(args.tag or [])
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (project, title, description, phase, priority) "
            "VALUES (?, ?, ?, ?, ?)",
            (args.project, args.title, args.description, args.phase, args.priority),
        )
        new_id = int(cur.lastrowid)
        if norm_tags:
            set_task_tags(conn, new_id, norm_tags)
    print(f"#{new_id} angelegt: {args.title}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (now, args.task_id),
        )
        if cur.rowcount == 0:
            print(f"ntasker: Task #{args.task_id} nicht gefunden", file=sys.stderr)
            return 1
    print(f"#{args.task_id} -> done")
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    fields: dict[str, Any] = {}
    if args.title is not None:
        fields["title"] = args.title
    if args.description is not None:
        fields["description"] = args.description
    if args.project is not None:
        fields["project"] = None if args.project == "" else args.project
    if args.phase is not None:
        fields["phase"] = None if args.phase == "" else args.phase
    if args.priority is not None:
        if args.priority not in {"critical", "high", "normal", "low"}:
            print(f"ntasker: Ungueltige Prioritaet: {args.priority!r}", file=sys.stderr)
            return 2
        fields["priority"] = args.priority
    if args.archived is not None:
        fields["archived"] = 1 if args.archived else 0
    if args.status is not None:
        if args.status not in {"open", "done"}:
            print(f"ntasker: Ungueltiger Status: {args.status!r}", file=sys.stderr)
            return 2
        fields["status"] = args.status
        fields["completed_at"] = (
            datetime.now().isoformat(timespec="seconds") if args.status == "done" else None
        )

    if not fields:
        print("ntasker: nichts zu aendern (mindestens ein Feld angeben)", file=sys.stderr)
        return 2

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    params = [*fields.values(), args.task_id]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", params)
        if cur.rowcount == 0:
            print(f"ntasker: Task #{args.task_id} nicht gefunden", file=sys.stderr)
            return 1
    print(f"#{args.task_id} aktualisiert ({', '.join(fields.keys())})")
    return 0


def cmd_tag_add(args: argparse.Namespace) -> int:
    norm = normalize_tags([args.tag])
    if not norm:
        print(f"ntasker: leerer Tag-Name: {args.tag!r}", file=sys.stderr)
        return 2
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if exists is None:
            print(f"ntasker: Task #{args.task_id} nicht gefunden", file=sys.stderr)
            return 1
        current = load_tags_for(conn, args.task_id)
        merged = list(dict.fromkeys([*current, *norm]))
        set_task_tags(conn, args.task_id, merged)
    print(f"#{args.task_id} Tag +{norm[0]}")
    return 0


def cmd_tag_rm(args: argparse.Namespace) -> int:
    target = args.tag.strip().lower()
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if exists is None:
            print(f"ntasker: Task #{args.task_id} nicht gefunden", file=sys.stderr)
            return 1
        current = load_tags_for(conn, args.task_id)
        if target not in current:
            print(f"ntasker: Tag {target!r} nicht vorhanden auf #{args.task_id}", file=sys.stderr)
            return 1
        new_tags = [t for t in current if t != target]
        set_task_tags(conn, args.task_id, new_tags)
    print(f"#{args.task_id} Tag -{target}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Tab-Counts honoring filters."""
    args.status = None
    args.archived = None
    tasks = _query_tasks(args)
    counts = {"open": 0, "done": 0, "archive": 0}
    for t in tasks:
        if t["archived"]:
            counts["archive"] += 1
        elif t["status"] == "open":
            counts["open"] += 1
        elif t["status"] == "done":
            counts["done"] += 1
    if getattr(args, "json", False):
        _print_json(counts)
    else:
        for k, v in counts.items():
            print(f"  {k:<8} {v}")
    return 0


# Settings subcommands -------------------------------------------------------


def cmd_config_list(args: argparse.Namespace) -> int:
    rows = list_settings()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("(keine Settings gesetzt)")
        return 0
    for r in rows:
        print(f"  {r['key']:<24} {r['value']}    ({r['updated_at']})")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    row = get_setting_raw(args.key)
    if row is None:
        print(f"ntasker: setting {args.key!r} nicht gesetzt", file=sys.stderr)
        return 1
    if args.json:
        _print_json(row)
    else:
        print(row["value"])
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    try:
        row = set_setting(args.key, args.value)
    except ValueError as exc:
        print(f"ntasker: {exc}", file=sys.stderr)
        return 2
    print(f"{row['key']} = {row['value']}")
    return 0


def cmd_config_unset(args: argparse.Namespace) -> int:
    if not delete_setting(args.key):
        print(f"ntasker: setting {args.key!r} war nicht gesetzt", file=sys.stderr)
        return 1
    print(f"{args.key} entfernt")
    return 0


# Claude Code assets ---------------------------------------------------------


def cmd_install_claude_assets(args: argparse.Namespace) -> int:
    """Install / check the packaged Claude Code skill + slash command.

    Modes:

    * ``--check``    -- read-only inspection. Exit 0 = identical, 1 = drift,
      2 = not installed.
    * ``--dry-run``  -- print actions without touching the filesystem.
    * default        -- write missing files; skip identical; abort on drift
      unless ``--force`` is given (then backup + overwrite).
    """
    try:
        command_name = validate_command_name(args.command_name)
    except ValueError as exc:
        print(f"ntasker: {exc}", file=sys.stderr)
        return 2
    try:
        claude_home = resolve_claude_home(args.claude_home)
    except Exception as exc:
        print(f"ntasker: invalid --claude-home: {exc}", file=sys.stderr)
        return 2

    if args.check:
        status = scan_status(claude_home, command_name=command_name)
        for fs in status.files:
            if not fs.installed:
                marker = "MISSING"
            elif fs.drift:
                marker = "DRIFT"
            else:
                marker = "OK"
            print(f"  {marker:<8} {fs.label:<8} {fs.path}")
        if not status.installed:
            print("ntasker: Claude assets not installed.")
            return 2
        if status.drift:
            print(
                "ntasker: Claude assets installed but out of date. "
                "Run `ntasker install-claude-assets --force` to update."
            )
            return 1
        print("ntasker: Claude assets up to date.")
        return 0

    result = install_assets(
        claude_home,
        command_name=command_name,
        force=args.force,
        dry_run=args.dry_run,
    )

    written = [a for a in result.actions if a.action == "write"]
    backed = [a for a in result.actions if a.action == "backup-and-write"]
    skipped = [a for a in result.actions if a.action == "skip"]
    blocked = [a for a in result.actions if a.action == "blocked"]

    prefix = "[dry-run] " if result.dry_run else ""
    for a in written:
        print(f"  {prefix}WRITE   {a.label:<8} {a.path}")
    for a in backed:
        print(
            f"  {prefix}BACKUP  {a.label:<8} {a.path}  ->  {a.backup_path}"
        )
    for a in skipped:
        print(f"  {prefix}SKIP    {a.label:<8} {a.path}  ({a.reason})")
    for a in blocked:
        print(
            f"  {prefix}BLOCKED {a.label:<8} {a.path}  ({a.reason})",
            file=sys.stderr,
        )

    if blocked:
        print(
            "ntasker: aborted -- one or more files differ. "
            "Pass --force to overwrite (with timestamped backup).",
            file=sys.stderr,
        )
        return 3
    print(
        f"ntasker: {'would install' if result.dry_run else 'installed'} "
        f"to {claude_home}"
    )
    return 0


# Vendor assets (CDN/local) -------------------------------------------------


def _verify_sri(data: bytes, expected_sri: str) -> bool:
    """Verify ``data`` against an SRI string of the form ``sha384-<base64>``.

    Used by ``ntasker assets fetch`` -- after every download, before we
    persist the file. A mismatch means either the CDN was tampered with
    or the manifest is stale; either way: drop the bytes, do not write.
    """
    import base64  # noqa: PLC0415  -- lazy import on purpose
    import hashlib  # noqa: PLC0415

    algo, _, b64 = expected_sri.partition("-")
    if algo != "sha384" or not b64:
        return False
    actual = base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")
    return actual == b64


def cmd_assets_fetch(args: argparse.Namespace) -> int:
    """Download every manifest entry into the user-data vendor cache.

    SRI is verified before the file is written. On hash mismatch the
    bytes are discarded and the command exits non-zero -- never trust
    the CDN, always verify.
    """
    try:
        import httpx  # noqa: PLC0415  -- lazy import on purpose
    except ImportError:
        print(
            "ntasker: httpx fehlt. Installiere ntasker neu (httpx ist Runtime-Dep).",
            file=sys.stderr,
        )
        return 2

    target_root = assets_dir()
    target_root.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    written: list[str] = []
    skipped: list[str] = []

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for spec in MANIFEST:
            target = local_path_for(spec)
            if target.is_file() and not args.force:
                # Check existing file's SRI before skipping.
                existing = target.read_bytes()
                if _verify_sri(existing, spec.sri):
                    skipped.append(spec.name)
                    print(f"  SKIP   {spec.name:<22} {target} (SRI ok)")
                    continue
                print(
                    f"  STALE  {spec.name:<22} {target} (SRI mismatch -- re-fetching)",
                    file=sys.stderr,
                )

            print(f"  FETCH  {spec.name:<22} {spec.cdn_url}")
            try:
                resp = client.get(spec.cdn_url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  ERROR  {spec.name:<22} HTTP-Fehler: {exc}", file=sys.stderr)
                failures.append(spec.name)
                continue

            data = resp.content
            if not _verify_sri(data, spec.sri):
                print(
                    f"  HASH   {spec.name:<22} SRI-Mismatch -- Datei verworfen!",
                    file=sys.stderr,
                )
                failures.append(spec.name)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            # Write atomically via tempfile + rename so a crash mid-write
            # never leaves a half-file that future SRI-checks would
            # misclassify as "valid file with wrong hash".
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            written.append(spec.name)
            print(f"  WRITE  {spec.name:<22} {target} ({len(data)} bytes)")

    print(
        f"\nntasker: assets fetch -- {len(written)} geschrieben, "
        f"{len(skipped)} übersprungen, {len(failures)} Fehler. "
        f"Cache: {target_root}"
    )
    return 1 if failures else 0


def cmd_assets_remove(args: argparse.Namespace) -> int:
    """Wipe the user-data vendor cache.

    Default: prompt for confirmation. ``--yes`` skips the prompt for
    scripts. Removes only files the manifest declares plus their parent
    directories if they end up empty -- never touches anything else.
    """
    import shutil  # noqa: PLC0415

    root = assets_dir()
    if not root.exists():
        print(f"ntasker: kein Asset-Cache vorhanden bei {root}.")
        return 0

    if not args.yes:
        try:
            answer = input(f"ntasker: Cache {root} wirklich löschen? [j/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"j", "ja", "y", "yes"}:
            print("ntasker: abgebrochen.")
            return 1

    # Whole-tree removal: we own the dir layout entirely (only files we
    # write live there) -- a recursive rmtree is correct and matches the
    # ``platformdirs`` user-data convention.
    shutil.rmtree(root)
    print(f"ntasker: Cache {root} entfernt.")
    return 0


def cmd_assets_status(args: argparse.Namespace) -> int:
    """Print per-asset status (present / SRI-ok / mode)."""
    from ntasker.settings import get_setting  # noqa: PLC0415

    raw_mode = get_setting("assets_mode", env_var="NTASKER_ASSETS_MODE")
    resolved = resolve_mode(raw_mode)

    rows: list[dict] = []
    for spec in MANIFEST:
        target = local_path_for(spec)
        present = target.is_file()
        sri_ok: bool | None
        if present:
            sri_ok = _verify_sri(target.read_bytes(), spec.sri)
        else:
            sri_ok = None
        rows.append(
            {
                "name": spec.name,
                "local_path": str(target),
                "present": present,
                "sri_ok": sri_ok,
                "cdn_url": spec.cdn_url,
                "sri": spec.sri,
            }
        )

    if args.json:
        _print_json(
            {
                "mode_setting": raw_mode or "(unset, default=auto)",
                "mode_resolved": resolved,
                "assets_dir": str(assets_dir()),
                "complete": local_assets_complete(),
                "assets": rows,
            }
        )
        return 0

    print(f"ntasker: assets_mode = {raw_mode or '(unset, default=auto)'} -> {resolved}")
    print(f"         assets_dir  = {assets_dir()}")
    print(f"         complete    = {local_assets_complete()}")
    print()
    print(f"  {'STATUS':<10} {'NAME':<24} {'PATH'}")
    print("  " + "-" * 78)
    for r in rows:
        if not r["present"]:
            status = "MISSING"
        elif r["sri_ok"] is False:
            status = "BAD-SRI"
        else:
            status = "OK"
        print(f"  {status:<10} {r['name']:<24} {r['local_path']}")
    return 0


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ntasker",
        description="Lightweight local task tracker. Single-user, FastAPI + SQLite.",
    )
    p.add_argument("--version", action="version", version=f"ntasker {__version__}")
    p.add_argument(
        "--db",
        metavar="PATH",
        help="DB-Pfad (ueberschreibt NTASKER_DB und Default).",
    )

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Schema anlegen / migrieren").set_defaults(func=cmd_init)

    sp_serve = sub.add_parser("serve", help="FastAPI-Server starten")
    sp_serve.add_argument("--host", default="127.0.0.1")
    sp_serve.add_argument("--port", type=int, default=8766)
    sp_serve.add_argument("--reload", action="store_true")
    sp_serve.set_defaults(func=cmd_serve)

    # list ----------------------------------------------------------------
    sp_list = sub.add_parser("list", help="Tasks listen")
    sp_list.add_argument("--project", action="append", default=[])
    sp_list.add_argument("--tag", action="append", default=[])
    sp_list.add_argument("--phase", action="append", default=[])
    sp_list.add_argument("--priority", action="append", default=[])
    sp_list.add_argument("--status", choices=["open", "done"])
    sp_list.add_argument(
        "--archived",
        type=lambda v: v.lower() in {"1", "true", "yes", "y"},
        default=None,
    )
    sp_list.add_argument("--search")
    sp_list.add_argument("--json", action="store_true")
    sp_list.set_defaults(func=cmd_list)

    # show ----------------------------------------------------------------
    sp_show = sub.add_parser("show", help="Task-Detail anzeigen")
    sp_show.add_argument("task_id", type=int)
    sp_show.add_argument("--json", action="store_true")
    sp_show.set_defaults(func=cmd_show)

    # add -----------------------------------------------------------------
    sp_add = sub.add_parser("add", help="Task anlegen")
    sp_add.add_argument("--title", required=True)
    sp_add.add_argument("--project")
    sp_add.add_argument("--description")
    sp_add.add_argument("--phase", choices=["wip", "planned", "later"])
    sp_add.add_argument("--priority", default="normal")
    sp_add.add_argument("--tag", action="append", default=[])
    sp_add.set_defaults(func=cmd_add)

    # done ----------------------------------------------------------------
    sp_done = sub.add_parser("done", help="Task auf done setzen")
    sp_done.add_argument("task_id", type=int)
    sp_done.set_defaults(func=cmd_done)

    # patch ---------------------------------------------------------------
    sp_patch = sub.add_parser("patch", help="Task-Felder aendern")
    sp_patch.add_argument("task_id", type=int)
    sp_patch.add_argument("--title")
    sp_patch.add_argument("--description")
    sp_patch.add_argument("--project")
    sp_patch.add_argument("--phase")
    sp_patch.add_argument("--priority")
    sp_patch.add_argument("--status")
    sp_patch.add_argument(
        "--archived",
        type=lambda v: v.lower() in {"1", "true", "yes", "y"},
        default=None,
    )
    sp_patch.set_defaults(func=cmd_patch)

    # tag-add / tag-rm ----------------------------------------------------
    sp_ta = sub.add_parser("tag-add", help="Tag hinzufuegen")
    sp_ta.add_argument("task_id", type=int)
    sp_ta.add_argument("tag")
    sp_ta.set_defaults(func=cmd_tag_add)

    sp_tr = sub.add_parser("tag-rm", help="Tag entfernen")
    sp_tr.add_argument("task_id", type=int)
    sp_tr.add_argument("tag")
    sp_tr.set_defaults(func=cmd_tag_rm)

    # stats ---------------------------------------------------------------
    sp_stats = sub.add_parser("stats", help="Counts (open/done/archive)")
    sp_stats.add_argument("--project", action="append", default=[])
    sp_stats.add_argument("--tag", action="append", default=[])
    sp_stats.add_argument("--phase", action="append", default=[])
    sp_stats.add_argument("--priority", action="append", default=[])
    sp_stats.add_argument("--search")
    sp_stats.add_argument("--json", action="store_true")
    sp_stats.set_defaults(func=cmd_stats)

    # config --------------------------------------------------------------
    sp_cfg = sub.add_parser("config", help="Settings (KV-Store)")
    cfg_sub = sp_cfg.add_subparsers(dest="config_cmd", required=True)

    cfg_list = cfg_sub.add_parser("list", help="Alle Settings")
    cfg_list.add_argument("--json", action="store_true")
    cfg_list.set_defaults(func=cmd_config_list)

    cfg_get = cfg_sub.add_parser("get", help="Einen Key lesen")
    cfg_get.add_argument("key")
    cfg_get.add_argument("--json", action="store_true")
    cfg_get.set_defaults(func=cmd_config_get)

    cfg_set = cfg_sub.add_parser("set", help="Einen Key setzen (mit Validator)")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg_set.set_defaults(func=cmd_config_set)

    cfg_unset = cfg_sub.add_parser("unset", help="Einen Key entfernen")
    cfg_unset.add_argument("key")
    cfg_unset.set_defaults(func=cmd_config_unset)

    # install-claude-assets ----------------------------------------------
    sp_ica = sub.add_parser(
        "install-claude-assets",
        help="Claude Code skill + slash command installieren / pruefen",
    )
    sp_ica.add_argument(
        "--command-name",
        default="task",
        help="Slash command file name (default: task -> /task <id>).",
    )
    sp_ica.add_argument(
        "--force",
        action="store_true",
        help="Overwrite divergent files (timestamped backup is created).",
    )
    sp_ica.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned actions without writing.",
    )
    sp_ica.add_argument(
        "--check",
        action="store_true",
        help="Read-only status check. Exit 0=identical, 1=drift, 2=not installed.",
    )
    sp_ica.add_argument(
        "--claude-home",
        default=None,
        help="Override ~/.claude (also via NTASKER_CLAUDE_HOME env var).",
    )
    sp_ica.set_defaults(func=cmd_install_claude_assets)

    # assets --------------------------------------------------------------
    sp_assets = sub.add_parser(
        "assets",
        help="Vendor-Assets verwalten (CDN-Default, Opt-in lokal).",
    )
    assets_sub = sp_assets.add_subparsers(dest="assets_cmd", required=True)

    sp_af = assets_sub.add_parser(
        "fetch",
        help="Vendor-Assets via HTTP in den User-Data-Cache laden (mit SRI-Verify).",
    )
    sp_af.add_argument(
        "--force",
        action="store_true",
        help="Vorhandene Files neu laden, auch wenn SRI bereits passt.",
    )
    sp_af.set_defaults(func=cmd_assets_fetch)

    sp_ar = assets_sub.add_parser("remove", help="User-Data-Asset-Cache loeschen.")
    sp_ar.add_argument(
        "--yes",
        action="store_true",
        help="Bestätigungs-Prompt überspringen.",
    )
    sp_ar.set_defaults(func=cmd_assets_remove)

    sp_as = assets_sub.add_parser("status", help="Cache-Zustand + Modus anzeigen.")
    sp_as.add_argument("--json", action="store_true")
    sp_as.set_defaults(func=cmd_assets_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # ``install-claude-assets`` and ``assets fetch|remove`` are
    # filesystem-only -- no DB involvement. ``assets status`` *does*
    # read the ``assets_mode`` setting and therefore goes through the
    # standard DB-init path below.
    if args.command == "install-claude-assets":
        return args.func(args)
    if args.command == "assets" and getattr(args, "assets_cmd", None) in {"fetch", "remove"}:
        return args.func(args)

    db_path = resolve_db_path(args.db)
    set_db_path(db_path)

    # Read-only commands get a friendly hint (no auto-init), the rest run
    # ``init_db()`` themselves so e.g. ``ntasker add`` against a fresh path
    # works without a separate ``ntasker init`` step.
    read_only = {"list", "show", "stats"}
    if args.command in read_only:
        warn_if_missing(db_path)
    else:
        # init / serve / write -- ensure schema exists. Idempotent.
        if not db_path.exists():
            print(f"ntasker: erstelle DB bei {db_path}", file=sys.stderr)
        init_db()

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
