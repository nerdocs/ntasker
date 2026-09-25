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
| ``completion``       | Shell completion script: print / install / remove  |

Global flags: ``--db <path>`` (highest precedence) and ``--version``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import sys
from datetime import datetime
from typing import Any, cast

from ntasker import __version__
from ntasker.assets import (
    MANIFEST,
    assets_dir,
    local_assets_complete,
    local_path_for,
    resolve_mode,
)
from ntasker import completion, plugins, projects
from ntasker.agents import AGENTS, agent_available, agent_keys, enabled_agents, resolve_home
from ntasker.claude_assets import (
    install_assets,
    permission_rule_state,
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
    report_fields,
    row_to_task,
    set_db_path,
    set_task_deps,
    set_task_tags,
    title_from_description,
    validate_deps,
)
from ntasker.i18n import _, resolve_for_cli, set_active_language
from ntasker.middleware import ALLOWED_HOSTS_ENV, LOOPBACK_HOSTS
from ntasker.paths import resolve_db_path, warn_if_missing
from ntasker import locks, taskqueue
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
    print(f"  {_('Agent'):<14}{t.get('agent') or _('(default)')}")
    print(f"  {_('Model'):<14}{t.get('model') or _('(agent default)')}")
    print(f"  {_('Tags'):<14}{', '.join(t.get('tags') or []) or '-'}")
    deps = t.get("depends") or []
    dep_str = ", ".join(f"#{d['id']}{'' if d['done'] else ' (open)'}" for d in deps) or "-"
    print(f"  {_('Depends on'):<14}{dep_str}")
    print(f"  {_('Archived'):<14}{bool(t.get('archived'))}")
    print(f"  {_('Draft'):<14}{bool(t.get('draft'))}")
    fasttrack = bool(t.get("fasttrack"))
    print(f"  {_('Fasttrack'):<14}{fasttrack}{' (fail-continue)' if fasttrack and t.get('fail_continue') else ''}")
    print(f"  {_('Created'):<14}{t.get('created_at') or '-'}")
    if t.get("completed_at"):
        print(f"  {_('Completed'):<14}{t['completed_at']}")
    context = t.get("context") or []
    if context:
        print()
        print(f"  --- {_('Attached context')} ---")
        for entry in context:
            missing = "" if entry.get("exists", True) else f"  [{_('missing')}]"
            print(f"  [{entry['kind']}] {entry['label']}{missing}")
            print(f"      {entry['path']}")
            if entry.get("note"):
                print(f"      {entry['note']}")
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
    sql = "SELECT tasks.* FROM tasks WHERE proposed = 0"
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


def _active_sessions(host: str, port: int, timeout: float = 1.0) -> list[int] | None:
    """Task ids with a live Claude session, via ``GET /api/claude/sessions``.

    ``active`` covers both *running* and *input-waiting* sessions -- either one
    would be killed by a restart (``KillMode=control-group``), so both must
    block it. ``None`` means the daemon could not be reached; the caller reads
    that as "nothing to protect" since a dead daemon has no child sessions.
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = f"http://{host}:{port}/api/claude/sessions"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            body = _json.loads(resp.read().decode("utf-8"))
            return list(body.get("active", []))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


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

    # The origin guard only trusts loopback hosts by default. Binding
    # elsewhere on purpose (``--host``) must not lock the operator out, so
    # propagate that host into the guard's allow-list -- via ENV, because
    # ``--reload`` runs the app in a subprocess that never sees `args`.
    if args.host not in LOOPBACK_HOSTS:
        os.environ[ALLOWED_HOSTS_ENV] = args.host

    # Spawned agent sessions inherit this so `ntasker hook ...` (Claude Code
    # hooks) can reach the server that started them, whatever it binds to.
    os.environ["NTASKER_URL"] = f"http://{args.host}:{args.port}"

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

    # Probe the raw port BEFORE /healthz, not after. Both probes open a
    # TCP connection, and a foreign listener that never accept()s -- the
    # exact case this diagnosis exists for -- holds each one in its
    # backlog. With a backlog of 1 (a plain `listen(1)`), the /healthz
    # attempt fills the queue and the follow-up probe then cannot connect,
    # so ntasker reported "no server running" about a port that was
    # visibly occupied. Asking the cheap question first keeps the answer
    # independent of how deep the other process's backlog happens to be.
    listening = _port_in_use(args.host, args.port)

    if not _healthz_ok(args.host, args.port):
        # /healthz silent: either the port is truly empty (nothing to
        # stop, exit 0) -- or something *is* there but does not speak
        # our protocol (pre-v1.4.0 ntasker, or a foreign process). In
        # the latter case we cannot POST /shutdown, so we tell the user
        # exactly that instead of a misleading "no server running".
        if listening:
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


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart the server so freshly deployed code goes live.

    A restart has to leave a *working* server behind, which rules out the naive
    stop-then-``serve``:

    * **Under a service manager**, stopping the daemon and starting our own
      foreground server hands the port to a process systemd knows nothing
      about. The unit then restarts into a port that is already taken, fails,
      and (``Restart=on-failure``) crash-loops forever while the squatter keeps
      serving the *old* code -- which looks exactly like "restart did nothing".
      So when a unit is installed, hand the job to the supervisor.
    * **Standalone**, a foreground server dies with the shell that started it.
      Restart therefore detaches by default, unlike ``serve``.

    ``--foreground`` opts out of both and does the literal stop-then-serve, for
    when you want the server in your terminal (``--reload`` implies it).

    An explicit ``--host`` / ``--port`` also skips the supervisor: it names one
    specific server, and the installed unit may well serve a different address.
    Restarting the unit would then leave the named address untouched while
    bouncing an unrelated daemon.
    """
    import time  # noqa: PLC0415

    from ntasker import service  # noqa: PLC0415

    # `restart` defaults host/port to None so "not given" stays distinguishable
    # from "given the default value" -- see the docstring.
    addressed = args.host is not None or args.port is not None
    args.host = args.host if args.host is not None else "127.0.0.1"
    args.port = args.port if args.port is not None else 8766

    foreground = getattr(args, "foreground", False) or args.reload

    if not foreground and not addressed and service.service_installed():
        if not service.restart_service():
            print(_("ntasker: could not restart the installed service."), file=sys.stderr)
            return 1
        # The supervisor restarts asynchronously; confirm it actually came back
        # rather than reporting success into a crash loop.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _healthz_ok(args.host, args.port, timeout=0.3):
                print(
                    _("ntasker: service restarted on {host}:{port}").format(
                        host=args.host, port=args.port
                    )
                )
                return 0
            time.sleep(0.2)
        print(
            _(
                "ntasker: the service was restarted but nothing answers on "
                "{host}:{port}. Check `ntasker service status`."
            ).format(host=args.host, port=args.port),
            file=sys.stderr,
        )
        return 1

    rc = cmd_stop(args)
    if rc != 0:
        return rc
    # Detached unless explicitly asked for a foreground server -- see above.
    args.detach = not foreground
    return cmd_serve(args)


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
        plugins.apply_task_hooks(conn, [task])
    if args.json:
        _print_json(task)
    else:
        _print_task_detail(task)
    return 0


def _parse_locks(raw: str | None) -> list[str]:
    """Split a comma-separated ``--locks`` value; ``None``/'' -> ``[]``."""
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


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
    if args.agent is not None and args.agent not in agent_keys():
        print(
            _("ntasker: invalid agent: {value!r}").format(value=args.agent),
            file=sys.stderr,
        )
        return 2
    # Title is optional -- fall back to the start of the description.
    title_value = (args.title or "").strip() or title_from_description(args.description)
    if not title_value:
        print(_("ntasker: need a --title or a --description"), file=sys.stderr)
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
            "INSERT INTO tasks (project, title, description, phase, priority, agent, model, "
            "locks, draft, fasttrack, fail_continue) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                args.project,
                title_value,
                args.description,
                phase_value,
                args.priority,
                args.agent,
                (args.model or "").strip() or None,
                locks.dump(locks.normalize(_parse_locks(args.locks), args.project)),
                1 if args.draft else 0,
                1 if args.fasttrack else 0,
                1 if args.fail_continue else 0,
            ),
        )
        # sqlite3 types lastrowid as ``int | None``; after a successful INSERT
        # on a rowid table it is always set, so narrow instead of coercing.
        new_id = cast(int, cur.lastrowid)
        if norm_tags:
            set_task_tags(conn, new_id, norm_tags)
        if dep_ids:
            set_task_deps(conn, new_id, dep_ids)
    print(_("#{id} created: {title}").format(id=new_id, title=title_value))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Store the agent's final report for a task (Markdown from --file or stdin).

    An empty text clears the report. Written directly to the DB like ``patch``
    -- a session spawned by ``ntasker serve`` inherits ``NTASKER_DB``, so the
    report lands in the server's database.
    """
    if args.file:
        try:
            text = pathlib.Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(_("ntasker: cannot read {path}: {err}").format(path=args.file, err=exc), file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()
    fields = report_fields(text)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET report = ?, report_at = ? WHERE id = ?",
            (fields["report"], fields["report_at"], args.task_id),
        )
        if cur.rowcount == 0:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
    if fields["report"]:
        print(_("#{id} report written").format(id=args.task_id))
    else:
        print(_("#{id} report cleared").format(id=args.task_id))
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
    if args.agent is not None:
        # Empty string clears the agent (-> default_agent at run time).
        candidate = args.agent.strip()
        if candidate and candidate not in agent_keys():
            print(
                _("ntasker: invalid agent: {value!r}").format(value=args.agent),
                file=sys.stderr,
            )
            return 2
        fields["agent"] = candidate or None
    if args.model is not None:
        # Empty string clears the model (-> the agent's <key>_model setting).
        fields["model"] = args.model.strip() or None
    if args.archived is not None:
        fields["archived"] = 1 if args.archived else 0
    if args.draft is not None:
        fields["draft"] = 1 if args.draft else 0
        if args.draft:   # a draft cannot stay queued (mirrors the API)
            fields["queue_order"] = None
            fields["session_ended_at"] = None
    if args.fasttrack is not None:
        fields["fasttrack"] = 1 if args.fasttrack else 0
    if args.fail_continue is not None:
        fields["fail_continue"] = 1 if args.fail_continue else 0
    if args.locks is not None:
        fields["locks"] = _parse_locks(args.locks)   # normalised below, once the project is known
    if args.report is not None:
        fields.update(report_fields(args.report))   # '' clears
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
            "SELECT project FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if exists is None:
            print(_("ntasker: task #{id} not found").format(id=args.task_id), file=sys.stderr)
            return 1
        if "locks" in fields:
            own = fields["project"] if "project" in fields else exists["project"]
            fields["locks"] = locks.dump(locks.normalize(fields["locks"], own))

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


def _set_plugin_enabled(name: str, enabled: bool) -> int:
    """``ntasker enable|disable <plugin>``: rewrite the matching plugin list.

    A default-on plugin lives in ``plugins_disabled``, an opt-in plugin in
    ``plugins_enabled`` -- the same two lists the /settings card writes.
    The validators reject unknown names and switching off the last agent.
    Enabling a plugin that declares an extra first installs the packages
    of that extra which are missing (like ``self-update``, into this
    interpreter's environment).
    """
    plugins.load_all()
    ctx = plugins.REGISTRY.get(name)
    if ctx is None:
        print(
            _("ntasker: unknown plugin {name!r}. Known: {known}").format(
                name=name, known=", ".join(plugins.REGISTRY)
            ),
            file=sys.stderr,
        )
        return 2
    if enabled and ctx.spec.extra:
        missing = plugins.missing_requirements(ctx.spec.extra)
        if missing:
            import subprocess  # noqa: PLC0415

            from ntasker import service  # noqa: PLC0415

            cmd = service.resolve_install_command(ctx.spec.extra, missing)
            print(_("ntasker: installing {pkgs} -- `{cmd}`").format(
                pkgs=", ".join(missing), cmd=" ".join(cmd)
            ))
            rc = subprocess.run(cmd).returncode  # noqa: S603 -- auto-detected installer
            if rc != 0:
                print(_("ntasker: install failed (exit {rc})").format(rc=rc), file=sys.stderr)
                return rc
            if service.install_needs_restart():
                print(_("ntasker: restart the service so the new packages take effect."))
    key = plugins.SETTING_DISABLED if ctx.spec.default_on else plugins.SETTING_ENABLED
    listed = enabled != ctx.spec.default_on
    row = get_setting_raw(key)
    names = set(json.loads(row["value"])) if row else set()
    names.discard(name)
    if listed:
        names.add(name)
    try:
        set_setting(key, json.dumps(sorted(names)))
    except ValueError as exc:
        print(f"ntasker: {exc}", file=sys.stderr)
        return 2
    state = _("enabled") if enabled else _("disabled")
    print(f"{name}: {state}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return _set_plugin_enabled(args.plugin, True)


def cmd_disable(args: argparse.Namespace) -> int:
    return _set_plugin_enabled(args.plugin, False)


# Task queue -----------------------------------------------------------------
# Thin wrappers around :mod:`ntasker.taskqueue` -- the same functions the web UI
# drives through /api/queue, so both surfaces stay in step. The CLI only edits
# the queue; the running server's worker is what actually starts the tasks.


def _queue_tasks() -> list[dict]:
    """The queue in run order, as full task dicts (tags + deps included)."""
    rows = taskqueue.load_queue()
    with get_conn() as conn:
        return [
            row_to_task(r, load_tags_for(conn, int(r["id"])), load_deps_for(conn, int(r["id"])))
            for r in rows
        ]


def _reject_unqueueable(task_ids: list[int]) -> int | None:
    """Print why an id cannot be queued and return an exit code, else ``None``.

    The API drops ineligible ids silently -- fine when a stale browser list is
    the source. A hand-typed id deserves to be told instead.
    """
    with get_conn() as conn:
        for tid in task_ids:
            row = conn.execute(
                "SELECT status, archived, draft, proposed FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            if row is None:
                print(_("ntasker: task #{id} not found").format(id=tid), file=sys.stderr)
                return 1
            if row["proposed"]:
                print(
                    _("ntasker: #{id} is an inbox proposal -- accept it first").format(id=tid),
                    file=sys.stderr,
                )
                return 2
            if row["draft"]:

                print(
                    _("ntasker: #{id} is a draft -- drafts are never started").format(id=tid),
                    file=sys.stderr,
                )
                return 2
            if row["archived"] or row["status"] != "open":
                print(
                    _("ntasker: #{id} is not open -- only open tasks can be queued").format(
                        id=tid
                    ),
                    file=sys.stderr,
                )
                return 2
    return None


def cmd_queue_list(args: argparse.Namespace) -> int:
    from ntasker.settings import get_queue_enabled  # noqa: PLC0415

    tasks = _queue_tasks()
    enabled = get_queue_enabled()
    if args.json:
        _print_json({"enabled": enabled, "items": tasks})
        return 0
    print(_("Queue: running") if enabled else _("Queue: paused"))
    if not tasks:
        print(_("(queue is empty)"))
        return 0
    for pos, t in enumerate(tasks, 1):
        proj = t.get("project") or _("(cross)")
        print(f"  {pos:>3}. #{t['id']:<5} {proj:<22} {_truncate(t['title'], 50)}")
    return 0


def cmd_queue_add(args: argparse.Namespace) -> int:
    """Queue tasks. An already-queued id is moved, not duplicated."""
    rc = _reject_unqueueable(args.task_id)
    if rc is not None:
        return rc
    rest = [i for i in (int(r["id"]) for r in taskqueue.load_queue()) if i not in args.task_id]
    ids = [*args.task_id, *rest] if args.top else [*rest, *args.task_id]
    taskqueue.set_queue(ids)
    if args.top:   # "run it again" for an entry whose session ended
        taskqueue.clear_ended(args.task_id)
    print(_("queued: {ids}").format(ids=", ".join(f"#{i}" for i in args.task_id)))
    return 0


def cmd_queue_rm(args: argparse.Namespace) -> int:
    queued = [int(r["id"]) for r in taskqueue.load_queue()]
    missing = [i for i in args.task_id if i not in queued]
    if missing:
        print(
            _("ntasker: not in the queue: {ids}").format(
                ids=", ".join(f"#{i}" for i in missing)
            ),
            file=sys.stderr,
        )
        return 1
    taskqueue.set_queue([i for i in queued if i not in args.task_id])
    print(_("removed from the queue: {ids}").format(ids=", ".join(f"#{i}" for i in args.task_id)))
    return 0


def cmd_queue_clear(args: argparse.Namespace) -> int:
    n = len(taskqueue.load_queue())
    taskqueue.set_queue([])
    print(_("queue cleared -- {n} task(s) removed").format(n=n))
    return 0


def _warn_if_no_server(host: str, port: int) -> None:
    """Point out that a started queue needs a running server to do anything.

    The switch lives in the DB, so ``queue start`` succeeds either way -- but
    without a server there is no worker, and the queue would sit there looking
    started while nothing happens.
    """
    if _healthz_ok(host, port):
        return
    print(
        _(
            "ntasker: note -- no server answering on {host}:{port}. The queue only "
            "runs while `ntasker serve` is up."
        ).format(host=host, port=port),
        file=sys.stderr,
    )


def cmd_queue_start(args: argparse.Namespace) -> int:
    set_setting("queue_enabled", "true")
    n = len(taskqueue.load_queue())
    print(_("queue started -- {n} task(s) queued").format(n=n))
    _warn_if_no_server(args.host, args.port)
    return 0


def cmd_queue_pause(args: argparse.Namespace) -> int:
    """Stop starting new tasks. A task already running keeps going."""
    set_setting("queue_enabled", "false")
    print(_("queue paused"))
    return 0


def cmd_queue_plan(args: argparse.Namespace) -> int:
    """Start the queue planner -- an agent session that orders the queue.

    Server-only: the planner is a live session, which only the running server
    can spawn. 1 when the server is unreachable or a planner is already up.
    """
    try:
        status, data = _api_call(_server_base(args), "POST", "/api/queue/plan", {})
    except OSError as exc:
        print(_("ntasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
        return 1
    if status >= 400:
        print(_("ntasker: {detail}").format(detail=data.get("detail", status)), file=sys.stderr)
        return 1
    print(_("queue planner started -- it orders the queue and switches it on"))
    return 0


# Run a task -------------------------------------------------------------------


def _lane_position(items: list[dict], task_id: int) -> int:
    """1-based run position of ``task_id`` within its own project lane.

    The queue runs one task per project, so the number that says whether a run
    starts now or waits is the position among the entries of *its* project --
    exactly the bead the queue panel draws.
    """
    me = next((t for t in items if int(t["id"]) == task_id), None)
    if me is None:
        return 0
    lane = [t for t in items if (t.get("project") or None) == (me.get("project") or None)]
    return lane.index(me) + 1


def cmd_run(args: argparse.Namespace) -> int:
    """Press the board's run button from a terminal.

    Same path as the UI (``POST /api/queue/run``): the task goes into its
    project's lane and the worker spawns its agent as soon as that lane is
    free. Server-only -- the worker lives in the running server, so without one
    nothing would ever start. Prints the run view's URL, which ``--open``
    opens.
    """
    rc = _reject_unqueueable(args.task_id)
    if rc is not None:
        return rc
    base = _server_base(args)
    paused = False
    for tid in args.task_id:
        try:
            status, data = _api_call(base, "POST", "/api/queue/run", {"id": tid})
        except OSError as exc:
            print(_("ntasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
            return 1
        if status >= 400:
            print(_("ntasker: {detail}").format(detail=data.get("detail", status)), file=sys.stderr)
            return 1
        print(
            _("#{id} queued -- {pos}. in its lane; watch it at {url}").format(
                id=tid,
                pos=_lane_position(data.get("items", []), tid),
                url=f"{base}/#/run/{tid}",
            )
        )
        paused = not data.get("enabled")
    if paused:   # nothing starts at all until the queue is running again
        print(
            _("ntasker: note -- the queue is paused; `ntasker queue start` runs it"),
            file=sys.stderr,
        )
    if args.open:
        import webbrowser  # noqa: PLC0415

        webbrowser.open(f"{base}/#/run/{args.task_id[0]}")
    return 0


# Directory locks --------------------------------------------------------------
# These go through the running server's API rather than the DB: a lock grant
# has to be checked against the *live* sessions, which only the server knows.


def _server_base(args: argparse.Namespace) -> str:
    """Base URL of the server: explicit ``--host/--port`` > ``NTASKER_URL`` > default.

    ``NTASKER_URL`` is what ``ntasker serve`` puts into the environment of every
    session it spawns, so an agent inside such a session reaches the right
    server without flags.
    """
    import os  # noqa: PLC0415

    host, port = getattr(args, "host", None), getattr(args, "port", None)
    if host is None and port is None and os.environ.get("NTASKER_URL"):
        return os.environ["NTASKER_URL"].rstrip("/")
    return f"http://{host or '127.0.0.1'}:{port or 8766}"


def _api_call(
    base: str, method: str, path: str, body: dict | None = None, timeout: float = 5.0
) -> tuple[int, dict]:
    """One JSON request against the server; ``(status, parsed body)``.

    Raises ``OSError`` (incl. ``URLError``) when the server is unreachable; a
    non-2xx answer is returned, not raised, so callers can print its detail.
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw}


def _print_locks(task: dict) -> None:
    own = task.get("project") or _("(cross)")
    extra = task.get("locks") or []
    print(_("#{id} holds: {own}{extra}").format(
        id=task["id"], own=own, extra=("".join(f", {p}" for p in extra))
    ))


def _lock_request(args: argparse.Namespace, method: str, path: str, body: dict | None) -> int:
    try:
        status, data = _api_call(_server_base(args), method, path, body)
    except OSError as exc:
        print(_("ntasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
        return 1
    if status >= 400:
        print(_("ntasker: {detail}").format(detail=data.get("detail", status)), file=sys.stderr)
        return 1
    _print_locks(data)
    return 0


def cmd_lock_add(args: argparse.Namespace) -> int:
    """Grant extra directory locks -- all or nothing, 1 when a dir is held."""
    return _lock_request(
        args, "POST", f"/api/tasks/{args.task_id}/locks", {"projects": args.project}
    )


def cmd_lock_rm(args: argparse.Namespace) -> int:
    rc = 0
    for project in args.project:
        rc = _lock_request(args, "DELETE", f"/api/tasks/{args.task_id}/locks/{project}", None) or rc
    return rc


def cmd_lock_list(args: argparse.Namespace) -> int:
    return _lock_request(args, "GET", f"/api/tasks/{args.task_id}", None)


def cmd_finish(args: argparse.Namespace) -> int:
    """Report the run's outcome -- the agent's one hand-off call.

    Posts to ``/api/tasks/{id}/outcome``; the server decides what follows
    (review hand-off, done, dequeue -- see the endpoint). The report comes from
    ``--file`` or, when stdin is not a terminal, from stdin. Server-only: an
    unreachable server is an error, not a fallback.
    """
    if args.file:
        try:
            report = pathlib.Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(_("ntasker: cannot read {path}: {err}").format(path=args.file, err=exc), file=sys.stderr)
            return 2
    else:
        report = "" if sys.stdin.isatty() else sys.stdin.read()
    body: dict[str, Any] = {
        "status": args.status,
        "summary": args.summary,
        "commit": args.commit,
        "report": report,
        "next_tasks": args.next or [],
    }
    if args.files is not None:
        body["files"] = _parse_locks(args.files)
    try:
        status, data = _api_call(_server_base(args), "POST", f"/api/tasks/{args.task_id}/outcome", body)
    except OSError as exc:
        print(_("ntasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
        return 1
    if status >= 400:
        print(_("ntasker: {detail}").format(detail=data.get("detail", status)), file=sys.stderr)
        return 1
    print(_("#{id} finished: {status}").format(id=args.task_id, status=args.status))
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    """Hand a session started outside ntasker over to a task.

    Typed inside the running terminal session: Claude Code exports its own
    session id and pid, so the only thing to say is *which* task it belongs to
    -- an existing ``<id>``, or ``--title`` for a task created on the spot.
    From then on the board can reopen this conversation. Server-only, like
    :func:`cmd_finish`: the task must be visible to the running server.
    """
    if (args.task_id is None) == (not args.title):
        print(_("ntasker: name a task id or pass --title, not both."), file=sys.stderr)
        return 2
    session = (args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not session:
        print(
            _(
                "ntasker: no session id -- run this inside a Claude Code session, "
                "or pass --session <uuid>."
            ),
            file=sys.stderr,
        )
        return 2
    cwd = os.path.abspath(os.path.expanduser(args.cwd)) if args.cwd else os.getcwd()
    base = _server_base(args)
    task_id = args.task_id
    if task_id is None:
        project = args.project if args.project is not None else projects.name_for_dir(cwd)
        try:
            status, data = _api_call(
                base,
                "POST",
                "/api/tasks",
                {"title": args.title, "project": project, "phase": "wip"},
            )
        except OSError as exc:
            print(_("ntasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
            return 1
        if status >= 400:
            print(_("ntasker: {detail}").format(detail=data.get("detail", status)), file=sys.stderr)
            return 1
        task_id = data["id"]
    body: dict[str, Any] = {"session_id": session, "cwd": cwd}
    # Only a session that is actually running holds the task -- a pid from
    # somewhere else would mark it busy forever.
    with contextlib.suppress(TypeError, ValueError):
        pid = int(os.environ.get("CLAUDE_PID") or 0)
        if pid > 0:
            body["pid"] = pid
    try:
        status, data = _api_call(base, "POST", f"/api/claude/sessions/{task_id}/adopt", body)
    except OSError as exc:
        print(_("ntasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
        return 1
    if status >= 400:
        print(_("ntasker: {detail}").format(detail=data.get("detail", status)), file=sys.stderr)
        return 1
    print(_("#{id} adopted this session -- resume it from the board.").format(id=task_id))
    return 0


# Claude Code hooks -------------------------------------------------------------
# Run *inside* an ntasker-spawned Claude Code session (wired via the
# ``--settings`` file, see claude_assets/hooks/*.json). They read the hook's
# JSON from stdin and find their task + server in ``NTASKER_TASK_ID`` /
# ``NTASKER_URL`` -- both set by the runner. A hook must never break the
# session: missing env, an unreachable server or a bad payload all exit 0
# silently. Only ``pretooluse`` ever blocks (exit 2 + a reason on stderr).


def _hook_context() -> tuple[int, str, dict] | None:
    """``(task_id, base_url, stdin payload)`` or ``None`` outside a spawned session."""
    import os  # noqa: PLC0415

    raw_id, base = os.environ.get("NTASKER_TASK_ID"), os.environ.get("NTASKER_URL")
    if not raw_id or not base:
        return None
    try:
        task_id = int(raw_id)
    except ValueError:
        return None
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    return task_id, base.rstrip("/"), payload if isinstance(payload, dict) else {}


def _hook_state(waiting: bool) -> int:
    ctx = _hook_context()
    if ctx is None:
        return 0
    task_id, base, _payload = ctx
    with contextlib.suppress(Exception):
        _api_call(base, "POST", f"/api/claude/sessions/{task_id}/state", {"waiting": waiting}, 2.0)
    return 0


def cmd_hook_waiting(args: argparse.Namespace) -> int:
    return _hook_state(True)


def cmd_hook_running(args: argparse.Namespace) -> int:
    return _hook_state(False)


def _pretooluse_target(payload: dict) -> str | None:
    """The path a tool call is about to write to, or ``None`` when it has none.

    Edit / Write / MultiEdit / NotebookEdit carry ``file_path`` /
    ``notebook_path``. For Bash only a ``cd <dir>`` is inspected (the shell
    then works in that directory); the dir is resolved against the hook's
    ``cwd``. A bare ``cd`` or ``cd -`` has no static target.
    """
    import os  # noqa: PLC0415
    import shlex  # noqa: PLC0415

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if path:
        return str(path)
    if payload.get("tool_name") != "Bash":
        return None
    command = (tool_input.get("command") or "").strip()
    if not command.startswith("cd "):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[1] in {"-", "~"} or tokens[1].startswith("-"):
        return None
    target = os.path.expanduser(tokens[1])
    return os.path.join(payload.get("cwd") or os.getcwd(), target)


def cmd_hook_session(args: argparse.Namespace) -> int:
    """Report the terminal session this hook fires in, so ntasker can pick it up.

    Wired into the *user's* Claude Code settings by the ``session_discovery``
    setting, which means it runs in every session -- including ones ntasker
    started itself (skipped: the server already tracks those) and while no
    server is running (a refused connection is not an error here). The pid
    comes from ``CLAUDE_PID``, the session id and directory from the hook
    payload, with the environment as a fallback.
    """
    if os.environ.get("NTASKER_TASK_ID"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session = str(payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
    try:
        pid = int(os.environ.get("CLAUDE_PID") or 0)
    except ValueError:
        pid = 0
    if not session or pid <= 0:
        return 0
    body = {"session_id": session, "pid": pid, "cwd": str(payload.get("cwd") or os.getcwd())}
    with contextlib.suppress(Exception):
        _api_call(_server_base(args), "POST", "/api/claude/sessions/live", body, 2.0)
    return 0


def cmd_hook_pretooluse(args: argparse.Namespace) -> int:
    """Refuse a write inside another project's directory the task does not hold.

    Asks ``GET /api/locks/check``; the server owns the resolution rules (see
    docs/directory-locks.md). Exit 2 blocks the tool call and Claude Code
    shows stderr to the agent; anything else -- no target, server down,
    allowed -- is exit 0.
    """
    from urllib.parse import urlencode  # noqa: PLC0415

    ctx = _hook_context()
    if ctx is None:
        return 0
    task_id, base, payload = ctx
    target = _pretooluse_target(payload)
    if not target:
        return 0
    try:
        status, data = _api_call(
            base, "GET", "/api/locks/check?" + urlencode({"task": task_id, "path": target}), None, 3.0
        )
    except Exception:  # noqa: BLE001 -- the server being down must not block the agent
        return 0
    if status != 200 or data.get("allowed", True):
        return 0
    project = data.get("project") or target
    print(
        f"{target} is not locked by task #{task_id} -- run `ntasker lock add {task_id} {project}` "
        f"if it is free, or leave it to a task in that project",
        file=sys.stderr,
    )
    return 2


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


# Agent integration assets (skill + /task slash command) --------------------


def _note_missing_permission_rule(spec) -> None:
    """Point at the permission switch when Claude Code would still stop and ask
    for every ntasker command.

    Deliberately only a note: the rule goes into the user's own settings file,
    so it stays opt-in (``ntasker config set claude_permissions on``) exactly
    like the session hook does.
    """
    if spec.key != "claude" or permission_rule_state()["installed"]:
        return
    print(
        _(
            "ntasker: Claude Code still asks before every ntasker command. "
            "Run `ntasker config set claude_permissions on` to allow them."
        )
    )


def _do_agent_install(
    spec,
    command_name_raw: str,
    home_override: str | None,
    *,
    check: bool,
    force: bool,
    dry_run: bool,
) -> int:
    """Shared install/check logic for one agent's ``/task`` skill + command."""
    try:
        command_name = validate_command_name(command_name_raw)
    except ValueError as exc:
        print(f"ntasker: {exc}", file=sys.stderr)
        return 2
    try:
        home = resolve_home(spec, home_override)
    except Exception as exc:  # noqa: BLE001
        print(f"ntasker: invalid agent home: {exc}", file=sys.stderr)
        return 2

    if check:
        status = scan_status(spec, home, command_name=command_name)
        for fs in status.files:
            marker = "MISSING" if not fs.installed else ("DRIFT" if fs.drift else "OK")
            print(f"  {marker:<8} {fs.label:<8} {fs.path}")
        if not status.installed:
            print(_("ntasker: {agent} assets not installed.").format(agent=spec.label))
            return 2
        if status.drift:
            print(
                _(
                    "ntasker: {agent} assets installed but out of date. "
                    "Run `ntasker agent install {key} --force` to update."
                ).format(agent=spec.label, key=spec.key)
            )
            return 1
        print(_("ntasker: {agent} assets up to date.").format(agent=spec.label))
        return 0

    result = install_assets(
        spec, home, command_name=command_name, force=force, dry_run=dry_run
    )

    written = [a for a in result.actions if a.action == "write"]
    backed = [a for a in result.actions if a.action == "backup-and-write"]
    skipped = [a for a in result.actions if a.action == "skip"]
    blocked = [a for a in result.actions if a.action == "blocked"]

    prefix = "[dry-run] " if result.dry_run else ""
    for a in written:
        print(f"  {prefix}WRITE   {a.label:<8} {a.path}")
    for a in backed:
        print(f"  {prefix}BACKUP  {a.label:<8} {a.path}  ->  {a.backup_path}")
    for a in skipped:
        print(f"  {prefix}SKIP    {a.label:<8} {a.path}  ({a.reason})")
    for a in blocked:
        print(f"  {prefix}BLOCKED {a.label:<8} {a.path}  ({a.reason})", file=sys.stderr)

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
        print(_("ntasker: would install to {path}").format(path=home))
    else:
        print(_("ntasker: installed to {path}").format(path=home))
        _note_missing_permission_rule(spec)
    return 0


def cmd_agent_install(args: argparse.Namespace) -> int:
    """Install / check one agent's ``/task`` skill + slash command.

    Modes: ``--check`` (read-only; 0=identical, 1=drift, 2=not installed),
    ``--dry-run`` (print actions, no writes), default (write missing, skip
    identical, abort on drift unless ``--force`` -> backup + overwrite).
    """
    key = args.agent
    if key not in AGENTS:
        print(
            _("ntasker: unknown agent {key!r}. Known: {keys}").format(
                key=key, keys=", ".join(AGENTS)
            ),
            file=sys.stderr,
        )
        return 2
    if not plugins.is_enabled(key):
        print(_("ntasker: plugin {name!r} is disabled.").format(name=key), file=sys.stderr)
        return 2
    return _do_agent_install(
        AGENTS[key],
        args.command_name,
        args.home,
        check=args.check,
        force=args.force,
        dry_run=args.dry_run,
    )


def cmd_agent_list(args: argparse.Namespace) -> int:
    """List the known agents with CLI availability + ``/task`` install status."""
    rows: list[dict] = []
    for spec in enabled_agents():
        try:
            home = resolve_home(spec)
            status = scan_status(spec, home)
            installed, drift = status.installed, status.drift
        except Exception:  # noqa: BLE001
            home, installed, drift = spec.default_home, False, False
        rows.append(
            {
                "key": spec.key,
                "label": spec.label,
                "binary": spec.binary,
                "available": agent_available(spec),
                "installed": installed,
                "drift": drift,
                "home": str(home),
            }
        )
    if getattr(args, "json", False):
        _print_json(rows)
        return 0
    print(f"  {'AGENT':<10} {'CLI':<6} {'INTEGRATION':<14} {'HOME'}")
    print("  " + "-" * 70)
    for r in rows:
        cli = "ok" if r["available"] else "-"
        integ = "drift" if r["drift"] else ("installed" if r["installed"] else "-")
        print(f"  {r['key']:<10} {cli:<6} {integ:<14} {r['home']}")
    return 0


def cmd_install_claude_assets(args: argparse.Namespace) -> int:
    """Deprecated alias for ``ntasker agent install claude``."""
    return _do_agent_install(
        AGENTS["claude"],
        args.command_name,
        args.claude_home,
        check=args.check,
        force=args.force,
        dry_run=args.dry_run,
    )


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


def cmd_service_start(args: argparse.Namespace) -> int:
    """Start the installed OS service."""
    from ntasker import service  # noqa: PLC0415

    if service.start_service():
        print(_("ntasker: service started."))
        return 0
    print(
        _("ntasker: no service installed -- run `ntasker service install` first."),
        file=sys.stderr,
    )
    return 1


def cmd_service_stop(args: argparse.Namespace) -> int:
    """Stop the installed OS service."""
    from ntasker import service  # noqa: PLC0415

    if service.stop_service():
        print(_("ntasker: service stopped."))
        return 0
    print(
        _("ntasker: no service installed -- run `ntasker service install` first."),
        file=sys.stderr,
    )
    return 1


def cmd_service_restart(args: argparse.Namespace) -> int:
    """Restart the installed service so a freshly deployed version goes live.

    Meant as the post-deploy hook: the daemon keeps the old code in memory until
    its process is replaced. Guarded against killing work in progress -- the unit
    uses ``KillMode=control-group``, so a restart tears down every child in the
    cgroup, including running (or input-waiting) ``claude`` task sessions. Unless
    ``--force`` is given, the restart is *deferred* (exit 2, no restart) while any
    such session is live, so a deploy never aborts a task mid-run.
    """
    from ntasker import service  # noqa: PLC0415

    if not service.service_installed():
        print(
            _("ntasker: no service installed -- run `ntasker service install` first."),
            file=sys.stderr,
        )
        return 1

    if not args.force:
        active = _active_sessions(args.host, args.port)
        if active:
            ids = ", ".join(f"#{t}" for t in sorted(active))
            print(
                _(
                    "ntasker: restart deferred -- {n} task session(s) still running "
                    "({ids}); a restart would kill them. Retry later or pass --force."
                ).format(n=len(active), ids=ids),
                file=sys.stderr,
            )
            return 2

    service.restart_service()
    print(_("ntasker: service restarted."))
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


def cmd_completion(args: argparse.Namespace) -> int:
    """Print the shell completion script, or install / remove it."""
    if args.install:
        rc = completion.install(args.shell)
        print(_("Completion installed -- open a new shell or run: source {rc}").format(rc=rc))
    elif args.uninstall:
        if completion.uninstall(args.shell):
            print(_("Completion removed."))
        else:
            print(_("Completion was not installed."))
    else:
        sys.stdout.write(completion.render(args.shell))
    return 0


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

    # Plugins contribute agent choices and subcommands. The parser knows every
    # built-in (the DB is not bound yet, so enablement cannot be read here);
    # a disabled plugin's command refuses at run time instead.
    plugins.load_all()
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help=_("Create / migrate the schema")).set_defaults(func=cmd_init)

    detach_help = _(
        "Start the server as a detached background process. "
        "Idempotent: returns 0 if a server is already answering /healthz."
    )

    sp_serve = sub.add_parser(
        "serve", aliases=["start"], help=_("Run the FastAPI server")
    )
    sp_serve.add_argument("--host", default="127.0.0.1")
    sp_serve.add_argument("--port", type=int, default=8766)
    sp_serve.add_argument("--reload", action="store_true")
    sp_serve.add_argument("--detach", action="store_true", help=detach_help)
    sp_serve.set_defaults(func=cmd_serve)

    # stop ----------------------------------------------------------------
    sp_stop = sub.add_parser(
        "stop",
        help=_("Ask a running ntasker server to shut down (POST /shutdown)."),
    )
    sp_stop.add_argument("--host", default="127.0.0.1")
    sp_stop.add_argument("--port", type=int, default=8766)
    sp_stop.set_defaults(func=cmd_stop)

    # restart --------------------------------------------------------------
    sp_restart = sub.add_parser(
        "restart",
        help=_("Stop a running ntasker server, then start it again."),
    )
    # Defaults stay None on purpose: cmd_restart needs to tell "no address
    # given" (restart whatever serves ntasker, service manager included) from
    # "this exact address" (never touch the service). It fills them in itself.
    sp_restart.add_argument("--host", default=None)
    sp_restart.add_argument("--port", type=int, default=None)
    sp_restart.add_argument("--reload", action="store_true")
    sp_restart.add_argument(
        "--foreground",
        action="store_true",
        help=_(
            "Bypass the service manager and run the new server in this terminal "
            "instead of detaching it. Implied by --reload."
        ),
    )
    # Detaching is what restart does anyway now; kept accepted (and hidden) so
    # scripts written against v2.21 do not start failing on an unknown flag.
    sp_restart.add_argument("--detach", action="store_true", help=argparse.SUPPRESS)
    sp_restart.set_defaults(func=cmd_restart)

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
    sp_add.add_argument("--title", help=_("Optional; defaults to the start of --description"))
    sp_add.add_argument("--project")
    sp_add.add_argument("--description")
    sp_add.add_argument("--phase", choices=["planned", "wip", "review"])
    sp_add.add_argument("--priority", default="normal")
    sp_add.add_argument(
        "--agent",
        choices=list(AGENTS),
        help=_("AI coding agent for this task (default: the default_agent setting)."),
    )
    sp_add.add_argument(
        "--model",
        help=_("Model for this task's sessions, e.g. opus (default: the agent's model setting)."),
    )
    sp_add.add_argument("--tag", action="append", default=[])
    sp_add.add_argument(
        "--depends",
        help=_("Comma-separated task ids this task depends on, e.g. 12,15."),
    )
    sp_add.add_argument(
        "--locks",
        help=_("Comma-separated extra projects whose directories the run holds."),
    )
    sp_add.add_argument(
        "--draft", action="store_true", help=_("Park as a draft: never started by anyone.")
    )
    sp_add.add_argument(
        "--fasttrack", action="store_true",
        help=_("Fasttrack: the agent commits and finishes the task itself."),
    )
    sp_add.add_argument(
        "--fail-continue", action="store_true",
        help=_("With --fasttrack: a failed run leaves the queue instead of blocking it."),
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
    sp_patch.add_argument(
        "--agent",
        help=_("Set the task's agent (claude/opencode/pi; '' clears to default)."),
    )
    sp_patch.add_argument(
        "--model",
        help=_("Set the task's model, e.g. opus ('' clears to the agent's model setting)."),
    )
    sp_patch.add_argument("--status")
    sp_patch.add_argument(
        "--archived",
        type=lambda v: v.lower() in {"1", "true", "yes", "y"},
        default=None,
    )
    sp_patch.add_argument(
        "--draft",
        type=lambda v: v.lower() in {"1", "true", "yes", "y"},
        default=None,
        help=_("true parks the task as a draft (and drops it from the queue); false releases it."),
    )
    sp_patch.add_argument(
        "--fasttrack", action=argparse.BooleanOptionalAction, default=None,
        help=_("Fasttrack: the agent commits and finishes the task itself."),
    )
    sp_patch.add_argument(
        "--fail-continue", action=argparse.BooleanOptionalAction, default=None,
        help=_("With fasttrack: a failed run leaves the queue instead of blocking it."),
    )
    sp_patch.add_argument(
        "--depends",
        help=_("Comma-separated task ids to depend on (replaces the set; '' clears)."),
    )
    sp_patch.add_argument(
        "--locks",
        help=_("Comma-separated extra projects to lock (replaces the set; '' clears)."),
    )
    sp_patch.add_argument("--report", help=_("The agent's final report, Markdown ('' clears)."))
    sp_patch.set_defaults(func=cmd_patch)

    # report --------------------------------------------------------------
    sp_report = sub.add_parser("report", help=_("Store the agent's final report for a task"))
    sp_report.add_argument("task_id", type=_task_id)
    sp_report.add_argument("--file", help=_("Read the Markdown from this file instead of stdin."))
    sp_report.set_defaults(func=cmd_report)

    # finish --------------------------------------------------------------
    sp_finish = sub.add_parser("finish", help=_("Report the run's outcome to the server"))
    sp_finish.add_argument("task_id", type=_task_id)
    sp_finish.add_argument("--status", choices=["ok", "failed", "blocked"], required=True)
    sp_finish.add_argument("--summary", help=_("One line; defaults to the report's first line."))
    sp_finish.add_argument("--commit", help=_("The commit's sha, if you committed."))
    sp_finish.add_argument("--file", help=_("Read the report from this file instead of stdin."))
    sp_finish.add_argument(
        "--files", help=_("Comma-separated changed paths (default: derived from the run's diff).")
    )
    sp_finish.add_argument(
        "--next", action="append", dest="next",
        help=_("A follow-up suggestion for the user (repeatable); never creates a task."),
    )
    # Server to talk to; default NTASKER_URL (set inside spawned sessions).
    sp_finish.add_argument("--host", default=None)
    sp_finish.add_argument("--port", type=int, default=None)
    sp_finish.set_defaults(func=cmd_finish)

    # adopt ---------------------------------------------------------------
    sp_adopt = sub.add_parser(
        "adopt", help=_("Attach the session you are in to a task, so it can be resumed")
    )
    sp_adopt.add_argument("task_id", type=_task_id, nargs="?", default=None)
    sp_adopt.add_argument("--title", help=_("Create a task with this title instead."))
    sp_adopt.add_argument(
        "--project", help=_("Project for --title (default: derived from the directory).")
    )
    sp_adopt.add_argument("--session", help=_("Session id (default: the session you are in)."))
    sp_adopt.add_argument("--cwd", help=_("Directory the session runs in (default: here)."))
    sp_adopt.add_argument("--host", default=None)
    sp_adopt.add_argument("--port", type=int, default=None)
    sp_adopt.set_defaults(func=cmd_adopt)

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

    # enable / disable ----------------------------------------------------
    sp_enable = sub.add_parser("enable", help=_("Switch a plugin on (e.g. voice)"))
    sp_enable.add_argument("plugin", metavar="PLUGIN")
    sp_enable.set_defaults(func=cmd_enable)

    sp_disable = sub.add_parser("disable", help=_("Switch a plugin off"))
    sp_disable.add_argument("plugin", metavar="PLUGIN")
    sp_disable.set_defaults(func=cmd_disable)

    # queue ---------------------------------------------------------------
    sp_q = sub.add_parser("queue", help=_("Auto-run task queue"))
    q_sub = sp_q.add_subparsers(dest="queue_cmd", required=True)

    q_list = q_sub.add_parser("list", help=_("Show the queue in run order"))
    q_list.add_argument("--json", action="store_true")
    q_list.set_defaults(func=cmd_queue_list)

    q_add = q_sub.add_parser("add", help=_("Put tasks in the queue"))
    q_add.add_argument("task_id", type=_task_id, nargs="+")
    q_add.add_argument(
        "--top", action="store_true", help=_("Insert at the front instead of the end")
    )
    q_add.set_defaults(func=cmd_queue_add)

    q_rm = q_sub.add_parser("rm", help=_("Take tasks out of the queue"))
    q_rm.add_argument("task_id", type=_task_id, nargs="+")
    q_rm.set_defaults(func=cmd_queue_rm)

    q_clear = q_sub.add_parser("clear", help=_("Empty the queue"))
    q_clear.set_defaults(func=cmd_queue_clear)

    q_start = q_sub.add_parser("start", help=_("Let the queue work through its tasks"))
    # Only used to warn when nothing is listening -- mirrors `serve`'s defaults
    # so the hint stays accurate on a non-default bind.
    q_start.add_argument("--host", default="127.0.0.1")
    q_start.add_argument("--port", type=int, default=8766)
    q_start.set_defaults(func=cmd_queue_start)

    q_pause = q_sub.add_parser("pause", help=_("Stop starting new tasks"))
    q_pause.set_defaults(func=cmd_queue_pause)

    q_plan = q_sub.add_parser(
        "plan", help=_("Let an agent order the queue and start it")
    )
    # Server to talk to; default NTASKER_URL (set inside spawned sessions).
    q_plan.add_argument("--host", default=None)
    q_plan.add_argument("--port", type=int, default=None)
    q_plan.set_defaults(func=cmd_queue_plan)

    # run -------------------------------------------------------------------
    sp_run = sub.add_parser(
        "run", help=_("Start a task in the UI -- the board's run button, from a terminal")
    )
    sp_run.add_argument("task_id", type=_task_id, nargs="+")
    sp_run.add_argument(
        "--open", action="store_true", help=_("Open the run view in a browser")
    )
    # Server to talk to; default NTASKER_URL (set inside spawned sessions).
    sp_run.add_argument("--host", default=None)
    sp_run.add_argument("--port", type=int, default=None)
    sp_run.set_defaults(func=cmd_run)

    # hook ----------------------------------------------------------------
    # Claude Code hook entry points; not meant to be typed by hand.
    sp_hook = sub.add_parser("hook", help=_("Claude Code hook handlers (used by spawned sessions)"))
    hook_sub = sp_hook.add_subparsers(dest="hook_cmd", required=True)
    for name, func in (
        ("waiting", cmd_hook_waiting),
        ("running", cmd_hook_running),
        ("pretooluse", cmd_hook_pretooluse),
        ("session", cmd_hook_session),
    ):
        hook_sub.add_parser(name).set_defaults(func=func)

    # lock ----------------------------------------------------------------
    sp_lock = sub.add_parser("lock", help=_("Directory locks a task's run holds"))
    lock_sub = sp_lock.add_subparsers(dest="lock_cmd", required=True)
    for name, func, with_projects, help_text in (
        ("add", cmd_lock_add, True, _("Lock more projects' directories for a task")),
        ("rm", cmd_lock_rm, True, _("Release directory locks")),
        ("list", cmd_lock_list, False, _("Show the directories a task holds")),
    ):
        lp = lock_sub.add_parser(name, help=help_text)
        lp.add_argument("task_id", type=_task_id)
        if with_projects:
            lp.add_argument("project", nargs="+")
        # Server to talk to; default NTASKER_URL (set inside spawned sessions).
        lp.add_argument("--host", default=None)
        lp.add_argument("--port", type=int, default=None)
        lp.set_defaults(func=func)

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

    # agent ---------------------------------------------------------------
    sp_agent = sub.add_parser(
        "agent",
        help=_("Manage AI coding agents (list, install /task integration)."),
    )
    agent_sub = sp_agent.add_subparsers(dest="agent_cmd", required=True)

    ag_list = agent_sub.add_parser(
        "list", help=_("List agents with CLI availability + integration status.")
    )
    ag_list.add_argument("--json", action="store_true")
    ag_list.set_defaults(func=cmd_agent_list)

    ag_install = agent_sub.add_parser(
        "install",
        help=_("Install / check an agent's skill + /task slash command."),
    )
    ag_install.add_argument("agent", choices=list(AGENTS))
    ag_install.add_argument(
        "--command-name",
        default="task",
        help=_("Slash command file name (default: task -> /task <id>)."),
    )
    ag_install.add_argument(
        "--force",
        action="store_true",
        help=_("Overwrite divergent files (timestamped backup is created)."),
    )
    ag_install.add_argument(
        "--dry-run", action="store_true", help=_("Show planned actions without writing.")
    )
    ag_install.add_argument(
        "--check",
        action="store_true",
        help=_("Read-only status check. Exit 0=identical, 1=drift, 2=not installed."),
    )
    ag_install.add_argument(
        "--home",
        default=None,
        help=_("Override the agent's config home (also via its *_DIR/*_HOME env var)."),
    )
    ag_install.set_defaults(func=cmd_agent_install)

    # install-claude-assets (deprecated alias of `agent install claude`) ---
    sp_ica = sub.add_parser(
        "install-claude-assets",
        help=_("Deprecated: use `ntasker agent install claude`."),
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

    svc_start = svc_sub.add_parser("start", help=_("Start the installed service."))
    svc_start.set_defaults(func=cmd_service_start)

    svc_stop = svc_sub.add_parser("stop", help=_("Stop the running service."))
    svc_stop.set_defaults(func=cmd_service_stop)

    svc_restart = svc_sub.add_parser(
        "restart",
        help=_("Restart the service to pick up a deployed version (deploy hook)."),
    )
    svc_restart.add_argument("--host", default="127.0.0.1")
    svc_restart.add_argument("--port", type=int, default=8766)
    svc_restart.add_argument(
        "--force",
        action="store_true",
        help=_("Restart even while task sessions run (they get killed)."),
    )
    svc_restart.set_defaults(func=cmd_service_restart)

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

    # completion ----------------------------------------------------------
    sp_comp = sub.add_parser(
        "completion",
        help=_("Print or install the shell completion script (bash, zsh)."),
    )
    sp_comp.add_argument("shell", choices=list(completion.SHELLS))
    comp_mode = sp_comp.add_mutually_exclusive_group()
    comp_mode.add_argument(
        "--install",
        action="store_true",
        help=_("Write the script to the user-data dir and source it from the shell's rc file."),
    )
    comp_mode.add_argument(
        "--uninstall", action="store_true", help=_("Remove the script and the rc-file line.")
    )
    sp_comp.set_defaults(func=cmd_completion)

    for _ctx in plugins.REGISTRY.values():
        for _add in _ctx.cli:
            _add(sub)

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
    if args.command in {"install-claude-assets", "agent"}:
        # Pin the active language for this run before any string is
        # printed -- ``resolve_for_cli`` falls back to env vars when no
        # DB is reachable yet (and these subcommands intentionally avoid
        # *creating* one).
        # ``agent list``/``install`` read settings (e.g. the ``<key>_bin``
        # binary-path overrides), so bind an *existing* DB -- but never
        # create one just to inspect agents.
        if args.command == "agent":
            db_path = resolve_db_path(args.db)
            if db_path.exists():
                set_db_path(db_path)
        set_active_language(resolve_for_cli())
        return args.func(args)
    if args.command == "assets" and getattr(args, "assets_cmd", None) in {"fetch", "remove"}:
        set_active_language(resolve_for_cli())
        return args.func(args)
    # `stop`, `lock`, `finish` and `hook` are pure HTTP requests to a running
    # server -- never create a DB for them (hooks fire inside every spawned session).
    if args.command in {"stop", "lock", "finish", "hook"}:
        set_active_language(resolve_for_cli())
        return args.func(args)
    # `service` (install/uninstall/status), `self-update` and `completion`
    # manage OS units, package upgrades and shell rc files -- never touch or
    # create the task DB.
    if args.command in {"service", "self-update", "completion"}:
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
