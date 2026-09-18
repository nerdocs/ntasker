"""Embed a real interactive ``claude`` session in the web UI over a WebSocket.

Spawns the actual ``claude`` CLI in a pseudo-terminal (PTY) and bridges it to a
terminal emulator (xterm.js) in the browser. The user gets the genuine Claude
Code TUI -- interactive prompts, permission dialogs, interrupt (Ctrl-C), and the
*identical* context (same ``~/.claude``, ``CLAUDE.md``, skills, MCP servers,
permissions) because it is the same binary they run from a shell.

Sessions are **persistent and reattachable**: the ``claude`` process lives in a
module-level registry keyed by task id and keeps running when the browser
detaches (navigates away or reloads). A reattaching client replays a bounded
output buffer to reconstruct the current screen, then streams live. The process
ends only when it exits on its own or the user stops it.

POSIX only: a PTY needs ``os.openpty`` + ``termios``. On a platform without
them the feature reports unavailable and the rest of ntasker is unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import shlex
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from ntasker.agents import AgentSpec, agent_available, get_spec, resolve_agent_key

try:  # POSIX-only PTY machinery
    import fcntl
    import struct
    import subprocess
    import termios

    _PTY_OK = True
except ImportError:  # pragma: no cover - non-POSIX
    _PTY_OK = False


# Cap on the per-session replay buffer. Large enough to hold a full TUI redraw
# (the alt-screen plus recent scrollback) so a reattaching client lands on the
# correct final screen; small enough to stay cheap.
BUFFER_LIMIT = 512 * 1024

# After a window resize the TUI repaints in one burst (SIGWINCH -> full redraw).
# That output is *not* progress -- it fires just from a client attaching and
# resizing the terminal. For this grace window we forward the redraw to the
# screen but do NOT treat it as activity, so a session parked at a prompt stays
# flagged "waiting" when the user only peeks at it. See :func:`session_states`.
RESIZE_REDRAW_GRACE = 1.5

def pty_available() -> tuple[bool, str | None]:
    """Whether the host can run *any* interactive agent session.

    Interactive runs need a POSIX PTY (``os.openpty`` + ``termios``). On a
    platform without them the whole feature is unavailable regardless of which
    agent binaries are installed.
    """
    if not _PTY_OK:
        return False, "interactive runs need a POSIX pseudo-terminal"
    return True, None


def terminal_available(spec: AgentSpec) -> tuple[bool, str | None]:
    """Return ``(available, reason_if_not)`` for one agent.

    Needs a POSIX PTY *and* the agent's CLI on PATH. Either missing -> the UI
    hides that agent's run button and surfaces the reason.
    """
    ok, reason = pty_available()
    if not ok:
        return ok, reason
    if not agent_available(spec):
        return False, (
            f"the `{spec.binary}` CLI was not found on PATH "
            f"(set `{spec.bin_setting_key}` to its full path if it is installed elsewhere)"
        )
    return True, None


class DraftTaskError(RuntimeError):
    """Raised by :func:`_start_session` for a draft task -- drafts never start."""


def _task_row(task_id: int):
    """The task's ``agent`` + ``project`` + ``draft`` columns, or ``None`` (missing / DB hiccup)."""
    from ntasker.db import get_conn  # noqa: PLC0415

    try:
        with get_conn() as conn:
            return conn.execute(
                "SELECT agent, project, draft FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
    except Exception:  # noqa: BLE001 -- a DB hiccup must not crash the spawn path
        return None


def _spec_for_task(task_id: int) -> AgentSpec:
    """Resolve the :class:`AgentSpec` for a task from its persisted ``agent``."""
    row = _task_row(task_id)
    task_agent = row["agent"] if row else None
    return get_spec(resolve_agent_key(task_agent))


def projects_base_dir() -> Path | None:
    """The configured projects base directory (expanded), or ``None``.

    Reads the ``projects_base`` setting (ENV ``NTASKER_PROJECTS_BASE`` first).
    Only this directory's subtree is eligible for auto-creating a new project
    directory on run -- see :func:`resolve_run_cwd`.
    """
    from ntasker.settings import get_setting  # noqa: PLC0415

    raw = get_setting("projects_base", env_var="NTASKER_PROJECTS_BASE")
    if not raw:
        return None
    return Path(os.path.abspath(os.path.expanduser(raw)))


def no_project_dir() -> str:
    """Start directory for a run without a usable project directory.

    Precedence: the ``no_project_dir`` setting (ENV ``NTASKER_NO_PROJECT_DIR``
    first) -> the configured ``projects_base`` -> the home directory. A
    configured path that does not exist is skipped rather than honoured -- the
    agent must always get a directory it can actually start in.

    The home directory is the last resort on purpose: Claude Code treats it as
    an untrusted workspace and blocks the session on its trust prompt, so a
    configured base is preferred whenever there is one.
    """
    from ntasker.settings import get_setting  # noqa: PLC0415

    raw = get_setting("no_project_dir", env_var="NTASKER_NO_PROJECT_DIR")
    for candidate in (raw, str(projects_base_dir() or "")):
        if not candidate:
            continue
        path = os.path.abspath(os.path.expanduser(candidate.strip()))
        if os.path.isdir(path):
            return path
    return os.path.expanduser("~")


def default_cwd_for_project(project: str | None) -> str | None:
    """Best-effort working directory for a task's ``project`` name.

    Inverse of :func:`ntasker.projects._path_to_name`: an absolute project name
    is used verbatim; a relative one is resolved under ``projects_base`` (or the
    home directory when unset). Only a *suggestion* -- the session shows its cwd
    in the TUI, and a wrong guess is one ``cd`` away.
    """
    if not project:
        return None
    p = Path(project).expanduser()
    if p.is_absolute():
        return str(p)
    base = projects_base_dir() or Path.home()
    return str(base / project)


def resolve_run_cwd(cwd: str | None) -> str:
    """Working directory for a run, creating a new project dir when warranted.

    Precedence:

    1. ``cwd`` if it already exists -> use it.
    2. ``cwd`` inside the configured ``projects_base`` -> create it (``mkdir
       -p``) and use it. This realises a "new project": the agent starts in a
       fresh directory inside the configured base.
    3. Otherwise :func:`no_project_dir` -- a best-effort fallback so the agent
       always starts. A path outside the base, or no ``projects_base``
       configured at all, is never silently created.
    """
    if not cwd:
        return no_project_dir()
    if os.path.isdir(cwd):
        return cwd
    base = projects_base_dir()
    if base is not None:
        try:
            target = Path(cwd).resolve()
            base_r = base.resolve()
            if target == base_r or base_r in target.parents:
                target.mkdir(parents=True, exist_ok=True)
                return str(target)
        except OSError:
            pass
    return no_project_dir()


def queue_seed_for_task(task: dict) -> str:
    """Initial input for a run started by the task queue -- i.e. for every run.

    The queue is the only path that starts a session, so every run gets this
    seed: the task data inlined into the prompt (no ``/task`` loader roundtrip
    -- one full inference pass and several thousand tokens saved, which matters
    on slow local models) plus the queue's hand-off rules. The user put the
    task in the queue to have it worked through unattended, so the seed grants
    the review hand-off without asking and tells the agent that the hand-off is
    what releases the next queued task. Closing stays the user's call. The
    ``phase=wip`` move the loader normally performs happens server-side at
    spawn instead (see :func:`mark_wip`). The ``/task`` command stays installed
    for manual terminal sessions.
    """
    from ntasker.db import get_conn, load_tags_for  # noqa: PLC0415 -- lazy: avoid cycle

    try:
        with get_conn() as conn:
            tags = load_tags_for(conn, int(task["id"]))
    except Exception:  # noqa: BLE001 -- tags are nice-to-have, never block a spawn
        tags = []

    facts = [f"Status: {task.get('status') or 'open'}"]
    if task.get("project"):
        facts.insert(0, f"Project: {task['project']}")
    facts.append(f"Priority: {task.get('priority') or 'normal'}")
    if tags:
        facts.append(f"Tags: {', '.join(tags)}")

    title = (task.get("title") or "").strip()
    head = f"# nTasker task #{task['id']}" + (f": {title}" if title else "")
    lines = [head, "", " | ".join(facts)]
    description = (task.get("description") or "").strip()
    if description:
        lines += ["", "## Description", "", description]
    from ntasker import plugins  # noqa: PLC0415 -- lazy: avoid cycle

    lines += plugins.run_briefings(int(task["id"]))
    tid = task["id"]
    lines += [
        "",
        "## Tracker rules (queued run)",
        "",
        "- The user put this task in nTasker's task queue to have it worked",
        "  through unattended. Carry it to completion if at all possible.",
        "- When the work is done, do two things in this order, without asking:",
        "  1. Write your final report (Markdown on stdin): what you did, what",
        "     you verified, what is open or left for the user:",
        f"       ntasker report {tid} <<'EOF'",
        "       ...",
        "       EOF",
        "  2. Hand the task over for review, then stop and wait:",
        f"       ntasker patch {tid} --phase review",
        "- This session stays open after the hand-off; the user reviews the",
        "  task here and closes it. The next queued task in this project only",
        "  starts once this task is done -- so never hand off on a guess.",
        "- If you cannot finish (blocker, missing info, a decision only the",
        "  user can make): still write the report (the blocker, what you",
        "  tried), leave the phase as-is and stop. Do not hand off.",
        "- Never set status=done or archive on your own initiative -- only when",
        "  the user or the task description explicitly tells you to (then:",
        f"  finish, commit if asked, then `ntasker done {tid}` as the very last",
        "  command -- it ends this session and releases the next queued task).",
        "- No new tracker tasks, no deletes, no writes to other task IDs.",
    ]
    return "\n".join(lines)


def quick_run_system_prompt(task_id: int) -> str:
    """Briefing for a quick-run session: name the task once it is clear.

    A quick run starts with a *blank* prompt (that is its whole point), so the
    hint cannot go into the seed -- it is appended to the agent's system prompt
    instead (:attr:`~ntasker.agents.AgentSpec.system_prompt_flag`), which costs
    no extra turn and leaves the input line empty. The task exists only so the
    session has something to hang on and carries a placeholder title; the agent
    replaces it once it knows what the user actually wants. Agents without such
    a flag simply never see this -- the task then keeps its placeholder title.
    """
    return (
        f"This session was started from nTasker's project quick-run. nTasker created "
        f"task #{task_id} (already phase=wip) with a placeholder title just so the "
        f"session is tracked -- the user deliberately started with an empty prompt. "
        f"As soon as their request is clear, give that task a real title, once and "
        f'without asking: `ntasker patch {task_id} --title "<concise title>"`. '
        f"Then get on with the work. This applies to task #{task_id} only; every "
        f"other nTasker rule stays as it is."
    )


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------


@dataclass
class TermSession:
    """One persistent ``claude`` PTY process plus its attached subscribers."""

    task_id: int
    proc: subprocess.Popen
    master_fd: int
    buffer: bytearray = field(default_factory=bytearray)
    subscribers: set = field(default_factory=set)  # set[asyncio.Queue]
    alive: bool = True
    exit_code: int | None = None
    # Monotonic timestamp of the last PTY output. Drives the "waiting for input"
    # heuristic (see :func:`session_states`): a long-silent terminal means
    # Claude is parked at a prompt rather than working.
    last_output: float = field(default_factory=time.monotonic)
    # Monotonic deadline until which PTY output is treated as a resize redraw
    # and does NOT bump ``last_output`` (see RESIZE_REDRAW_GRACE). 0 = inactive.
    resize_grace_until: float = 0.0
    # Explicit waiting/running state reported by the agent's own hooks
    # (``ntasker hook waiting|running``, see :func:`set_hook_state`). ``None``
    # until the first hook fires -- then the silence heuristic decides.
    hook_waiting: bool | None = None
    # The event loop the PTY reader lives on (set by :func:`_attach_reader`).
    # :func:`_stop` may run off-loop (a sync route in the threadpool) and needs
    # it to hand the final teardown back to the loop thread.
    loop: asyncio.AbstractEventLoop | None = None


SESSIONS: dict[int, TermSession] = {}


def active_session_ids() -> list[int]:
    """Task ids with a *live* session -- drives the per-task busy indicator."""
    return [tid for tid, s in SESSIONS.items() if s.alive]


# Sessions started OUTSIDE ntasker (``/task <id>`` typed into a terminal
# Claude Code), keyed by task id -> the ``claude`` process id the loader
# reported. No PTY, no hooks: the only thing known about such a run is that
# its process is alive, so liveness is a ``kill(pid, 0)`` probe on every read
# and a dead entry drops out on its own -- no end-of-session hook needed.
EXTERNAL: dict[int, int] = {}


def register_external(task_id: int, pid: int) -> None:
    """Record that task ``task_id`` is being worked on by external process ``pid``.

    One process works on one task: a second ``/task`` in the same session
    replaces the earlier entry for that pid.
    """
    for tid in [t for t, p in EXTERNAL.items() if p == pid]:
        del EXTERNAL[tid]
    EXTERNAL[task_id] = pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: exists, not ours
    return True


def external_session_ids() -> list[int]:
    """Task ids with a live external session -- dead processes are forgotten."""
    for tid in [t for t, p in EXTERNAL.items() if not _pid_alive(p)]:
        del EXTERNAL[tid]
    return list(EXTERNAL)


def set_hook_state(task_id: int, waiting: bool) -> bool:
    """Record the explicit state a session's hook reported. ``False`` = no live session."""
    sess = SESSIONS.get(task_id)
    if sess is None or not sess.alive:
        return False
    sess.hook_waiting = waiting
    return True


def session_states() -> dict[int, str]:
    """Map each live session's task id to ``"waiting"`` or ``"running"``.

    A session whose hooks have reported (:func:`set_hook_state`) is taken at
    its word: ``hook_waiting`` True -> ``"waiting"``, False -> ``"running"``.
    Claude Code sessions get those hooks via ``--settings``; OpenCode / Pi
    (and a Claude session before its first hook fires) fall back to the
    silence heuristic: no PTY output for at least the configured idle window
    reads as "parked at a prompt and wants the user" -- while an agent works
    its TUI keeps repainting, so a quiet terminal means it is blocked on
    input. The window comes from the ``claude_idle_seconds`` setting.
    """
    from ntasker.settings import CLAUDE_IDLE_SECONDS_DEFAULT, get_setting  # noqa: PLC0415

    # Never let a settings read break the busy-indicator poll: any failure
    # (DB hiccup, bad value) falls back to the default window.
    try:
        raw = get_setting("claude_idle_seconds")
        idle = float(raw) if raw else CLAUDE_IDLE_SECONDS_DEFAULT
    except Exception:  # noqa: BLE001 -- bad value or DB hiccup both fall back
        idle = CLAUDE_IDLE_SECONDS_DEFAULT
    now = time.monotonic()

    def state(s: TermSession) -> str:
        if s.hook_waiting is not None:
            return "waiting" if s.hook_waiting else "running"
        return "waiting" if now - s.last_output >= idle else "running"

    return {tid: state(s) for tid, s in SESSIONS.items() if s.alive}


def _clean_env(spec: AgentSpec, task_id: int) -> dict:
    """Child environment: nesting markers stripped, ntasker's own markers added.

    ``NTASKER_TASK_ID`` / ``NTASKER_URL`` let ``ntasker hook ...`` (and
    ``ntasker lock ...``) inside the session find their task and server;
    ``ntasker serve`` sets ``NTASKER_URL`` for its own process, the default
    covers a server started another way. The server's own ``bin`` dir goes
    first on ``PATH`` so the bare ``ntasker`` the hooks and seeds call is the
    same version as the server -- a stale install elsewhere on PATH would make
    every hook fail (and a failing ``Stop`` hook blocks the session's stop).
    """
    env = {k: v for k, v in os.environ.items() if k not in spec.strip_env}
    env["TERM"] = "xterm-256color"
    env["NTASKER_TASK_ID"] = str(task_id)
    env.setdefault("NTASKER_URL", "http://127.0.0.1:8766")
    own_bin = os.path.dirname(sys.executable)
    if os.path.isfile(os.path.join(own_bin, "ntasker")):
        env["PATH"] = own_bin + os.pathsep + env.get("PATH", "")
    return env


def _child_setup() -> None:
    """Run in the forked child before exec: make the PTY our controlling tty."""
    os.setsid()
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def mark_wip(task_id: int) -> None:
    """Move a starting task to ``phase=wip`` -- the loader's job, done here.

    Queued runs never execute the ``/task`` loader, so its "starting work
    marks the task in progress" step moves here. Same guards as the loader:
    never resurrect an archived/closed task, no-op when already wip.
    Best-effort -- a DB hiccup must not block the spawn.
    """
    from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle

    with contextlib.suppress(Exception):
        with get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET phase = 'wip' "
                "WHERE id = ? AND archived = 0 AND status != 'done' AND phase != 'wip'",
                (task_id,),
            )


def _store_session_id(task_id: int, session_id: str) -> None:
    """Persist a run's forced session id so the task can be resumed later.

    Best-effort, mirroring :func:`mark_wip`: a DB hiccup must never block the
    spawn. Overwrites any previous id -- the column always points at the task's
    most recent web-terminal run.
    """
    from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle

    with contextlib.suppress(Exception):
        with get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?",
                (session_id, task_id),
            )


def _store_run_baselines(task_id: int, cwd: str) -> None:
    """Persist the Diff view's baselines for a fresh run.

    One HEAD sha per directory the run holds -- ``cwd`` plus the task's
    directory locks (see :mod:`ntasker.locks`) -- so the agent's own commits
    still show and the diff outlives the session. A resume keeps the previous
    baselines. Best-effort, like :func:`_store_session_id`.
    """
    from ntasker import locks  # noqa: PLC0415 -- lazy: avoid cycle
    from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle
    from ntasker.rundiff import baselines_for  # noqa: PLC0415 -- lazy: avoid cycle

    with contextlib.suppress(Exception):
        with get_conn() as conn:
            row = conn.execute("SELECT locks FROM tasks WHERE id = ?", (task_id,)).fetchone()
            dirs = [os.path.realpath(cwd)]
            for name in locks.parse(row["locks"] if row else None):
                d = locks.resolve_dir(name)
                if d not in dirs and os.path.isdir(d):
                    dirs.append(d)
            conn.execute(
                "UPDATE tasks SET run_baselines = ? WHERE id = ?",
                (baselines_for(dirs), task_id),
            )


def _stored_session_id(task_id: int) -> str | None:
    """The task's persisted Claude session id, or ``None`` if it never ran."""
    from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle

    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
    except Exception:  # noqa: BLE001 -- a DB hiccup must not break the spawn path
        return None
    return row["session_id"] if row else None


def _start_session(
    task_id: int,
    *,
    seed: str | None = None,
    resume: bool = False,
    quick: bool = False,
) -> TermSession:
    """Spawn a fresh agent session in a PTY and register it with a live reader.

    The agent (Claude / OpenCode / Pi) is resolved from the task's ``agent``
    field; its :class:`~ntasker.agents.AgentSpec` builds the full argv --
    including permission/auto flags and how the seed is attached (positional
    vs ``--prompt``) -- and which env markers get stripped. The cwd is derived
    from the task's project (see :func:`default_cwd_for_project`) and always
    set on the subprocess (uniform across agents).

    ``quick`` marks a session started from the sidebar quick run: it has no seed
    at all, and gets the "name this task once you know what it is" briefing via
    the system prompt (see :func:`quick_run_system_prompt`).
    """
    from ntasker.claude_assets import hooks_settings_path  # noqa: PLC0415 -- lazy: avoid cycle
    from ntasker.settings import get_dir_locks  # noqa: PLC0415 -- lazy: avoid cycle

    row = _task_row(task_id)
    # The one choke point every spawn passes (queue, resume): a draft is an
    # idea on file, never a job -- refuse here so no caller can slip past.
    if row is not None and row["draft"]:
        raise DraftTaskError(f"task #{task_id} is a draft")
    spec = get_spec(resolve_agent_key(row["agent"] if row else None))
    cwd = default_cwd_for_project(row["project"] if row else None)
    # ntasker's Claude Code hooks (explicit waiting/running state, and the
    # directory-lock guard when dir_locks is on). Only agents with a settings
    # flag get the file; build_spawn ignores it otherwise.
    settings_path = hooks_settings_path(get_dir_locks()) if spec.settings_flag else None
    master, slave = os.openpty()
    # Resume: reopen the stored session (conversation replays, no seed). Only
    # when the agent supports it and an id was captured on a previous run --
    # otherwise fall through to a fresh session.
    resume_id = _stored_session_id(task_id) if (resume and spec.resume_flag) else None
    sys_prompt = quick_run_system_prompt(task_id) if quick else None
    if resume_id:
        args = spec.build_spawn(None, resume_id=resume_id, settings_path=settings_path)
    elif spec.session_flag:
        # Fresh run: force a known session id so it can be resumed later, and
        # persist it. uuid4 is what --session-id expects (a canonical UUID).
        forced_id = str(uuid.uuid4())
        args = spec.build_spawn(
            seed, session_id=forced_id, system_prompt=sys_prompt, settings_path=settings_path
        )
        _store_session_id(task_id, forced_id)
    else:
        args = spec.build_spawn(seed, system_prompt=sys_prompt, settings_path=settings_path)
    # The cwd is a best-effort guess from the task's project name (see
    # default_cwd_for_project). A new project's directory may not exist yet:
    # resolve_run_cwd creates it when it lives inside the configured
    # ``projects_base`` (so a new project starts in a fresh dir), and otherwise
    # falls back to the home directory so the agent always starts rather than
    # dying on a FileNotFoundError before the TUI ever paints.
    run_cwd = resolve_run_cwd(cwd)
    proc = subprocess.Popen(
        args,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=run_cwd,
        env=_clean_env(spec, task_id),
        preexec_fn=_child_setup,
        close_fds=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    if not resume_id:
        _store_run_baselines(task_id, run_cwd)
    sess = TermSession(task_id=task_id, proc=proc, master_fd=master)
    SESSIONS[task_id] = sess
    _attach_reader(sess)
    return sess


def start_detached_session(
    task_id: int, seed: str | None, quick: bool = False, resume: bool = False
) -> bool:
    """Start a session with no browser attached. The task queue's spawn path.

    The queue is the only way a fresh session starts, so this is *the* spawn:
    the session lands in the registry, shows up in the busy indicators and the
    run-view tab strip, and the user can open its terminal at any point to
    watch or take over. ``quick`` = a sidebar quick run (blank prompt, see
    :func:`quick_run_system_prompt`); ``resume`` reopens the task's stored
    session instead of seeding a fresh one, and leaves the phase untouched.
    Returns ``False`` when the task already has a live session, or is a draft.

    Must be called from the event loop: the PTY reader is registered on the
    running loop (see :func:`_attach_reader`).
    """
    sess = SESSIONS.get(task_id)
    if sess is not None and sess.alive:
        return False
    SESSIONS.pop(task_id, None)   # drop a stale dead session before replacing it
    try:
        _start_session(task_id, seed=seed, quick=quick, resume=resume)
    except DraftTaskError:
        return False
    if not resume:
        mark_wip(task_id)
    return True


def _attach_reader(sess: TermSession) -> None:
    """Drain the PTY master into the buffer + all subscriber queues."""
    loop = asyncio.get_running_loop()
    sess.loop = loop

    def on_readable() -> None:
        try:
            data = os.read(sess.master_fd, 65536)
        except OSError:
            data = b""
        if not data:  # EOF / EIO -> the process exited
            _reap(sess)
            return
        # Output inside the post-resize grace window is just the SIGWINCH redraw,
        # not progress -- forward it but keep the "waiting" clock running so a
        # quick peek doesn't reset the heuristic.
        now = time.monotonic()
        if now >= sess.resize_grace_until:
            sess.last_output = now
        sess.buffer.extend(data)
        if len(sess.buffer) > BUFFER_LIMIT:
            del sess.buffer[: len(sess.buffer) - BUFFER_LIMIT]
        for q in list(sess.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(("output", data))

    loop.add_reader(sess.master_fd, on_readable)


def _reap(sess: TermSession) -> None:
    """Tear down a session whose process has exited."""
    with contextlib.suppress(Exception):
        asyncio.get_running_loop().remove_reader(sess.master_fd)
    with contextlib.suppress(OSError):
        os.close(sess.master_fd)
    sess.alive = False
    rc = sess.proc.poll()
    if rc is None:
        with contextlib.suppress(Exception):
            rc = sess.proc.wait(timeout=1)
    sess.exit_code = rc
    for q in list(sess.subscribers):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(("exit", rc))
    # A finished session with nobody attached can't be reattached usefully --
    # drop it so it doesn't linger in the registry (and the busy indicator).
    if not sess.subscribers:
        SESSIONS.pop(sess.task_id, None)


def _resize(sess: TermSession, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 1000))
    cols = max(1, min(int(cols), 1000))
    sess.resize_grace_until = time.monotonic() + RESIZE_REDRAW_GRACE
    with contextlib.suppress(OSError):
        fcntl.ioctl(sess.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# Grace period between the polite SIGTERM and the SIGKILL fallback. ``claude``
# is a Node TUI with child processes (MCP servers, spawned shells) that don't
# always honour SIGTERM promptly; without an escalation the PTY never closes,
# _reap never runs, and the whole process group lingers in the background. We
# give it a short window, then force the group down so a stopped session is
# *guaranteed* to die.
STOP_GRACE_SECONDS = 3.0


def _stop(sess: TermSession) -> None:
    """Terminate the session's whole process group, escalating to SIGKILL.

    Sends SIGTERM first, then -- from a short-lived daemon thread so the caller
    never blocks -- waits for the PTY to close (``sess.alive`` flips False once
    :func:`_reap` runs) and force-kills the group with SIGKILL if anything is
    still standing after ``STOP_GRACE_SECONDS``. Whatever still holds the PTY
    open after that is not the agent (its process is gone) but a stray
    grandchild outside its process group -- e.g. a ``setsid`` background job
    that inherited the terminal. Such a session would otherwise stay "alive"
    forever, with Stop having no visible effect; so the escalation ends with
    an unconditional :func:`_reap` on the loop thread. Idempotent; safe to call
    twice.
    """
    if not sess.alive:
        return
    try:
        pgid: int | None = os.getpgid(sess.proc.pid)
    except OSError:
        pgid = None  # agent process already gone -- only the PTY is still open

    if pgid is not None:
        with contextlib.suppress(Exception):
            os.killpg(pgid, signal.SIGTERM)

    def _finish() -> None:
        if sess.alive:
            _reap(sess)

    def _escalate() -> None:
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not sess.alive:
                return  # _reap saw the PTY close -> the group is gone
            time.sleep(0.1)
        if pgid is not None:
            with contextlib.suppress(Exception):
                os.killpg(pgid, signal.SIGKILL)
        if sess.loop is not None:
            sess.loop.call_soon_threadsafe(_finish)

    threading.Thread(
        target=_escalate, name=f"ntasker-stop-{sess.task_id}", daemon=True
    ).start()


def stop_session(task_id: int) -> bool:
    """Terminate a task's session completely, if one is running.

    Reached by the user's Stop button and by ``status=done`` -- the one case in
    which nTasker ends a session on its own (the PATCH handler and the queue
    worker's sweep). Returns ``True`` iff a live session was stopped.
    """
    sess = SESSIONS.get(task_id)
    if sess is None or not sess.alive:
        return False
    _stop(sess)
    return True


def _b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


# ---------------------------------------------------------------------------
# Drag-dropped file uploads
# ---------------------------------------------------------------------------

# Cap on a single drag-dropped file (base64-decoded). Big enough for any
# screenshot/image, small enough to reject an accidental multi-GB drop.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Where drag-dropped files land before their path is typed into the PTY. One
# dir for the whole app; unique names avoid collisions. Cleared by the OS on
# reboot like any temp dir -- we don't own the files' lifecycle once the agent
# has read them.
_UPLOAD_DIR = Path(tempfile.gettempdir()) / "ntasker-uploads"


def _save_upload(name: str, data: bytes) -> str:
    """Write a drag-dropped file to a temp dir and return its absolute path.

    The returned path is what gets typed into the PTY -- mirroring a real
    terminal, where dragging a file inserts its path. The agent then reads the
    file (e.g. attaches an image) from that path. ``name`` is reduced to a bare
    basename so a crafted value cannot escape the upload dir.
    """
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    base = os.path.basename(name).lstrip(".") or "file"
    dest = _UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{base}"
    dest.write_bytes(data)
    return str(dest)


# ---------------------------------------------------------------------------
# WebSocket bridge
# ---------------------------------------------------------------------------


async def serve(websocket: WebSocket, task_id: int) -> None:
    """Bridge an accepted WebSocket to task ``task_id``'s PTY session.

    Protocol (JSON):

    * client -> ``{"type":"attach", "resume"}`` -- reattaches the task's live
      session. Attaching never starts a fresh run (the task queue does that);
      the one exception is ``resume`` truthy, which reopens a finished task's
      stored session id. No live session and no ``resume`` -> ``error``.
    * client -> ``{"type":"input", "data"}`` (keystrokes, written to the PTY)
    * client -> ``{"type":"file", "name", "data"}`` (base64 file bytes; saved to
      a temp file whose path is typed into the PTY -- like a terminal drag-drop)
    * client -> ``{"type":"resize", "rows", "cols"}``
    * client -> ``{"type":"stop"}``
    * server -> ``{"type":"output", "data"}`` (base64 PTY bytes)
    * server -> ``{"type":"exit", "code"}``
    """
    available, reason = terminal_available(_spec_for_task(task_id))
    if not available:
        await websocket.send_json({"type": "error", "error": reason})
        return
    try:
        first = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError):
        return
    if first.get("type") != "attach":
        await websocket.send_json({"type": "error", "error": "expected an attach message"})
        return

    sess = SESSIONS.get(task_id)
    if sess is None or not sess.alive:
        if not first.get("resume"):
            await websocket.send_json(
                {"type": "error", "error": "no live session for this task -- queue it to start one"}
            )
            return
        if sess is not None:  # stale dead session -> replace
            SESSIONS.pop(task_id, None)
        try:
            sess = _start_session(task_id, resume=True)
        except DraftTaskError:
            await websocket.send_json({"type": "error", "error": "draft tasks are never started"})
            return

    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    sess.subscribers.add(queue)

    # Replay the buffer so the reattaching terminal lands on the live screen.
    if sess.buffer:
        await websocket.send_json({"type": "output", "data": _b64(sess.buffer)})
    if not sess.alive:
        await websocket.send_json({"type": "exit", "code": sess.exit_code})

    async def pump_out() -> None:
        while True:
            kind, payload = await queue.get()
            if kind == "output":
                await websocket.send_json({"type": "output", "data": _b64(payload)})
            elif kind == "exit":
                await websocket.send_json({"type": "exit", "code": payload})

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "input" and sess.alive:
                with contextlib.suppress(OSError):
                    os.write(sess.master_fd, str(msg.get("data", "")).encode("utf-8", "ignore"))
            elif kind == "file" and sess.alive:
                # A file dropped onto the terminal: decode, cap, save, then type
                # its quoted path (+ trailing space) into the PTY -- exactly what
                # a real terminal does on drag-drop. Invalid/oversized payloads
                # are dropped silently (the client guards size and toasts).
                try:
                    blob = base64.b64decode(str(msg.get("data", "")), validate=True)
                except (ValueError, binascii.Error):
                    continue
                if not blob or len(blob) > MAX_UPLOAD_BYTES:
                    continue
                path = _save_upload(str(msg.get("name", "")), blob)
                with contextlib.suppress(OSError):
                    os.write(sess.master_fd, (shlex.quote(path) + " ").encode("utf-8"))
            elif kind == "resize":
                _resize(sess, msg.get("rows", 24), msg.get("cols", 80))
            elif kind == "stop":
                _stop(sess)
    except WebSocketDisconnect:
        pass
    finally:
        out_task.cancel()
        sess.subscribers.discard(queue)
        # A finished session with nobody watching is no longer reattachable
        # in any useful way -- drop it so it stops lingering in the registry.
        if not sess.alive and not sess.subscribers:
            SESSIONS.pop(task_id, None)
