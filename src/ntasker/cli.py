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
    DepError,
    get_conn,
    init_db,
    load_deps_for,
    load_tags_for,
    normalize_dep_ids,
    normalize_tags,
    row_to_task,
    set_db_path,
    set_task_deps,
    set_task_tags,
    validate_deps,
)
from ntasker.i18n import _, resolve_for_cli, set_active_language
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
        print(_("(no tasks)"))
        return
    # Headers -- column titles go through _() so they translate per locale.
    h_id = _("ID")
    h_stat = _("STAT")
    h_pr = _("PR")
    h_ph = _("PH")
    h_proj = _("PROJECT")
    h_title = _("TITLE")
    print(f"{h_id:>5} {h_stat:<6} {h_pr:<3} {h_ph:<8} {h_proj:<22} {h_title}")
    print("-" * 80)
    for t in tasks:
        prio_short = {"critical": "!!!", "high": "!!", "normal": "·", "low": ".."}.get(
            t.get("priority") or "normal", "·"
        )
        ph = (t.get("phase") or "-")[:8]
        proj = (t.get("project") or _("(cross)"))[:22]
        title = _truncate(t.get("title") or "", 60)
        print(f"{t['id']:>5} {t['status']:<6} {prio_short:<3} {ph:<8} {proj:<22} {title}")


def _print_task_detail(t: dict) -> None:
    print(f"#{t['id']} {t['title']}")
    print(f"  {_('Project'):<14}{t.get('project') or _('(cross-project)')}")
    print(f"  {_('Status'):<14}{t['status']}")
    print(f"  {_('Phase'):<14}{t.get('phase') or '-'}")
    print(f"  {_('Priority'):<14}{t.get('priority') or 'normal'}")
    print(f"  {_('Tags'):<14}{', '.join(t.get('tags') or []) or '-'}")
    deps = t.get("depends") or []
    dep_str = ", ".join(f"#{d['id']}{'' if d['done'] else ' (open)'}" for d in deps) or "-"
    print(f"  {_('Depends on'):<14}{dep_str}")
    print(f"  {_('Archived'):<14}{bool(t.get('archived'))}")
    print(f"  {_('Created'):<14}{t.get('created_at') or '-'}")
    if t.get("completed_at"):
        print(f"  {_('Completed'):<14}{t['completed_at']}")
    if t.get("description"):
        print()
        print(f"  --- {_('Description')} ---")
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
        valid = {"planned", "wip", "review"}
        names = [p for p in args.phase if p in valid]
        if names:
            placeholders = ", ".join("?" for _ in names)
            sql += f" AND phase IN ({placeholders})"
            params.extend(names)

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
        # Mirror the API: substring match on title / description, plus an
        # exact id match when the search string (with optional leading
        # `#`) is purely digits. So `ntasker list --search 240` and
        # `--search '#240'` both surface task #240.
        clauses = ["title LIKE ?", "COALESCE(description, '') LIKE ?"]
        like = f"%{args.search}%"
        params.extend([like, like])
        candidate = args.search.lstrip("#").strip()
        if candidate.isdigit():
            clauses.append("tasks.id = ?")
            params.append(int(candidate))
        sql += " AND (" + " OR ".join(clauses) + ")"

    sql += " ORDER BY archived ASC, status ASC, created_at DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for r in rows:
            tid = int(r["id"])
            tags = load_tags_for(conn, tid)
            depends = load_deps_for(conn, tid)
            out.append(row_to_task(r, tags, depends))
    return out


def _parse_depends(raw: str | None) -> list[int] | None:
    """Parse a comma-separated ``--depends`` value into a list of task ids.

    ``None`` (flag omitted) -> ``None`` (leave unchanged). Empty string ->
    ``[]`` (clear all). Accepts an optional leading ``#`` per id. Raises
    ``ValueError`` on a non-numeric token.
    """
    if raw is None:
        return None
    out: list[int] = []
    for part in raw.split(","):
        token = part.strip().lstrip("#").strip()
        if token:
            out.append(int(token))  # ValueError bubbles up to the caller
    return out


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create the schema at the active DB path."""
    from ntasker import db as _db  # noqa: PLC0415  -- read module-level DB_PATH

    init_db()
    print(_("ntasker: DB initialised at {path}").format(path=_db.DB_PATH))
    return 0


def _port_in_use(host: str, port: int, timeout: float = 0.2) -> bool:
    """Return True iff *something* is listening on ``host:port``.

    Used by ``ntasker stop`` to distinguish "no server at all" from
    "server is there but doesn't speak our /healthz/shutdown protocol"
    (e.g. a pre-v1.4.0 ntasker, or a third-party process squatting the
    port). Uses a connect-probe rather than a bind-probe so a non-root
    user on a privileged port still gets a meaningful answer.
    """
    import socket  # noqa: PLC0415

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
        except (OSError, TimeoutError):
            return False
        return True


def _healthz_ok(host: str, port: int, timeout: float = 0.5) -> bool:
    """Best-effort liveness probe against ``GET /healthz``.

    Uses stdlib ``urllib`` so it stays dependency-free (httpx is a runtime
    dep but importing it costs ~20ms on cold start -- the detach path
    runs this in a tight loop). Any error (connection refused, timeout,
    non-200, unparseable JSON) counts as "not up yet".
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return False
            body = _json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _spawn_detached_server(host: str, port: int, db_path: str | None) -> int:
    """Start ``ntasker serve`` as a detached background child, cross-platform.

    POSIX: ``start_new_session=True`` -- the child becomes its own session
    leader so a closing terminal does not SIGHUP it.

    Windows: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` -- the child
    is decoupled from the console and Ctrl-C in the parent does not reach
    it.

    Returns the child PID.
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    cmd = [sys.executable, "-m", "ntasker"]
    if db_path is not None:
        cmd.extend(["--db", db_path])
    cmd.extend(["serve", "--host", host, "--port", str(port)])

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # Windows: combine flags to fully detach from the parent console.
        creation_flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = creation_flags
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 -- args list is internal
    return proc.pid


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI app via uvicorn. Bind hardcoded to 127.0.0.1.

    Importing uvicorn lazily keeps ``ntasker --version`` fast and lets the
    CLI work in environments where uvicorn is missing (read-only ops).

    With ``--reload`` uvicorn spawns a worker subprocess that imports
    ``ntasker.app:app`` directly -- ``main()`` does not run there, so the
    module-level ``DB_PATH`` would be unbound. We propagate the resolved
    path via ``NTASKER_DB`` so the worker re-resolves to the same file.
    The app's startup hook re-resolves on its own (lifespan-safe), but
    we still pin the ENV here so an explicit ``--db`` actually reaches
    the reload worker.

    With ``--detach``: probe ``/healthz`` first; if a server is already
    answering on the same host/port, exit 0 (idempotent). Otherwise spawn
    a detached child (OS-specific) and poll ``/healthz`` until it answers
    or the deadline elapses.
    """
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    from ntasker import db as _db  # noqa: PLC0415  -- read module-level DB_PATH

    if _db.DB_PATH is not None:
        os.environ["NTASKER_DB"] = str(_db.DB_PATH)

    if getattr(args, "detach", False):
        if args.reload:
            print(
                _("ntasker: --detach and --reload are mutually exclusive."),
                file=sys.stderr,
            )
            return 2
        if _healthz_ok(args.host, args.port):
            print(
                _("ntasker: server already running on {host}:{port}").format(
                    host=args.host, port=args.port
                )
            )
            return 0
        db_path = str(_db.DB_PATH) if _db.DB_PATH is not None else None
        pid = _spawn_detached_server(args.host, args.port, db_path)
        # Poll up to ~3s; first-boot init_db on a fresh DB can take a moment.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _healthz_ok(args.host, args.port):
                print(
                    _("ntasker: started detached on {host}:{port} (pid {pid})").format(
                        host=args.host, port=args.port, pid=pid
                    )
                )
                return 0
            time.sleep(0.1)
        print(
            _(
                "ntasker: detached child (pid {pid}) did not answer /healthz "
                "within 3s on {host}:{port}"
            ).format(pid=pid, host=args.host, port=args.port),
            file=sys.stderr,
        )
        return 1

    try:
        import uvicorn  # noqa: PLC0415  -- lazy import on purpose
    except ImportError:
        print(_("ntasker: uvicorn not installed. `pip install ntasker[serve]`"), file=sys.stderr)
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


def cmd_stop(args: argparse.Namespace) -> int:
    """Ask a running ``ntasker serve`` to shut itself down via ``POST /shutdown``.

    Idempotent: if no server is reachable, exit 0 with a friendly note --
    "stop" on something that is already stopped is not a failure. Once the
    request is sent, poll ``/healthz`` briefly and only return success
    when the server has actually disappeared (max ~3s).
    """
    import time  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    base = f"http://{args.host}:{args.port}"

    if not _healthz_ok(args.host, args.port):
        # /healthz silent: either the port is truly empty (nothing to
        # stop, exit 0) -- or something *is* there but does not speak
        # our protocol (pre-v1.4.0 ntasker, or a foreign process). In
        # the latter case we cannot POST /shutdown, so we tell the user
        # exactly that instead of a misleading "no server running".
        if _port_in_use(args.host, args.port):
            print(
                _(
                    "ntasker: something is listening on {host}:{port} but does not "
                    "answer /healthz -- probably a pre-v1.4.0 ntasker or a foreign "
                    "process. Kill it manually (POSIX: `pkill -f 'ntasker serve'`)."
                ).format(host=args.host, port=args.port),
                file=sys.stderr,
            )
            return 1
        print(
            _("ntasker: no server running on {host}:{port}").format(
                host=args.host, port=args.port
            )
        )
        return 0

    try:
        req = urllib.request.Request(f"{base}/shutdown", method="POST")
        urllib.request.urlopen(req, timeout=2.0)  # noqa: S310
    except urllib.error.URLError as exc:
        # Connection may legitimately drop *during* the response -- the
        # server is killing itself, after all. Treat connection-reset as
        # "shutdown initiated"; anything else as a real error.
        msg = str(exc).lower()
        if "connection" not in msg and "reset" not in msg:
            print(
                _("ntasker: shutdown request failed: {exc}").format(exc=exc),
                file=sys.stderr,
            )
            return 1

    # Wait for the server to actually go away. Avoids races where the
    # caller immediately starts a new instance and trips a port collision.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _healthz_ok(args.host, args.port, timeout=0.2):
            print(
                _("ntasker: server on {host}:{port} stopped.").format(
                    host=args.host, port=args.port
                )
            )
            return 0
        time.sleep(0.1)

    print(
        _("ntasker: server on {host}:{port} still answering after 3s -- giving up.").format(
            host=args.host, port=args.port
        ),
        file=sys.stderr,
    )
    return 1


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
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
        tags = load_tags_for(conn, args.task_id)
        depends = load_deps_for(conn, args.task_id)
    task = row_to_task(row, tags, depends)
    if args.json:
        _print_json(task)
    else:
        _print_task_detail(task)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    if args.priority not in {"critical", "high", "normal", "low"}:
        print(
            _("ntasker: invalid priority: {value!r}").format(value=args.priority),
            file=sys.stderr,
        )
        return 2
    if args.phase is not None and args.phase not in {"planned", "wip", "review"}:
        print(
            _("ntasker: invalid phase: {value!r}").format(value=args.phase),
            file=sys.stderr,
        )
        return 2
    # phase column is NOT NULL since v2.0 -- default to ``planned`` when the
    # caller omits ``--phase`` so ``ntasker add --title X`` keeps working.
    phase_value = args.phase or "planned"
    norm_tags = normalize_tags(args.tag or [])
    try:
        dep_ids = normalize_dep_ids(_parse_depends(args.depends) or [])
    except ValueError:
        print(
            _("ntasker: invalid --depends value: {value!r}").format(value=args.depends),
            file=sys.stderr,
        )
        return 2
    with get_conn() as conn:
        # Check dependency targets exist *before* inserting, so a bad
        # reference aborts cleanly without leaving an orphan task. A brand-new
        # task has no incoming edges, so it cannot be part of a cycle yet.
        for d in dep_ids:
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (d,)).fetchone() is None:
                print(
                    _("ntasker: dependency task #{id} does not exist").format(id=d),
                    file=sys.stderr,
                )
                return 2
        cur = conn.execute(
            "INSERT INTO tasks (project, title, description, phase, priority) "
            "VALUES (?, ?, ?, ?, ?)",
            (args.project, args.title, args.description, phase_value, args.priority),
        )
        new_id = int(cur.lastrowid)
        if norm_tags:
            set_task_tags(conn, new_id, norm_tags)
        if dep_ids:
            set_task_deps(conn, new_id, dep_ids)
    print(_("#{id} created: {title}").format(id=new_id, title=args.title))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Hard-delete a task. Asks for confirmation unless ``--yes`` is given.

    There is no archived-only gate at the CLI level: hitting ``ntasker
    delete <id>`` is already a deliberate, typed-out action. The
    confirmation prompt is the safety net.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT title, archived FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if row is None:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
    title = row["title"]
    archived = bool(row["archived"])

    if not args.yes:
        prompt = _("ntasker: delete #{id} {title!r} (archived={archived})? [y/N] ").format(
            id=args.task_id, title=title, archived=archived
        )
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"j", "ja", "y", "yes"}:
            print(_("ntasker: aborted."))
            return 1

    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (args.task_id,))
        if cur.rowcount == 0:
            # Race: task disappeared between the existence check and the
            # delete. Treat as success-no-op rather than a hard error.
            print(_("ntasker: task #{id} already gone").format(id=args.task_id))
            return 0
    print(_("#{id} deleted").format(id=args.task_id))
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (now, args.task_id),
        )
        if cur.rowcount == 0:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
    print(_("#{id} -> done").format(id=args.task_id))
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
        candidate = args.phase.strip()
        if candidate == "":
            # Empty string used to mean "clear phase"; with phase NOT NULL
            # we route it to the canonical default instead.
            fields["phase"] = "planned"
        elif candidate not in {"planned", "wip", "review"}:
            print(
                _("ntasker: invalid phase: {value!r}").format(value=args.phase),
                file=sys.stderr,
            )
            return 2
        else:
            fields["phase"] = candidate
    if args.priority is not None:
        if args.priority not in {"critical", "high", "normal", "low"}:
            print(
                _("ntasker: invalid priority: {value!r}").format(value=args.priority),
                file=sys.stderr,
            )
            return 2
        fields["priority"] = args.priority
    if args.archived is not None:
        fields["archived"] = 1 if args.archived else 0
    if args.status is not None:
        if args.status not in {"open", "done"}:
            print(
                _("ntasker: invalid status: {value!r}").format(value=args.status),
                file=sys.stderr,
            )
            return 2
        fields["status"] = args.status
        fields["completed_at"] = (
            datetime.now().isoformat(timespec="seconds") if args.status == "done" else None
        )

    try:
        dep_ids = _parse_depends(args.depends)
    except ValueError:
        print(
            _("ntasker: invalid --depends value: {value!r}").format(value=args.depends),
            file=sys.stderr,
        )
        return 2

    if not fields and dep_ids is None:
        print(_("ntasker: nothing to change (specify at least one field)"), file=sys.stderr)
        return 2

    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if exists is None:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1

        if dep_ids is not None:
            dep_ids = normalize_dep_ids(dep_ids)
            try:
                validate_deps(conn, args.task_id, dep_ids)
            except DepError as e:
                if e.reason == "self":
                    msg = _("ntasker: a task cannot depend on itself")
                elif e.reason == "missing":
                    msg = _("ntasker: dependency task #{id} does not exist").format(id=e.ref)
                else:
                    msg = _("ntasker: that dependency would create a cycle (via #{id})").format(
                        id=e.ref
                    )
                print(msg, file=sys.stderr)
                return 2

        changed: list[str] = list(fields.keys())
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?",
                [*fields.values(), args.task_id],
            )
        if dep_ids is not None:
            set_task_deps(conn, args.task_id, dep_ids)
            changed.append("depends")
    print(
        _("#{id} updated ({fields})").format(
            id=args.task_id, fields=", ".join(changed)
        )
    )
    return 0


def cmd_tag_add(args: argparse.Namespace) -> int:
    norm = normalize_tags([args.tag])
    if not norm:
        print(_("ntasker: empty tag name: {value!r}").format(value=args.tag), file=sys.stderr)
        return 2
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if exists is None:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
        current = load_tags_for(conn, args.task_id)
        merged = list(dict.fromkeys([*current, *norm]))
        set_task_tags(conn, args.task_id, merged)
    print(_("#{id} tag +{tag}").format(id=args.task_id, tag=norm[0]))
    return 0


def cmd_tag_rm(args: argparse.Namespace) -> int:
    target = args.tag.strip().lower()
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if exists is None:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
        current = load_tags_for(conn, args.task_id)
        if target not in current:
            print(
                _("ntasker: tag {tag!r} not present on #{id}").format(
                    tag=target, id=args.task_id
                ),
                file=sys.stderr,
            )
            return 1
        new_tags = [t for t in current if t != target]
        set_task_tags(conn, args.task_id, new_tags)
    print(_("#{id} tag -{tag}").format(id=args.task_id, tag=target))
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
        print(_("(no settings configured)"))
        return 0
    for r in rows:
        print(f"  {r['key']:<24} {r['value']}    ({r['updated_at']})")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    row = get_setting_raw(args.key)
    if row is None:
        print(_("ntasker: setting {key!r} not set").format(key=args.key), file=sys.stderr)
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
        print(_("ntasker: setting {key!r} was not set").format(key=args.key), file=sys.stderr)
        return 1
    print(_("{key} removed").format(key=args.key))
    return 0


# Claude Code projects -------------------------------------------------------


def _match_key(name: str) -> str:
    """Fold a project basename for tolerant matching (case + ``_``/``-``)."""
    base = name.rsplit("/", 1)[-1]
    return base.casefold().replace("_", "-")


def cmd_projects_list(args: argparse.Namespace) -> int:
    from ntasker.projects import discover_claude_projects  # noqa: PLC0415

    names = discover_claude_projects()
    if args.json:
        _print_json(names)
        return 0
    if not names:
        print(_("(no Claude projects found)"))
        return 0
    for n in names:
        print(f"  {n}")
    return 0


def cmd_projects_migrate(args: argparse.Namespace) -> int:
    """Rename existing task projects to their Claude-project path form.

    Matches a task's current project value against the basename of every
    discovered Claude project (case-insensitive, ``_``/``-`` folded) and
    rewrites it to the canonical ``~``-relative path name. Free-form names
    with no Claude counterpart are left untouched.
    """
    from ntasker.projects import discover_claude_projects  # noqa: PLC0415

    discovered = discover_claude_projects()
    # match-key -> canonical name; drop ambiguous collisions.
    canonical: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name in discovered:
        key = _match_key(name)
        if key in canonical and canonical[key] != name:
            ambiguous.add(key)
        canonical[key] = name
    for key in ambiguous:
        canonical.pop(key, None)

    known = set(discovered)
    plan: list[tuple[str, str, int]] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT project AS p, COUNT(*) AS c FROM tasks "
            "WHERE project IS NOT NULL GROUP BY project"
        ).fetchall()
        for row in rows:
            old = row["p"]
            if old in known:
                continue  # already canonical
            new = canonical.get(_match_key(old))
            if new and new != old:
                plan.append((old, new, int(row["c"])))

        if not plan:
            print(_("Nothing to migrate -- task projects already match."))
            return 0

        for old, new, count in plan:
            print(f"  {old!r:<28} -> {new!r:<32} ({count})")

        if args.dry_run:
            print(_("Dry run -- no changes written."))
            return 0

        for old, new, _count in plan:
            conn.execute("UPDATE tasks SET project = ? WHERE project = ?", (new, old))
        conn.commit()
    print(_("Migrated {n} project name(s).").format(n=len(plan)))
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
            print(_("ntasker: Claude assets not installed."))
            return 2
        if status.drift:
            print(
                _(
                    "ntasker: Claude assets installed but out of date. "
                    "Run `ntasker install-claude-assets --force` to update."
                )
            )
            return 1
        print(_("ntasker: Claude assets up to date."))
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
            _(
                "ntasker: aborted -- one or more files differ. "
                "Pass --force to overwrite (with timestamped backup)."
            ),
            file=sys.stderr,
        )
        return 3
    if result.dry_run:
        print(_("ntasker: would install to {path}").format(path=claude_home))
    else:
        print(_("ntasker: installed to {path}").format(path=claude_home))
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
            _("ntasker: httpx missing. Reinstall ntasker (httpx is a runtime dep)."),
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
                print(
                    _("  ERROR  {name:<22} HTTP error: {exc}").format(
                        name=spec.name, exc=exc
                    ),
                    file=sys.stderr,
                )
                failures.append(spec.name)
                continue

            data = resp.content
            if not _verify_sri(data, spec.sri):
                print(
                    _("  HASH   {name:<22} SRI mismatch -- file discarded!").format(
                        name=spec.name
                    ),
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
        "\n"
        + _(
            "ntasker: assets fetch -- {written} written, {skipped} skipped, "
            "{failures} errors. Cache: {cache}"
        ).format(
            written=len(written),
            skipped=len(skipped),
            failures=len(failures),
            cache=target_root,
        )
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
        print(_("ntasker: no asset cache at {path}.").format(path=root))
        return 0

    if not args.yes:
        try:
            answer = (
                input(_("ntasker: really delete cache {path}? [y/N] ").format(path=root))
                .strip()
                .lower()
            )
        except EOFError:
            answer = ""
        if answer not in {"j", "ja", "y", "yes"}:
            print(_("ntasker: aborted."))
            return 1

    # Whole-tree removal: we own the dir layout entirely (only files we
    # write live there) -- a recursive rmtree is correct and matches the
    # ``platformdirs`` user-data convention.
    shutil.rmtree(root)
    print(_("ntasker: cache {path} removed.").format(path=root))
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
# Service integration + self-update
# ---------------------------------------------------------------------------


def cmd_self_update(args: argparse.Namespace) -> int:
    """Upgrade the ntasker package from PyPI, then restart the service.

    The package-upgrade command is the ``update_command`` setting, or an
    auto-detected default (``uv tool upgrade`` / ``pip install -U``). On a
    successful upgrade and unless ``--no-restart`` is given, the supervised
    service is restarted so the new code takes effect for the running daemon.
    """
    import subprocess  # noqa: PLC0415

    from ntasker import service  # noqa: PLC0415
    from ntasker.settings import get_setting  # noqa: PLC0415

    # Bind the DB path so the configured ``update_command`` can be read, but
    # only if the DB already exists -- self-update must never *create* one.
    db_path = resolve_db_path(args.db)
    update_command = None
    if db_path.exists():
        set_db_path(db_path)
        update_command = get_setting("update_command")

    cmd = service.resolve_update_command(update_command)
    print(_("ntasker: running `{cmd}`").format(cmd=" ".join(cmd)))
    proc = subprocess.run(cmd)  # noqa: S603 -- user-configured / auto-detected
    if proc.returncode != 0:
        print(_("ntasker: update command failed (exit {rc})").format(rc=proc.returncode))
        return proc.returncode

    # Report the now-installed version by asking the freshly upgraded CLI.
    ver = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ntasker", "--version"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(_("ntasker: now at {ver} (was {old})").format(ver=ver or "?", old=f"ntasker {__version__}"))

    if not getattr(args, "no_restart", False):
        if service.restart_service():
            print(_("ntasker: service restarted."))
    return 0


def cmd_service_install(args: argparse.Namespace) -> int:
    """Install + enable the OS service (and optional auto-update timer)."""
    import os  # noqa: PLC0415

    from ntasker import service  # noqa: PLC0415

    # Embed ``--db`` in the unit only when explicitly chosen -- otherwise let
    # the daemon resolve the platform default at runtime (cleaner unit file).
    db_path = args.db or os.environ.get("NTASKER_DB")
    try:
        log = service.install(args.host, args.port, db_path, args.auto_update)
    except RuntimeError as exc:
        print(f"ntasker: {exc}", file=sys.stderr)
        return 2
    for line in log:
        print(f"ntasker: {line}")
    return 0


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    """Disable + remove all ntasker units."""
    from ntasker import service  # noqa: PLC0415

    try:
        log = service.uninstall()
    except RuntimeError as exc:
        print(f"ntasker: {exc}", file=sys.stderr)
        return 2
    for line in log:
        print(f"ntasker: {line}")
    return 0


def cmd_service_status(args: argparse.Namespace) -> int:
    """Print the install/active state of the ntasker units."""
    from ntasker import service  # noqa: PLC0415

    for line in service.status():
        print(f"ntasker: {line}")
    return 0


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def _task_id(value: str) -> int:
    """Parse a task id, tolerating a leading ``#``.

    Task ids are shown to humans as ``#311`` everywhere (UI, ``show``
    output, copy-to-clipboard), so users and agents naturally pass that
    form back -- e.g. ``ntasker patch #311``. Strip an optional leading
    ``#`` (and surrounding whitespace) before the int conversion so the
    decorated form is accepted instead of failing with "invalid int
    value".
    """
    return int(value.strip().lstrip("#"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ntasker",
        description=_("Lightweight local task tracker. Single-user, FastAPI + SQLite."),
    )
    p.add_argument("--version", action="version", version=f"ntasker {__version__}")
    p.add_argument(
        "--db",
        metavar="PATH",
        help=_("DB path (overrides NTASKER_DB and the default)."),
    )

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help=_("Create / migrate the schema")).set_defaults(func=cmd_init)

    sp_serve = sub.add_parser("serve", help=_("Run the FastAPI server"))
    sp_serve.add_argument("--host", default="127.0.0.1")
    sp_serve.add_argument("--port", type=int, default=8766)
    sp_serve.add_argument("--reload", action="store_true")
    sp_serve.add_argument(
        "--detach",
        action="store_true",
        help=_(
            "Start the server as a detached background process. "
            "Idempotent: returns 0 if a server is already answering /healthz."
        ),
    )
    sp_serve.set_defaults(func=cmd_serve)

    # stop ----------------------------------------------------------------
    sp_stop = sub.add_parser(
        "stop",
        help=_("Ask a running ntasker server to shut down (POST /shutdown)."),
    )
    sp_stop.add_argument("--host", default="127.0.0.1")
    sp_stop.add_argument("--port", type=int, default=8766)
    sp_stop.set_defaults(func=cmd_stop)

    # list ----------------------------------------------------------------
    sp_list = sub.add_parser("list", help=_("List tasks"))
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
    sp_show = sub.add_parser("show", help=_("Show task detail"))
    sp_show.add_argument("task_id", type=_task_id)
    sp_show.add_argument("--json", action="store_true")
    sp_show.set_defaults(func=cmd_show)

    # add -----------------------------------------------------------------
    sp_add = sub.add_parser("add", help=_("Create a task"))
    sp_add.add_argument("--title", required=True)
    sp_add.add_argument("--project")
    sp_add.add_argument("--description")
    sp_add.add_argument("--phase", choices=["planned", "wip", "review"])
    sp_add.add_argument("--priority", default="normal")
    sp_add.add_argument("--tag", action="append", default=[])
    sp_add.add_argument(
        "--depends",
        help=_("Comma-separated task ids this task depends on, e.g. 12,15."),
    )
    sp_add.set_defaults(func=cmd_add)

    # done ----------------------------------------------------------------
    sp_done = sub.add_parser("done", help=_("Mark a task as done"))
    sp_done.add_argument("task_id", type=_task_id)
    sp_done.set_defaults(func=cmd_done)

    # delete --------------------------------------------------------------
    # Hard delete from the CLI. Confirms unless --yes is passed; works
    # regardless of archived state -- the deliberate `ntasker delete <id>`
    # command itself is the safety mechanism, the prompt is the second.
    sp_del = sub.add_parser(
        "delete",
        help=_("Delete a task permanently (use with care)."),
    )
    sp_del.add_argument("task_id", type=_task_id)
    sp_del.add_argument(
        "--yes",
        action="store_true",
        help=_("Skip the confirmation prompt (for scripts)."),
    )
    sp_del.set_defaults(func=cmd_delete)

    # patch ---------------------------------------------------------------
    sp_patch = sub.add_parser("patch", help=_("Edit task fields"))
    sp_patch.add_argument("task_id", type=_task_id)
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
    sp_patch.add_argument(
        "--depends",
        help=_("Comma-separated task ids to depend on (replaces the set; '' clears)."),
    )
    sp_patch.set_defaults(func=cmd_patch)

    # tag-add / tag-rm ----------------------------------------------------
    sp_ta = sub.add_parser("tag-add", help=_("Add a tag"))
    sp_ta.add_argument("task_id", type=_task_id)
    sp_ta.add_argument("tag")
    sp_ta.set_defaults(func=cmd_tag_add)

    sp_tr = sub.add_parser("tag-rm", help=_("Remove a tag"))
    sp_tr.add_argument("task_id", type=_task_id)
    sp_tr.add_argument("tag")
    sp_tr.set_defaults(func=cmd_tag_rm)

    # stats ---------------------------------------------------------------
    sp_stats = sub.add_parser("stats", help=_("Counts (open/done/archive)"))
    sp_stats.add_argument("--project", action="append", default=[])
    sp_stats.add_argument("--tag", action="append", default=[])
    sp_stats.add_argument("--phase", action="append", default=[])
    sp_stats.add_argument("--priority", action="append", default=[])
    sp_stats.add_argument("--search")
    sp_stats.add_argument("--json", action="store_true")
    sp_stats.set_defaults(func=cmd_stats)

    # config --------------------------------------------------------------
    sp_cfg = sub.add_parser("config", help=_("Settings (KV store)"))
    cfg_sub = sp_cfg.add_subparsers(dest="config_cmd", required=True)

    cfg_list = cfg_sub.add_parser("list", help=_("List all settings"))
    cfg_list.add_argument("--json", action="store_true")
    cfg_list.set_defaults(func=cmd_config_list)

    cfg_get = cfg_sub.add_parser("get", help=_("Read one key"))
    cfg_get.add_argument("key")
    cfg_get.add_argument("--json", action="store_true")
    cfg_get.set_defaults(func=cmd_config_get)

    cfg_set = cfg_sub.add_parser("set", help=_("Write one key (validated)"))
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg_set.set_defaults(func=cmd_config_set)

    cfg_unset = cfg_sub.add_parser("unset", help=_("Remove one key"))
    cfg_unset.add_argument("key")
    cfg_unset.set_defaults(func=cmd_config_unset)

    # projects ------------------------------------------------------------
    sp_proj = sub.add_parser("projects", help=_("Claude Code projects"))
    proj_sub = sp_proj.add_subparsers(dest="projects_cmd", required=True)

    proj_list = proj_sub.add_parser("list", help=_("List discovered Claude projects"))
    proj_list.add_argument("--json", action="store_true")
    proj_list.set_defaults(func=cmd_projects_list)

    proj_migrate = proj_sub.add_parser(
        "migrate", help=_("Rename task projects to their Claude-project path form")
    )
    proj_migrate.add_argument(
        "--dry-run", action="store_true", help=_("Show planned renames without writing.")
    )
    proj_migrate.set_defaults(func=cmd_projects_migrate)

    # install-claude-assets ----------------------------------------------
    sp_ica = sub.add_parser(
        "install-claude-assets",
        help=_("Install / check the Claude Code skill + slash command"),
    )
    sp_ica.add_argument(
        "--command-name",
        default="task",
        help=_("Slash command file name (default: task -> /task <id>)."),
    )
    sp_ica.add_argument(
        "--force",
        action="store_true",
        help=_("Overwrite divergent files (timestamped backup is created)."),
    )
    sp_ica.add_argument(
        "--dry-run",
        action="store_true",
        help=_("Show planned actions without writing."),
    )
    sp_ica.add_argument(
        "--check",
        action="store_true",
        help=_("Read-only status check. Exit 0=identical, 1=drift, 2=not installed."),
    )
    sp_ica.add_argument(
        "--claude-home",
        default=None,
        help=_("Override ~/.claude (also via NTASKER_CLAUDE_HOME env var)."),
    )
    sp_ica.set_defaults(func=cmd_install_claude_assets)

    # assets --------------------------------------------------------------
    sp_assets = sub.add_parser(
        "assets",
        help=_("Manage vendor assets (CDN by default, opt-in local cache)."),
    )
    assets_sub = sp_assets.add_subparsers(dest="assets_cmd", required=True)

    sp_af = assets_sub.add_parser(
        "fetch",
        help=_("Fetch vendor assets via HTTP into the user-data cache (SRI-verified)."),
    )
    sp_af.add_argument(
        "--force",
        action="store_true",
        help=_("Re-fetch existing files even when their SRI already matches."),
    )
    sp_af.set_defaults(func=cmd_assets_fetch)

    sp_ar = assets_sub.add_parser("remove", help=_("Delete the user-data asset cache."))
    sp_ar.add_argument(
        "--yes",
        action="store_true",
        help=_("Skip the confirmation prompt."),
    )
    sp_ar.set_defaults(func=cmd_assets_remove)

    sp_as = assets_sub.add_parser("status", help=_("Show cache state + active mode."))
    sp_as.add_argument("--json", action="store_true")
    sp_as.set_defaults(func=cmd_assets_status)

    # service -------------------------------------------------------------
    sp_svc = sub.add_parser(
        "service",
        help=_("Install ntasker as an OS service (systemd / launchd)."),
    )
    svc_sub = sp_svc.add_subparsers(dest="service_cmd", required=True)

    svc_install = svc_sub.add_parser("install", help=_("Install + enable the service."))
    svc_install.add_argument("--host", default="127.0.0.1")
    svc_install.add_argument("--port", type=int, default=8766)
    svc_install.add_argument(
        "--auto-update",
        action="store_true",
        help=_("Also install a daily timer that runs `ntasker self-update`."),
    )
    svc_install.set_defaults(func=cmd_service_install)

    svc_uninstall = svc_sub.add_parser("uninstall", help=_("Disable + remove the service."))
    svc_uninstall.set_defaults(func=cmd_service_uninstall)

    svc_status = svc_sub.add_parser("status", help=_("Show service install / active state."))
    svc_status.set_defaults(func=cmd_service_status)

    # self-update ---------------------------------------------------------
    sp_su = sub.add_parser(
        "self-update",
        help=_("Upgrade ntasker from PyPI, then restart the service."),
    )
    sp_su.add_argument(
        "--no-restart",
        action="store_true",
        help=_("Upgrade only; do not restart the running service."),
    )
    sp_su.set_defaults(func=cmd_self_update)

    return p


def main(argv: list[str] | None = None) -> int:
    # Pin the active language BEFORE constructing the argparse tree --
    # help strings are translated at ``add_argument`` time. The DB is
    # not bound here yet, so the resolver falls through to LANG/env
    # (which is what --help / --version users want anyway).
    set_active_language(resolve_for_cli())

    parser = build_parser()
    args = parser.parse_args(argv)

    # ``install-claude-assets`` and ``assets fetch|remove`` are
    # filesystem-only -- no DB involvement. ``assets status`` *does*
    # read the ``assets_mode`` setting and therefore goes through the
    # standard DB-init path below.
    if args.command == "install-claude-assets":
        # Pin the active language for this run before any string is
        # printed -- ``resolve_for_cli`` falls back to env vars when no
        # DB is reachable yet (and these subcommands intentionally avoid
        # opening one).
        set_active_language(resolve_for_cli())
        return args.func(args)
    if args.command == "assets" and getattr(args, "assets_cmd", None) in {"fetch", "remove"}:
        set_active_language(resolve_for_cli())
        return args.func(args)
    # `stop` is a pure HTTP request to a running server -- never create
    # a DB just to send a shutdown.
    if args.command == "stop":
        set_active_language(resolve_for_cli())
        return args.func(args)
    # `service` (install/uninstall/status) and `self-update` manage OS units
    # and package upgrades -- never touch or create the task DB.
    if args.command in {"service", "self-update"}:
        set_active_language(resolve_for_cli())
        return args.func(args)

    db_path = resolve_db_path(args.db)
    set_db_path(db_path)

    # Now that the DB path is bound, the language setting can be read.
    # Pin it once for the entire process (CLI is sync and short-lived).
    set_active_language(resolve_for_cli())

    # Read-only commands get a friendly hint (no auto-init), the rest run
    # ``init_db()`` themselves so e.g. ``ntasker add`` against a fresh path
    # works without a separate ``ntasker init`` step.
    read_only = {"list", "show", "stats"}
    if args.command in read_only:
        warn_if_missing(db_path)
    else:
        # init / serve / write -- ensure schema exists. Idempotent.
        if not db_path.exists():
            print(_("ntasker: creating DB at {path}").format(path=db_path), file=sys.stderr)
        init_db()

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
