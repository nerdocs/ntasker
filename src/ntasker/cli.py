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


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ntasker",
        description="Lightweight local task tracker for the nerdocs HQ.",
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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
