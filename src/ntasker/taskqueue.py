"""The auto-run task queue -- an ordered worklist ntasker works through itself.

A task is *queued* when its ``queue_order`` is not NULL; queued tasks are worked
top-down (``queue_order ASC``). :func:`worker` ticks a few times a second and
does exactly two things:

* **retire** entries that are finished: the task is ``status=done`` (closed by
  the user, or by the agent itself when the task told it to), or it was
  archived / deleted;
* **start** the head-most startable task of every project that has no live
  session of an *open* task yet -- one concurrent run per project, so several
  projects progress in parallel while a single project stays strictly
  sequential. With ``dir_locks`` on, a task also waits while another live
  session holds one of its directories (see :mod:`ntasker.locks`), and with
  ``require_clean`` while one is git-dirty.

``done`` is the **only** thing that makes nTasker end a session: once a task
is done its session is finished work, so :func:`tick` kills it -- whichever way
the status was set (UI, API, ``ntasker done`` from inside the session, a direct
DB write). Nothing else ends a session: it otherwise runs until its process
exits or the user stops it from the terminal view. The agent's review hand-off
(``ntasker patch <id> --phase review``) is a phase change like any other: the
task waits in the review column, its session alive, and the queue moves on
only once it is done.

Session ended without the task being done (agent stopped, crashed, hit a
blocker)? The entry **stays** queued, flagged ``session_ended_at``, and blocks
its lane until the user looks at it: remove it, set the task done, run it
again (the run button / ``queue add --top`` clear the flag and start fresh), or
**resume** it -- reopen the stored conversation (:data:`RESUME`, Claude only).
A server restart kills every session; the worker flags the running ones on
shutdown and resumes every flagged, resumable entry on its first tick, so the
runs pick up where they were instead of starting over.

A queued run gets its own seed (:func:`~ntasker.claude_runner.queue_seed_for_task`)
which tells the agent to hand the finished task to ``phase=review`` unprompted
and leave the session open. Closing stays the user's call unless the task
description grants it -- so a queue run leaves its results in the review
column, and the next task in that project starts when the user closes it.

The queue is the **only** way a session starts: every run button appends to
the queue (:func:`enqueue`), and the worker picks the task up once its project
lane is free. ``queue_enabled`` (default on) is a pause switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3

from ntasker import locks
from ntasker.agents import agent_keys, get_spec, resolve_agent_key
from ntasker.claude_runner import (
    active_session_ids,
    mark_wip,
    queue_seed_for_task,
    start_detached_session,
    stop_session,
    terminal_available,
)
from ntasker.db import get_conn

# How often the worker looks at the queue. Fast enough that the next task starts
# right after you see the previous one finish, cheap enough to run forever: a
# tick is one small indexed SELECT and nothing else while the queue is empty.
QUEUE_TICK = 2.0

# Task ids the queue considers "currently running". Populated from the live
# session registry on each tick, so a run started from the UI on an already
# queued task counts too. An id in here that no longer has a session means the
# run is over -- that is what triggers the retire step.
_running: set[int] = set()

# Task ids to start as a *quick run* -- blank prompt plus the "name this task"
# briefing (see :func:`~ntasker.claude_runner.quick_run_system_prompt`). Filled
# by the sidebar quick run, consumed by :func:`tick` on start. In memory only:
# API and worker share the process, and a quick task that has not started
# before a restart simply runs as a normal queued run.
QUICK: set[int] = set()

# Task ids to start by *resuming* their stored session (``claude --resume``)
# instead of a fresh seeded run: the user pressed resume on an ended entry, or
# the worker collected the ended entries on its first tick after a restart.
# Consumed by :func:`tick` on start, which also clears the entry's ended flag.
RESUME: set[int] = set()

# Whether :func:`tick` has run once since the process started -- gates the
# resume-after-restart sweep.
_booted = False


def _bucket(project: str | None) -> str:
    """Concurrency bucket for a task: its project, or one shared cross-project
    bucket for tasks without one."""
    return project or ""


def load_queue() -> list[sqlite3.Row]:
    """Queued task rows in execution order (head first)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE queue_order IS NOT NULL ORDER BY queue_order ASC"
        ).fetchall()


def set_queue(ids: list[int]) -> list[sqlite3.Row]:
    """Replace the queue with ``ids`` (head first) and return the stored result.

    The whole column is rewritten as a dense 1..n sequence: the frontend owns
    the ordered list and PUTs it after every drop, which keeps add / reorder /
    remove a single operation instead of three. Ids that are not open, are
    archived, or do not exist are dropped silently -- the worker retires exactly
    those anyway, and a stale browser list must not resurrect them.
    """
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET queue_order = NULL WHERE queue_order IS NOT NULL")
        position = 0
        for tid in ids:
            cur = conn.execute(
                "UPDATE tasks SET queue_order = ? "
                "WHERE id = ? AND archived = 0 AND status = 'open'",
                (float(position + 1), int(tid)),
            )
            if cur.rowcount:
                position += 1
        # Queue-run state belongs to a queued row only; a removed entry must
        # not carry a stale flag into its next run.
        conn.execute(
            "UPDATE tasks SET session_ended_at = NULL "
            "WHERE queue_order IS NULL AND session_ended_at IS NOT NULL"
        )
        return conn.execute(
            "SELECT * FROM tasks WHERE queue_order IS NOT NULL ORDER BY queue_order ASC"
        ).fetchall()


def enqueue(task_id: int) -> list[sqlite3.Row]:
    """Append ``task_id`` to the queue -- "run this when its lane is free".

    What every run button does: a new id goes to the end, so the running task
    stays at the top of its project and the rest follow in the order they
    were queued; an already-queued id keeps its position. Same filtering as
    :func:`set_queue`.

    The task moves to ``phase=wip`` right away, not only once its session
    spawns: a task waiting behind a running one is in progress from the
    user's point of view, and leaving it in ``planned`` misleads the board.
    """
    clear_ended([task_id])   # "run it again" for an entry whose session ended
    mark_wip(task_id)
    ids = [int(r["id"]) for r in load_queue()]
    if task_id in ids:
        return load_queue()
    return set_queue([*ids, task_id])


def clear_ended(ids: list[int]) -> None:
    """Forget that these entries' sessions ended -- "run it again"."""
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE tasks SET session_ended_at = NULL WHERE id IN ({placeholders})", ids
        )


def resumable(row: sqlite3.Row) -> bool:
    """Whether a queued entry's stored session can be reopened: it ran once
    (a captured session id) and its agent has a resume flag (Claude only)."""
    return bool(row["session_id"]) and get_spec(resolve_agent_key(row["agent"])).resume_flag is not None


def request_resume(task_id: int) -> bool:
    """The resume button on an ended entry: reopen its conversation next tick.

    Only for a queued, resumable task; the entry keeps its queue position and
    its ended flag is cleared when the session is actually spawned. Returns
    ``False`` when the task is not queued or cannot be resumed.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND queue_order IS NOT NULL", (task_id,)
        ).fetchone()
    if row is None or not resumable(row):
        return False
    RESUME.add(task_id)
    return True


def flag_running_ended() -> None:
    """Shutdown: flag every queued run that is live right now as ended.

    The server going down takes its sessions with it, and the worker will not
    tick again to notice -- so the flag is written here, and the first tick
    after the restart resumes the flagged entries (see :func:`tick`).
    """
    ids = _running | ({int(r["id"]) for r in load_queue()} & set(active_session_ids()))
    if not ids:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE tasks SET session_ended_at = ? "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND session_ended_at IS NULL",
            [_now(), *ids],
        )


def _dequeue(conn: sqlite3.Connection, ids: list[int]) -> None:
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE tasks SET queue_order = NULL, session_ended_at = NULL "
        f"WHERE id IN ({placeholders})",
        ids,
    )


def _busy_buckets(live: set[int], conn: sqlite3.Connection) -> set[str]:
    """Buckets occupied by a live session of an open task -- queued or started
    by hand.

    A manually opened session in a project blocks the queue there too: two
    agents in one working directory is exactly what the one-per-project rule
    exists to prevent. A done task's session is being killed (see
    :func:`_kill_done`) and does not count in the meantime.
    """
    if not live:
        return set()
    placeholders = ",".join("?" * len(live))
    rows = conn.execute(
        f"SELECT project FROM tasks WHERE id IN ({placeholders}) AND status != 'done'",
        list(live),
    ).fetchall()
    return {_bucket(r["project"]) for r in rows}


def _runnable_agents() -> set[str]:
    """Agent keys whose CLI is launchable right now.

    Resolved once per tick: ``terminal_available`` reads the agent's binary
    override from the settings store, so asking it per queued task would mean a
    fresh DB connection per row, every two seconds, forever.
    """
    return {key for key in agent_keys() if terminal_available(get_spec(key))[0]}


def _startable(
    row: sqlite3.Row, conn: sqlite3.Connection, runnable: set[str], default_agent: str
) -> bool:
    """Whether the queue may launch this task right now.

    Rejects a task whose dependencies are still open (the same guard the kanban
    drag applies) and one whose agent CLI is not installed. Neither is a failure
    of the task -- it stays queued, and the queue simply looks at the next entry
    in that project instead of stalling behind it.

    ``runnable`` and ``default_agent`` are resolved once per tick by the caller;
    both would otherwise cost a settings lookup (i.e. a DB connection) per row.
    Same precedence as :func:`~ntasker.agents.resolve_agent_key`.
    """
    key = row["agent"] if row["agent"] in agent_keys() else default_agent
    if key not in runnable:
        return False
    blocked = conn.execute(
        """
        SELECT 1 FROM task_deps d JOIN tasks t ON t.id = d.depends_on_id
        WHERE d.task_id = ? AND t.status != 'done' LIMIT 1
        """,
        (row["id"],),
    ).fetchone()
    return blocked is None


def _lock_reason(
    row: sqlite3.Row, held: dict[str, int], require_clean: bool
) -> dict | None:
    """Why directory locks keep this task from starting, or ``None``.

    ``{"reason": "lock", "project", "holder"}`` when another live task holds
    one of its directories; ``{"reason": "dirty", "project", "holder": None}``
    when ``require_clean`` is on and one of them has uncommitted changes. The
    git check only runs for a task that passed every cheaper gate, so an idle
    queue costs nothing. Shared by the start decision and :func:`skipped`.
    """
    own = row["project"]
    names = [*([own] if own else []), *locks.parse(row["locks"])]
    by_dir = {locks.resolve_dir(n): n for n in names}
    hit = locks.conflict(set(by_dir), held, exclude=int(row["id"]))
    if hit is not None:
        return {"reason": "lock", "project": by_dir[hit[0]], "holder": hit[1]}
    if require_clean:
        dirty = locks.dirty_dir(set(by_dir))
        if dirty is not None:
            return {"reason": "dirty", "project": by_dir[dirty], "holder": None}
    return None


def skipped(rows: list[sqlite3.Row], live: set[int]) -> dict[int, dict]:
    """Lock / dirty reasons per queued task id, for the queue panel.

    Same rules as the start step: only while ``dir_locks`` is on, only for
    tasks without a live session, and a lock conflict wins over the git check.
    """
    from ntasker.settings import get_dir_locks, get_require_clean  # noqa: PLC0415

    out: dict[int, dict] = {
        int(row["id"]): {"reason": "ended", "project": row["project"], "holder": None}
        for row in rows
        if row["session_ended_at"] and int(row["id"]) not in live and int(row["id"]) not in RESUME
    }
    if not rows or not get_dir_locks():
        return out
    require_clean = get_require_clean()
    with get_conn() as conn:
        held = locks.held_dirs(conn, live)
    for row in rows:
        if int(row["id"]) in live or int(row["id"]) in out:
            continue
        reason = _lock_reason(row, held, require_clean and int(row["id"]) not in RESUME)
        if reason is not None:
            out[int(row["id"])] = reason
    return out


def _kill_done(live: set[int]) -> None:
    """Stop every live session whose task is done.

    The DB is the single source of truth and the status can flip in another
    process (``ntasker done`` run inside the session, a direct DB write), so
    the worker sweeps rather than relying on the API path alone. The PATCH
    handler still stops synchronously so the UI does not have to wait a tick.
    """
    if not live:
        return
    placeholders = ",".join("?" * len(live))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status = 'done'",
            list(live),
        ).fetchall()
    for r in rows:
        stop_session(int(r["id"]))


def tick() -> None:
    """One pass: kill the done, retire what is finished, start what is next. Never raises."""
    global _booted
    from ntasker.settings import (  # noqa: PLC0415 -- lazy: avoid cycle
        get_dir_locks,
        get_queue_enabled,
        get_require_clean,
    )

    enabled = get_queue_enabled()
    live = set(active_session_ids())
    _kill_done(live)
    rows = load_queue()
    queued_ids = {int(r["id"]) for r in rows}

    # Adopt every queued task that has a session, so a run the user started by
    # hand on a queued task advances the queue just like a queued one. Only
    # while the queue is on -- a hand-started run on a paused queue is the
    # user's own business and must not consume its queue entry. Ids that left
    # the queue behind our back (removed, deleted) are forgotten.
    if enabled:
        _running.update(queued_ids & live)
    _running.intersection_update(queued_ids)

    # Finished: ``status=done`` (the user closed it, or the agent did because
    # the task told it to) or archived. The entry leaves the queue; a done
    # task's session is already being killed by ``_kill_done``. A review
    # hand-off is not a finish.
    retired = [int(r["id"]) for r in rows if r["status"] == "done" or r["archived"]]
    if retired:
        with get_conn() as conn:
            _dequeue(conn, retired)
        _running.difference_update(retired)
        rows = [r for r in rows if int(r["id"]) not in retired]

    # Ended without a hand-off (stopped, crashed, blocker): flag the entry and
    # keep it -- it blocks its lane until the user has looked at it. A column,
    # not memory, so a server restart does not silently start it again.
    ended = [int(r["id"]) for r in rows if int(r["id"]) in _running and int(r["id"]) not in live]
    if ended:
        with get_conn() as conn:
            conn.execute(
                f"UPDATE tasks SET session_ended_at = ? "
                f"WHERE id IN ({','.join('?' * len(ended))}) AND session_ended_at IS NULL",
                [_now(), *ended],
            )
        _running.difference_update(ended)
        rows = load_queue()

    # After a restart every session is gone: resume the entries flagged ended
    # (by the previous process on shutdown, or by its last tick) rather than
    # starting them over. Once, on the first tick -- a session the user stopped
    # on purpose later must not come back on its own.
    if not _booted:
        _booted = True
        RESUME.update(int(r["id"]) for r in rows if r["session_ended_at"] and resumable(r))
    RESUME.intersection_update(int(r["id"]) for r in rows)

    if not rows or not enabled:
        return

    # Start: one task per free bucket, head-most first. With directory locks
    # on, a task whose directories a live session holds (or, with
    # ``require_clean``, whose directories are git-dirty) waits as well.
    runnable = _runnable_agents()
    default_agent = resolve_agent_key(None)
    dir_locks = get_dir_locks()
    require_clean = dir_locks and get_require_clean()
    starts: list[int] = []
    with get_conn() as conn:
        busy = _busy_buckets(live, conn)
        busy |= {
            _bucket(r["project"])
            for r in rows
            if r["session_ended_at"] and int(r["id"]) not in RESUME
        }
        held = locks.held_dirs(conn, live) if dir_locks else {}
        for row in rows:
            bucket = _bucket(row["project"])
            if bucket in busy or not _startable(row, conn, runnable, default_agent):
                continue
            if dir_locks:
                # A resume skips the git check: the dirt is that run's own
                # unfinished work, which is exactly what it continues.
                resume = int(row["id"]) in RESUME
                if _lock_reason(row, held, require_clean and not resume) is not None:
                    continue
                for d in locks.task_dirs(row["project"], locks.parse(row["locks"])):
                    held.setdefault(d, int(row["id"]))
            starts.append(int(row["id"]))
            busy.add(bucket)

    # Spawning happens after the DB context is closed: it forks a process and
    # registers a PTY reader, neither of which should hold a connection open.
    for task_id in starts:
        task = _task_dict(task_id)
        if task is None:
            continue
        quick = task_id in QUICK
        resume = task_id in RESUME
        started = start_detached_session(
            task_id,
            None if (quick or resume) else queue_seed_for_task(task),
            quick=quick,
            resume=resume,
        )
        if started:
            QUICK.discard(task_id)
            if resume:
                RESUME.discard(task_id)
                clear_ended([task_id])
            else:
                # The loader's "starting work marks it in progress" move. A
                # resume keeps the phase -- the conversation continues as is.
                mark_wip(task_id)
            _running.add(task_id)


def _now() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().isoformat(timespec="seconds")


def _task_dict(task_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


async def worker() -> None:
    """Background loop driving :func:`tick`. Started by the FastAPI app."""
    while True:
        await asyncio.sleep(QUEUE_TICK)
        # A DB hiccup or a failed spawn must never kill the loop -- the next
        # tick re-reads the queue from scratch and picks up where it left off.
        with contextlib.suppress(Exception):
            tick()
