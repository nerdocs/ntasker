"""The auto-run task queue -- an ordered worklist ntasker works through itself.

A task is *queued* when its ``queue_order`` is not NULL; queued tasks are worked
top-down (``queue_order ASC``). :func:`worker` ticks a few times a second and
does exactly two things:

* **retire** entries that are finished -- handed off to ``phase=review`` by
  their own run, ``status=done``, archived, deleted, or whose agent session has
  ended (whoever moved the task, from the UI, the CLI or the agent itself, is
  irrelevant: the DB is the single source of truth);
* **start** the head-most startable task of every project that has no live
  session yet -- one concurrent run per project, so several projects progress in
  parallel while a single project stays strictly sequential.

Session ended but the task is still open (agent stopped, crashed, hit a blocker)?
The entry is retired anyway and the queue moves on. A task that cannot finish
must not wedge the queue behind it -- it keeps its phase and stays on the board.

A queued run gets its own seed (:func:`~ntasker.claude_runner.queue_seed_for_task`)
which tells the agent to hand the finished task to ``phase=review`` unprompted.
That hand-off is what advances the queue -- closing stays the user's call, so a
queue run leaves its results in the review column instead of closing them out.

The whole thing is off until the user switches it on (``queue_enabled``), so
dropping tasks in and sorting them never launches an agent by accident.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3

from ntasker.agents import agent_keys, get_spec, resolve_agent_key
from ntasker.claude_runner import (
    active_session_ids,
    default_cwd_for_project,
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
        return conn.execute(
            "SELECT * FROM tasks WHERE queue_order IS NOT NULL ORDER BY queue_order ASC"
        ).fetchall()


def _dequeue(conn: sqlite3.Connection, ids: list[int]) -> None:
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE tasks SET queue_order = NULL WHERE id IN ({placeholders})", ids
    )


def _busy_buckets(live: set[int], conn: sqlite3.Connection) -> set[str]:
    """Buckets occupied by a live session -- queued or started by hand.

    A manually opened session in a project blocks the queue there too: two
    agents in one working directory is exactly what the one-per-project rule
    exists to prevent.
    """
    if not live:
        return set()
    placeholders = ",".join("?" * len(live))
    rows = conn.execute(
        f"SELECT project FROM tasks WHERE id IN ({placeholders})", list(live)
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


def tick() -> None:
    """One pass: retire what is finished, start what is next. Never raises."""
    from ntasker.settings import get_queue_enabled  # noqa: PLC0415 -- lazy: avoid cycle

    enabled = get_queue_enabled()
    live = set(active_session_ids())
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

    # Handed off: the run we started moved its task to review -- that is a
    # queued run's "finished" signal. Only for tasks we are running, so queueing
    # a task that already sits in review still gets it worked on.
    handed_off = {
        int(r["id"]) for r in rows if int(r["id"]) in _running and r["phase"] == "review"
    }

    # Retire: handed off, closed, archived, or the run is over -- finished or not.
    retired = [
        int(r["id"])
        for r in rows
        if int(r["id"]) in handed_off
        or r["status"] == "done"
        or r["archived"]
        or (int(r["id"]) in _running and int(r["id"]) not in live)
    ]
    if retired:
        with get_conn() as conn:
            _dequeue(conn, retired)
        # A hand-off leaves the task open, so nothing else tears its session
        # down -- and a live session keeps its project bucket busy. Stop it here
        # or the next task of that project would never start.
        for task_id in handed_off:
            stop_session(task_id)
        _running.difference_update(retired)
        rows = [r for r in rows if int(r["id"]) not in retired]

    if not rows or not enabled:
        return

    # Start: one task per free bucket, head-most first.
    runnable = _runnable_agents()
    default_agent = resolve_agent_key(None)
    starts: list[tuple[int, str | None]] = []
    with get_conn() as conn:
        busy = _busy_buckets(live, conn)
        for row in rows:
            bucket = _bucket(row["project"])
            if bucket in busy or not _startable(row, conn, runnable, default_agent):
                continue
            starts.append((int(row["id"]), row["project"]))
            busy.add(bucket)

    # Spawning happens after the DB context is closed: it forks a process and
    # registers a PTY reader, neither of which should hold a connection open.
    for task_id, project in starts:
        task = _task_dict(task_id)
        if task is None:
            continue
        started = start_detached_session(
            task_id,
            default_cwd_for_project(project),
            queue_seed_for_task(task),
        )
        if started:
            # Unconditionally, unlike the spawn path's own compact-seed-only
            # call: retiring keys on ``phase=review``, so a task queued while it
            # sits in review has to leave that phase the moment it starts.
            mark_wip(task_id)
            _running.add(task_id)


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
