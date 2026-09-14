"""Directory locks -- which working directories a task's run holds.

A run always holds its own project's directory. A task may additionally lock
other projects' directories (``tasks.locks``, a JSON list of project names)
when its work spans repos. The queue worker (:mod:`ntasker.taskqueue`) refuses
to start a task while a live session of *another* task holds any of its
directories, and -- with ``require_clean`` on -- while any of them is
git-dirty. The Claude Code ``PreToolUse`` hook (``ntasker hook pretooluse``)
asks :func:`project_for_path` via ``GET /api/locks/check`` whether an edit
target lies in a directory the task does not hold.

The lock key is the **resolved directory** (:func:`resolve_dir`), not the
project name, so two names mapping to one path collide as they should.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile

from ntasker.claude_runner import default_cwd_for_project


def parse(raw: str | None) -> list[str]:
    """Decode a stored ``locks`` column; anything unreadable is ``[]``."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [str(p) for p in data] if isinstance(data, list) else []


def dump(projects: list[str]) -> str:
    """Encode a lock list for the ``locks`` column."""
    return json.dumps(list(projects))


def normalize(projects: list[str] | None, own_project: str | None) -> list[str]:
    """Strip, drop empties and the task's own project, dedupe (order kept)."""
    out: list[str] = []
    for name in projects or []:
        name = (name or "").strip()
        if not name or name == own_project or name in out:
            continue
        out.append(name)
    return out


def resolve_dir(project: str) -> str:
    """The directory a project name stands for, realpath'd. Never creates it."""
    return os.path.realpath(default_cwd_for_project(project) or "")


def task_dirs(project: str | None, locks: list[str]) -> set[str]:
    """Every directory a task's run holds: its own project's plus its locks.

    A task without a project holds only its explicit locks.
    """
    names = [*([project] if project else []), *locks]
    return {resolve_dir(n) for n in names}


def held_dirs(conn: sqlite3.Connection, live_ids: set[int]) -> dict[str, int]:
    """Directory -> holder task id, over every *open* task with a live session.

    A done task's session may still be alive (nTasker never ends a session),
    but it no longer holds anything.
    """
    if not live_ids:
        return {}
    placeholders = ",".join("?" * len(live_ids))
    rows = conn.execute(
        f"SELECT id, project, locks FROM tasks "
        f"WHERE id IN ({placeholders}) AND status != 'done'",
        list(live_ids),
    ).fetchall()
    held: dict[str, int] = {}
    for row in rows:
        for d in task_dirs(row["project"], parse(row["locks"])):
            held.setdefault(d, int(row["id"]))
    return held


def conflict(
    dirs: set[str], held: dict[str, int], *, exclude: int | None = None
) -> tuple[str, int] | None:
    """First ``(dir, holder)`` in ``dirs`` held by a task other than ``exclude``."""
    for d in sorted(dirs):
        holder = held.get(d)
        if holder is not None and holder != exclude:
            return d, holder
    return None


def dirty_dir(dirs: set[str]) -> str | None:
    """First directory with uncommitted git changes, or ``None``.

    Not a repo, git missing, or any git error counts as clean -- the gate is
    about not clobbering someone's work in progress, not about enforcing git.
    """
    for d in sorted(dirs):
        try:
            out = subprocess.run(
                ["git", "-C", d, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return d
    return None


def known_projects(conn: sqlite3.Connection) -> set[str]:
    """Project names the tracker knows: from tasks plus discovered agent projects."""
    from ntasker.projects import discover_claude_projects  # noqa: PLC0415 -- lazy: avoid cycle

    names = {
        r["project"]
        for r in conn.execute("SELECT DISTINCT project FROM tasks WHERE project IS NOT NULL")
    }
    with_discovered = names | set(discover_claude_projects())
    return {n for n in with_discovered if n}


def _never_project_dirs() -> set[str]:
    """Directories that are scratch space, not a project, even if an agent was
    once launched there: the home directory and the system temp dir. Treating
    either as a project would fence off plan files, temp files -- everything."""
    return {os.path.realpath(os.path.expanduser("~")), os.path.realpath(tempfile.gettempdir())}


def project_for_path(conn: sqlite3.Connection, path: str) -> str | None:
    """The known project whose directory contains ``path`` (longest match)."""
    target = os.path.realpath(path)
    skip = _never_project_dirs()
    best: tuple[int, str] | None = None
    for name in known_projects(conn):
        d = resolve_dir(name)
        if d in skip:
            continue
        if target == d or target.startswith(d.rstrip(os.sep) + os.sep):
            if best is None or len(d) > best[0]:
                best = (len(d), name)
    return best[1] if best else None
