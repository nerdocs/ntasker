"""The auto-run task queue -- an ordered worklist ntasker works through itself.

A task is *queued* when its ``queue_order`` is not NULL; queued tasks are worked
top-down (``queue_order ASC``). :func:`worker` ticks a few times a second and
does two things (plus handing pending agent messages to idle sessions, see
:func:`ntasker.messages.deliver`):

* **retire** entries that are finished: the task is ``status=done`` (closed by
  the user, or by the agent itself when the task told it to), or it was
  archived / deleted -- or flagged ``draft``, which can never be queued;
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

A run a Claude **usage limit** stopped is neither: it is *paused*
(``tasks.limit_until``, see :mod:`ntasker.usagelimit`). It keeps its entry
and its lane, gets no run-log row, is not resumed early after a restart, and
:func:`resume_paused` continues it once the limit has reset.

A queued run gets its own seed (:func:`~ntasker.claude_runner.queue_seed_for_task`)
which tells the agent to hand the finished task to ``phase=review`` unprompted
and leave the session open. Closing stays the user's call unless the task
description grants it -- so a queue run leaves its results in the review
column, and the next task in that project starts when the user closes it.

A **fasttrack** task (``tasks.fasttrack``) finishes itself: its seed grants
the commit and tells the agent to end with ``ntasker finish`` -- ``ok`` sets the
task done (session killed, lane continues), ``failed``/``blocked`` leaves it as
today unless ``fail_continue`` is set, which drops the entry from the queue so
the lane keeps moving. Every fasttrack run leaves a ``run_outcomes`` row (the
run log, see :func:`~ntasker.db.insert_outcome`); a session that ends without
``finish`` gets an ``ended`` row from :func:`tick`.

A session started **outside** ntasker (``/task`` in a terminal, ``ntasker
adopt``) that is attached to a task while it runs joins the queue at the front
as that entry's run (:func:`adopt_running`): it ends and resumes like a queued
run, but a server restart drops the entry instead of resuming it.

The queue is the **only** way a session starts: every run button appends to
the queue (:func:`enqueue`), and the worker picks the task up once its project
lane is free. ``queue_enabled`` (default on) is a pause switch.

A **Quicktask** (the sidebar's quick run, with or without a prompt) is the one
exception to the lanes: with ``quicktasks_bypass_lanes`` on (the default) it
starts on the next tick regardless of what runs in its project, and its own
session neither occupies the lane nor holds its directories for the queue
(:data:`LANELESS`). Everything else -- retire, ended, resume -- applies as
usual.

A task **without a project** has no lane at all: it shares no working
directory with anything, so any number of them run side by side. Only its
explicit directory locks (``tasks.locks``) can still make it wait. The same
holds for a task in a git **worktree** (``tasks.worktree_id``, see
:mod:`ntasker.worktrees`): the tree is a lane of its own, so it runs next to
its project's lane, while two tasks of one tree still run one after the
other.

**Start now** (:func:`request_start_now`, :data:`FORCE`) is the user's override
for a single waiting entry: it starts on the next tick although its lane is
busy, a directory it needs is held, or one is git-dirty. From then on it is an
ordinary run -- it occupies the lane and its directories like any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
import time
from datetime import datetime

from ntasker import collab, lockqueue, locks, messages, usagelimit, worktrees
from ntasker.agents import agent_keys, get_spec, resolve_agent_key
from ntasker.claude_runner import (
    active_session_ids,
    SESSIONS,
    _write_pty,
    external_session_ids,
    mark_wip,
    queue_seed_for_task,
    start_detached_session,
    stop_session,
    terminal_available,
)
from ntasker.db import get_conn, insert_outcome
from ntasker.rundiff import changed_paths, parse_baselines

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

# Task ids that start outside the project lanes -- Quicktasks created while
# ``quicktasks_bypass_lanes`` is on. Such an entry neither waits for a free
# lane, held directory or clean git state, nor counts as occupying any of them.
# In memory like :data:`QUICK`: after a restart the entry is a normal queued
# run (its session is gone anyway and gets resumed like every other).
LANELESS: set[int] = set()

# Task ids to start by *resuming* their stored session (``claude --resume``)
# instead of a fresh seeded run: the user pressed resume on an ended entry, or
# the worker collected the ended entries on its first tick after a restart.
# Consumed by :func:`tick` on start, which also clears the entry's ended flag.
RESUME: set[int] = set()

# The subset of :data:`RESUME` a usage limit paused: their resume carries
# :data:`~ntasker.usagelimit.RESUME_PROMPT`, so the agent goes on by itself.
LIMIT_RESUME: set[int] = set()

# Task ids the user told to start *now* -- lane, directory locks and the git
# check are skipped for exactly one tick, after which the set is emptied: an
# override that could not be honoured must not fire on its own later.
FORCE: set[int] = set()

# Whether :func:`tick` has run once since the process started -- gates the
# resume-after-restart sweep.
_booted = False

# Summary of the ``ended`` outcome row -- stored data, not UI text.
ENDED_SUMMARY = "session ended without finish"


def _bucket(row: sqlite3.Row) -> str:
    """Concurrency bucket for a task: its project, or ``wt:<id>`` for a task in
    a git worktree (the tree is a lane of its own, see :mod:`ntasker.worktrees`).
    ``""`` for a task without a project -- which is no lane: such tasks never
    wait for each other."""
    if row["worktree_id"]:
        return f"wt:{row['worktree_id']}"
    return row["project"] or ""


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
    archived, are drafts, or do not exist are dropped silently -- the worker
    retires exactly those anyway, and a stale browser list must not resurrect
    them.
    """
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET queue_order = NULL WHERE queue_order IS NOT NULL")
        position = 0
        for tid in ids:
            cur = conn.execute(
                "UPDATE tasks SET queue_order = ? "
                "WHERE id = ? AND archived = 0 AND status = 'open' AND draft = 0 AND proposed = 0",
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
    if is_draft(task_id):
        return load_queue()
    clear_ended([task_id])   # "run it again" for an entry whose session ended
    mark_wip(task_id)
    ids = [int(r["id"]) for r in load_queue()]
    if task_id in ids:
        return load_queue()
    return set_queue([*ids, task_id])


def adopt_running(task_id: int) -> None:
    """Put a task whose session already runs outside ntasker into the queue.

    Called when a live external session is attached to a task (the ``/task``
    loader in a terminal, ``ntasker adopt`` with a pid). The run is already
    under way, so the entry goes to the front and counts as a queue run from
    the start: it occupies its lane, and when its process exits without the
    task being done the entry is flagged ended and offers a resume of that
    conversation -- like any queued run. Drafts and proposals stay out.
    """
    if is_draft(task_id):
        return
    clear_ended([task_id])
    mark_wip(task_id)
    rest = [int(r["id"]) for r in load_queue() if int(r["id"]) != task_id]
    if any(int(r["id"]) == task_id for r in set_queue([task_id, *rest])):
        _running.add(task_id)


def is_draft(task_id: int) -> bool:
    """True when the task is a draft or an inbox proposal (never to be started); False when missing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT draft, proposed FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return bool(row and (row["draft"] or row["proposed"]))


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


def request_start_now(task_id: int) -> bool:
    """The start-now button on a waiting entry: start it on the next tick,
    whatever occupies its lane or directories (see :data:`FORCE`).

    Only for a queued entry that waits: no live session, not flagged ended
    (that one is run again or resumed), queue switched on. Open dependencies
    and a missing agent CLI are not overridden -- the entry could not run.
    Returns ``False`` when any of that keeps the task from starting.
    """
    from ntasker.settings import get_queue_enabled  # noqa: PLC0415 -- lazy: avoid cycle

    if not get_queue_enabled():
        return False
    if task_id in set(active_session_ids()) | set(external_session_ids()):
        return False
    runnable = _runnable_agents()
    default_agent = resolve_agent_key(None)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND queue_order IS NOT NULL", (task_id,)
        ).fetchone()
        if row is None or row["session_ended_at"]:
            return False
        if not _startable(row, conn, runnable, default_agent):
            return False
    FORCE.add(task_id)
    return True


def flag_running_ended() -> None:
    """Shutdown: flag every queued run that is live right now as ended.

    The server going down takes its sessions with it, and the worker will not
    tick again to notice -- so the flag is written here, and the first tick
    after the restart resumes the flagged entries (see :func:`tick`).

    An adopted external run is the exception: its terminal process outlives
    the server, so resuming it would open the same conversation twice. Its
    entry leaves the queue instead -- the restarted server no longer knows the
    process, and the task stays in ``wip`` as before it was adopted.
    """
    external = _running & set(external_session_ids())
    ids = (_running | ({int(r["id"]) for r in load_queue()} & set(active_session_ids()))) - external
    with get_conn() as conn:
        if external:
            _dequeue(conn, list(external))
        if ids:
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
    """Buckets occupied by a live session of an open task -- queued, started
    by hand, or external.

    A manually opened session in a project blocks the queue there too: two
    agents in one working directory is exactly what the one-per-project rule
    exists to prevent -- so ``live`` includes the external sessions (``/task``
    in a terminal). A done task's session is being killed (see
    :func:`_kill_done`) and does not count in the meantime. Tasks without a
    project occupy nothing -- they have no lane.
    """
    if not live:
        return set()
    placeholders = ",".join("?" * len(live))
    rows = conn.execute(
        f"SELECT project, worktree_id FROM tasks WHERE id IN ({placeholders}) AND status != 'done'",
        list(live),
    ).fetchall()
    return {b for b in map(_bucket, rows) if b}


def _runnable_agents() -> set[str]:
    """Agent keys whose CLI is launchable right now.

    Resolved once per tick: ``terminal_available`` reads the agent's binary
    override from the settings store, so asking it per queued task would mean a
    fresh DB connection per row, every two seconds, forever.

    Claude is left out while a usage limit pauses a run (see
    :func:`~ntasker.usagelimit.hold_until`): a new run would only hit it at
    once and sit paused in its lane.
    """
    keys = {key for key in agent_keys() if terminal_available(get_spec(key))[0]}
    if usagelimit.hold_until() is not None:
        keys.discard("claude")
    return keys


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
    by_dir = locks.run_dirs(row)
    hit = locks.conflict(set(by_dir), held, exclude=int(row["id"]))
    if hit is not None:
        return {"reason": "lock", "project": by_dir[hit[0]], "holder": hit[1]}
    if require_clean:
        dirty = locks.dirty_dir(set(by_dir))
        if dirty is not None:
            return {"reason": "dirty", "project": by_dir[dirty], "holder": None}
    return None


def skipped(rows: list[sqlite3.Row], live: set[int]) -> dict[int, dict]:
    """Lock / dirty / limit reasons per queued task id, for the queue panel.

    Same rules as the start step: only while ``dir_locks`` is on, only for
    tasks without a live session, and a lock conflict wins over the git check.
    A Claude task waiting for a usage limit to reset (``limit``, with ``until``
    as ISO time) is named regardless of ``dir_locks`` -- a paused one with its
    own resume time, the others with the latest pause's.
    """
    from ntasker.settings import get_dir_locks, get_require_clean  # noqa: PLC0415

    out: dict[int, dict] = {
        int(row["id"]): {"reason": "limit", "project": row["project"], "holder": None, "until": row["limit_until"]}
        for row in rows
        if row["limit_until"] and int(row["id"]) not in live
    }
    out.update({
        int(row["id"]): {"reason": "ended", "project": row["project"], "holder": None}
        for row in rows
        if row["session_ended_at"] and int(row["id"]) not in live | RESUME | set(out)
    })
    hold = usagelimit.hold_until()
    if hold is not None:
        until = datetime.fromtimestamp(hold).astimezone().isoformat()
        default_agent = resolve_agent_key(None)
        for row in rows:
            key = row["agent"] if row["agent"] in agent_keys() else default_agent
            if key == "claude" and int(row["id"]) not in live and int(row["id"]) not in out:
                out[int(row["id"])] = {"reason": "limit", "project": row["project"], "holder": None, "until": until}
    if not rows or not get_dir_locks():
        return out
    require_clean = get_require_clean()
    with get_conn() as conn:
        held = locks.held_dirs(conn, live | set(external_session_ids()))
    for row in rows:
        if int(row["id"]) in live or int(row["id"]) in out or int(row["id"]) in LANELESS:
            continue
        reason = _lock_reason(row, held, require_clean and int(row["id"]) not in RESUME)
        if reason is not None:
            out[int(row["id"])] = reason
    return out


def _kill_done(live: set[int], external: set[int]) -> None:
    """Stop every live session whose task is done, and tell who waits for it.

    The DB is the single source of truth and the status can flip in another
    process (``ntasker done`` run inside the session, a direct DB write), so
    the worker sweeps rather than relying on the API path alone. The PATCH
    handler still stops synchronously so the UI does not have to wait a tick.
    A done task gives its project directory back, so its live dependents are
    told (:func:`ntasker.messages.notify_finished`, idempotent) -- also for an
    external session, which is not killed here.
    """
    ids = live | external
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status = 'done'",
            list(ids),
        ).fetchall()
        for r in rows:
            messages.notify_finished(conn, int(r["id"]), "done")
    for r in rows:
        if int(r["id"]) in live:
            stop_session(int(r["id"]))


#: Tasks with a live session on the previous tick (see :func:`_notify_ended`).
_was_live: set[int] = set()


def _notify_ended(alive: set[int]) -> None:
    """Sessions gone since the last tick give their project directory back.

    A task still open (review, stopped, crashed) stops holding its own project
    once its session ends (:func:`ntasker.locks.held_dirs`), so its live
    dependents are told; a ``done`` one is told by :func:`_kill_done`.
    """
    global _was_live
    gone = _was_live - alive
    _was_live = set(alive)
    if not gone:
        return
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, project, phase FROM tasks WHERE id IN ({','.join('?' * len(gone))}) "
            "AND status != 'done'",
            list(gone),
        ).fetchall()
        for r in rows:
            if r["project"] is not None:
                messages.notify_lock_released(conn, int(r["id"]), [r["project"]])
            # Handed off to review = finished; anything else ended unfinished.
            if r["phase"] != "review":
                collab.notify(conn, int(r["id"]), "run_ended", None)


def tick() -> None:
    """One pass: kill the done, grant waited-for locks, retire what is finished, start
    what is next. Never raises."""
    global _booted
    from ntasker.settings import (  # noqa: PLC0415 -- lazy: avoid cycle
        get_dir_locks,
        get_queue_enabled,
        get_require_clean,
    )

    enabled = get_queue_enabled()
    live = set(active_session_ids())
    external = set(external_session_ids())
    _kill_done(live, external)
    _notify_ended(live | external)
    # Freed directories go to the sessions queued for them first, before a
    # queued task could start into them (see ntasker.lockqueue).
    with get_conn() as conn:
        lockqueue.grant(conn, live | external)
    worktrees.sweep(live | external)
    rows = load_queue()
    queued_ids = {int(r["id"]) for r in rows}

    # Adopt every queued task that has a session -- ours or an external one --
    # so a run the user started by hand on a queued task advances the queue
    # just like a queued one. Only while the queue is on -- a hand-started run
    # on a paused queue is the user's own business and must not consume its
    # queue entry. Ids that left the queue behind our back (removed, deleted)
    # are forgotten.
    if enabled:
        _running.update(queued_ids & (live | external))
    _running.intersection_update(queued_ids)
    LANELESS.intersection_update(queued_ids)
    FORCE.intersection_update(queued_ids)

    # Finished: ``status=done`` (the user closed it, or the agent did because
    # the task told it to) or archived. The entry leaves the queue; a done
    # task's session is already being killed by ``_kill_done``. A review
    # hand-off is not a finish. A draft or proposal leaves too (a direct DB
    # write can flag a queued row) -- it must never reach the start step.
    retired = [
        int(r["id"])
        for r in rows
        if r["status"] == "done" or r["archived"] or r["draft"] or r["proposed"]
    ]

    if retired:
        with get_conn() as conn:
            _dequeue(conn, retired)
        _running.difference_update(retired)
        rows = [r for r in rows if int(r["id"]) not in retired]

    # Ended without a hand-off (stopped, crashed, blocker): flag the entry and
    # keep it -- it blocks its lane until the user has looked at it. A column,
    # not memory, so a server restart does not silently start it again.
    ended_rows = [
        r for r in rows if int(r["id"]) in _running and int(r["id"]) not in live | external
    ]
    if ended_rows:
        ended = [int(r["id"]) for r in ended_rows]
        # A fasttrack run that ends without ``ntasker finish`` still gets its
        # run-log row -- once per run (``session_ended_at`` is still NULL the
        # first time round). Git runs before the write connection opens. With
        # ``fail_continue`` the entry leaves the queue instead of blocking.
        # A run a usage limit paused is no failure: no run-log row, and it
        # keeps its queue entry to be resumed (see ntasker.usagelimit).
        logged = [
            (r, changed_paths(parse_baselines(r["run_baselines"])))
            for r in ended_rows
            if r["fasttrack"] and r["session_ended_at"] is None and not r["limit_until"]
        ]
        skipped = [
            int(r["id"]) for r in ended_rows if r["fasttrack"] and r["fail_continue"] and not r["limit_until"]
        ]
        with get_conn() as conn:
            for r, paths in logged:
                insert_outcome(conn, r, "ended", ENDED_SUMMARY, report=r["report"], files=paths)
            conn.execute(
                f"UPDATE tasks SET session_ended_at = ? "
                f"WHERE id IN ({','.join('?' * len(ended))}) AND session_ended_at IS NULL",
                [_now(), *ended],
            )
            if skipped:
                _dequeue(conn, skipped)
        _running.difference_update(ended)
        rows = load_queue()

    # After a restart every session is gone: resume the entries flagged ended
    # (by the previous process on shutdown, or by its last tick) rather than
    # starting them over. Once, on the first tick -- a session the user stopped
    # on purpose later must not come back on its own. A run a usage limit
    # paused waits for its reset instead (see :func:`resume_paused`).
    if not _booted:
        _booted = True
        RESUME.update(
            int(r["id"]) for r in rows if r["session_ended_at"] and not r["limit_until"] and resumable(r)
        )
    RESUME.intersection_update(int(r["id"]) for r in rows)
    LIMIT_RESUME.intersection_update(RESUME)

    forced = set(FORCE)
    FORCE.clear()
    if not rows or not enabled:
        return

    # Start: one task per free bucket, head-most first. With directory locks
    # on, a task whose directories a live session holds (or, with
    # ``require_clean``, whose directories are git-dirty) waits as well.
    runnable = _runnable_agents()
    default_agent = resolve_agent_key(None)
    dir_locks = get_dir_locks()
    require_clean = dir_locks and get_require_clean()
    # External sessions occupy their lane and directories like any other; a
    # queued one counts as that entry's run (see :func:`adopt_running`).
    # Laneless Quicktasks occupy nothing (see :data:`LANELESS`).
    occupied = (live | external) - LANELESS
    starts: list[int] = []
    with get_conn() as conn:
        busy = _busy_buckets(occupied, conn)
        # An ended entry waits for the user (or its resume), a paused one for
        # its reset -- either keeps its lane.
        busy |= {
            _bucket(r)
            for r in rows
            if _bucket(r)
            and (r["session_ended_at"] or r["limit_until"])
            and int(r["id"]) not in RESUME
            and int(r["id"]) not in LANELESS
        }
        held = locks.held_dirs(conn, occupied) if dir_locks else {}
        for row in rows:
            task_id = int(row["id"])
            # A live entry runs, an ended one waits for the user (or its
            # resume) -- for a laned task its busy bucket already says so, a
            # laneless or project-less one needs the explicit guard.
            if task_id in live | external or (
                (row["session_ended_at"] or row["limit_until"]) and task_id not in RESUME
            ):
                continue
            laneless = task_id in LANELESS
            # Start now: the user overrides lane, locks and the git check for
            # this one start; the run then occupies them like any other.
            force = task_id in forced
            bucket = _bucket(row)
            if (bucket in busy and not (laneless or force)) or not _startable(
                row, conn, runnable, default_agent
            ):
                continue
            if dir_locks and not laneless:
                # A resume skips the git check: the dirt is that run's own
                # unfinished work, which is exactly what it continues.
                resume = task_id in RESUME
                if not force and _lock_reason(row, held, require_clean and not resume) is not None:
                    continue
                for d in locks.run_dirs(row):
                    held.setdefault(d, task_id)
            starts.append(task_id)
            if bucket and not laneless:
                busy.add(bucket)

    # Spawning happens after the DB context is closed: it forks a process and
    # registers a PTY reader, neither of which should hold a connection open.
    for task_id in starts:
        task = _task_dict(task_id)
        if task is None:
            continue
        quick = task_id in QUICK
        resume = task_id in RESUME
        if task_id in LIMIT_RESUME:
            seed = usagelimit.RESUME_PROMPT
        else:
            seed = None if (quick or resume) else queue_seed_for_task(task)
        try:
            started = start_detached_session(task_id, seed, quick=quick, resume=resume)
        except worktrees.WorktreeError as e:
            # No checkout, no run: flag the entry ended like a crashed run, so
            # it waits for the user instead of retrying git every tick.
            print(f"nTasker: task #{task_id} not started, worktree failed: {e}", file=sys.stderr)
            with get_conn() as conn:
                conn.execute("UPDATE tasks SET session_ended_at = ? WHERE id = ?", (_now(), task_id))
            RESUME.discard(task_id)
            LIMIT_RESUME.discard(task_id)
            continue
        if started:
            QUICK.discard(task_id)
            if resume:
                RESUME.discard(task_id)
                LIMIT_RESUME.discard(task_id)
                clear_ended([task_id])
            else:
                # The loader's "starting work marks it in progress" move. A
                # resume keeps the phase -- the conversation continues as is.
                mark_wip(task_id)
            _running.add(task_id)


async def resume_paused() -> list[int]:
    """Resume every run a usage limit paused whose moment has come; returns their task ids.

    In queue order. First the usage windows are asked once more (see
    :func:`~ntasker.usagelimit.still_limited`): one still at 100 % moves every
    due pause to its reset instead. Then, per task:

    * a live spawned session gets :data:`~ntasker.usagelimit.RESUME_PROMPT`
      typed in;
    * a live external session (``/task`` in a terminal) has no terminal
      nTasker can type into: the prompt goes in as a message, handed over on
      the session's next hook;
    * an ended, queued, resumable run (server restart, closed terminal) is
      reopened with ``claude --resume`` and the prompt, by :func:`tick` --
      lane and lock rules apply as to any start.

    Anything else (removed from the queue, nothing to resume) just stops being
    paused; an ended entry then waits for the user as usual.
    """
    rows = usagelimit.due()
    if not rows:
        return []
    later = usagelimit.still_limited()
    if later is not None:
        for row in rows:
            usagelimit.park(int(row["id"]), later)
        return []
    live = set(active_session_ids())
    external = set(external_session_ids())
    for row in rows:
        task_id = int(row["id"])
        usagelimit.clear(task_id)
        if task_id in live:
            sess = SESSIONS[task_id]
            sess.hook_waiting = False
            sess.turn_over = False
            sess.last_output = time.monotonic()
            await _write_pty(sess, usagelimit.RESUME_PROMPT.encode())
            await asyncio.sleep(0.05)
            await _write_pty(sess, b"\r")
        elif task_id in external:
            with get_conn() as conn:
                messages.post(conn, task_id, usagelimit.RESUME_PROMPT)
        elif row["queue_order"] is not None and resumable(row):
            RESUME.add(task_id)
            LIMIT_RESUME.add(task_id)
    return [int(r["id"]) for r in rows]


def _now() -> str:
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
        # Pending agent-to-agent messages go into sessions whose turn is over.
        with contextlib.suppress(Exception):
            await messages.deliver()
        # Runs a usage limit paused go on once it has reset.
        with contextlib.suppress(Exception):
            await resume_paused()
