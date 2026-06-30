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
import contextlib
import os
import signal
import threading
import time
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


def _spec_for_task(task_id: int) -> AgentSpec:
    """Resolve the :class:`AgentSpec` for a task from its persisted ``agent``."""
    from ntasker.db import get_conn  # noqa: PLC0415

    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT agent FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
    except Exception:  # noqa: BLE001 -- a DB hiccup must not crash the spawn path
        row = None
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
    3. Otherwise the home directory -- a best-effort fallback so the agent
       always starts. A path outside the base, or no ``projects_base``
       configured at all, is never silently created.
    """
    home = os.path.expanduser("~")
    if not cwd:
        return home
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
    return home


def seed_command_for_task(task: dict) -> str:
    """Initial input for the session: the ntasker ``/task <id>`` slash command.

    ntasker ships that command for Claude Code; firing it as the first message
    loads the task (title, description, phase) into the session via the existing
    integration -- no guessing, no clash with the built-in ``Task`` tools.
    """
    return f"/task {task['id']}"


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


SESSIONS: dict[int, TermSession] = {}


def active_session_ids() -> list[int]:
    """Task ids with a *live* session -- drives the per-task busy indicator."""
    return [tid for tid, s in SESSIONS.items() if s.alive]


def session_states() -> dict[int, str]:
    """Map each live session's task id to ``"waiting"`` or ``"running"``.

    ``"waiting"``: the PTY produced no output for at least the configured idle
    window, which we read as "Claude is parked at a prompt and wants the user".
    The CLI emits no explicit "I have a question" event, so output-silence is
    the stand-in -- while Claude works its TUI keeps repainting (the spinner),
    so a quiet terminal means it is blocked on input. Everything else is
    ``"running"``. The window comes from the ``claude_idle_seconds`` setting.
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
    return {
        tid: ("waiting" if now - s.last_output >= idle else "running")
        for tid, s in SESSIONS.items()
        if s.alive
    }


def _clean_env(spec: AgentSpec) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in spec.strip_env}
    env["TERM"] = "xterm-256color"
    return env


def _child_setup() -> None:
    """Run in the forked child before exec: make the PTY our controlling tty."""
    os.setsid()
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _start_session(task_id: int, cwd: str | None, seed: str | None) -> TermSession:
    """Spawn a fresh agent session in a PTY and register it with a live reader.

    The agent (Claude / OpenCode / Pi) is resolved from the task's ``agent``
    field; its :class:`~ntasker.agents.AgentSpec` builds the full argv --
    including permission/auto flags and how the ``/task`` seed is attached
    (positional vs ``--prompt``) -- and which env markers get stripped. The cwd
    is always set on the subprocess (uniform across agents).
    """
    spec = _spec_for_task(task_id)
    master, slave = os.openpty()
    args = spec.build_spawn(seed)
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
        env=_clean_env(spec),
        preexec_fn=_child_setup,
        close_fds=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    sess = TermSession(task_id=task_id, proc=proc, master_fd=master)
    SESSIONS[task_id] = sess
    _attach_reader(sess)
    return sess


def _attach_reader(sess: TermSession) -> None:
    """Drain the PTY master into the buffer + all subscriber queues."""
    loop = asyncio.get_running_loop()

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
    still standing after ``STOP_GRACE_SECONDS``. Idempotent; safe to call twice.
    """
    if not sess.alive:
        return
    try:
        pgid = os.getpgid(sess.proc.pid)
    except OSError:
        return  # process already gone -- nothing left to signal

    with contextlib.suppress(Exception):
        os.killpg(pgid, signal.SIGTERM)

    def _escalate() -> None:
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not sess.alive:
                return  # _reap saw the PTY close -> the group is gone
            time.sleep(0.1)
        with contextlib.suppress(Exception):
            os.killpg(pgid, signal.SIGKILL)

    threading.Thread(
        target=_escalate, name=f"ntasker-stop-{sess.task_id}", daemon=True
    ).start()


def stop_session(task_id: int) -> bool:
    """Terminate a task's session completely, if one is running.

    Used when a task is marked done -- the work is finished, so the interactive
    session is torn down. Returns ``True`` iff a live session was stopped.
    """
    sess = SESSIONS.get(task_id)
    if sess is None or not sess.alive:
        return False
    _stop(sess)
    return True


def _b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


# ---------------------------------------------------------------------------
# WebSocket bridge
# ---------------------------------------------------------------------------


async def serve(websocket: WebSocket, task_id: int) -> None:
    """Bridge an accepted WebSocket to task ``task_id``'s PTY session.

    Protocol (JSON):

    * client -> ``{"type":"attach", "cwd", "seed"}`` (cwd/seed only used when a
      session has to be started; ignored on reattach)
    * client -> ``{"type":"input", "data"}`` (keystrokes, written to the PTY)
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
        if sess is not None:  # stale dead session -> replace
            SESSIONS.pop(task_id, None)
        cwd = (first.get("cwd") or "").strip() or None
        seed = (first.get("seed") or "").strip() or None
        sess = _start_session(task_id, cwd, seed)

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
