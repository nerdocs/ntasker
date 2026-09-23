"""Claude Code sessions that ntasker did not start -- finding and adopting them.

A conversation begun in a terminal leaves two traces ntasker can use:

* its **transcript**, ``<claude_home>/projects/<cwd-slug>/<session-id>.jsonl``.
  The file name *is* the session id ``claude --resume`` takes, and the first
  lines carry the working directory and the first thing the user typed. That
  is enough to list past sessions and hand one to a task -- see
  :func:`discover_for_project`.
* while it runs, a **process**. Nothing in the transcript names it, so the
  session has to report it: with the ``session_discovery`` setting on, ntasker
  installs a ``SessionStart`` / ``UserPromptSubmit`` hook into the user's own
  Claude Code settings which calls ``ntasker hook session`` (see
  :mod:`ntasker.cli`). That fills :data:`LIVE` -- and only a session in there
  can be ended from the board.

Everything here is read-only discovery plus one kill; binding a session to a
task is :func:`ntasker.claude_runner.bind_session`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ntasker.claude_runner import SESSION_ID_RE, _pid_alive
from ntasker.projects import discover_claude_project_dirs

#: Lines read from a transcript before giving up on its metadata. The working
#: directory shows up within the first handful; the first user message follows
#: shortly after. The cap bounds the read for a pathological file.
_SCAN_LINES = 60

#: Longest preview text kept per session -- enough to recognise a conversation
#: in a list, short enough not to bloat the payload.
_PREVIEW_CHARS = 140

#: Most recent transcripts inspected per project. A long-lived project
#: accumulates hundreds; nobody picks up a session from months ago.
_MAX_PER_PROJECT = 25


@dataclass
class LiveSession:
    """A terminal session that reported itself while starting or prompting."""

    pid: int
    cwd: str
    #: Wall-clock of the first report -- what the board shows as "running since".
    seen_at: float = field(default_factory=time.time)


#: Live terminal sessions by session id, filled by ``ntasker hook session``.
#: In-memory on purpose: it mirrors running processes, and the ``UserPromptSubmit``
#: half of the hook refills it after a server restart on the next thing the
#: user types. A session missing here is still adoptable -- just not endable.
LIVE: dict[str, LiveSession] = {}


def register_live(session_id: str, pid: int, cwd: str) -> None:
    """Record a running terminal session. Repeat reports just refresh it."""
    known = LIVE.get(session_id)
    LIVE[session_id] = LiveSession(pid=pid, cwd=cwd, seen_at=known.seen_at if known else time.time())


def live_pid(session_id: str) -> int | None:
    """The session's process id while it is alive; ``None`` once it is gone."""
    entry = LIVE.get(session_id)
    if entry is None:
        return None
    if not _pid_alive(entry.pid):
        del LIVE[session_id]
        return None
    return entry.pid


#: How long :func:`end_live` waits for the process to actually be gone. The
#: caller usually resumes the session next, and two Claude Code processes on
#: one transcript would fight over it -- so the wait is part of ending, not
#: something every caller has to reinvent.
_END_TIMEOUT = 5.0
_END_POLL = 0.1


def end_live(session_id: str) -> bool:
    """Ask a running terminal session to exit, so its task can resume it.

    ``SIGTERM``, not ``SIGKILL``: Claude Code gets to shut down on its own
    terms. The transcript is written as the conversation goes, so it is
    complete up to the last message either way. Returns once the process is
    gone, or after :data:`_END_TIMEOUT` -- a session that ignores the signal
    is reported as still running rather than silently left behind. ``False``
    when no live process is known for this id (already ended, or never
    reported because discovery is off).
    """
    pid = live_pid(session_id)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        LIVE.pop(session_id, None)
        return False
    deadline = time.monotonic() + _END_TIMEOUT
    while time.monotonic() < deadline:
        if live_pid(session_id) is None:
            return True
        time.sleep(_END_POLL)
    return False


@dataclass
class DiscoveredSession:
    """One transcript on disk, plus what ntasker knows about it."""

    session_id: str
    cwd: str | None
    started: str | None
    last_active: float
    preview: str
    #: Process id while the session is still running, else ``None``.
    pid: int | None = None
    #: Task already pointing at this session, if any -- the UI marks it instead
    #: of offering it a second time.
    task_id: int | None = None

    def as_dict(self) -> dict:
        # ``last_active`` goes out as UTC ISO like every other timestamp in the
        # API, so the frontend formats it with the same helper.
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "started": self.started,
            "last_active": datetime.fromtimestamp(self.last_active, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "preview": self.preview,
            "live": self.pid is not None,
            "task_id": self.task_id,
        }


def _text_of(content) -> str:
    """First plain text in a message's ``content`` (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text") or "")
    return ""


def _clean_preview(text: str) -> str:
    """Condense a first user message into one recognisable line.

    Seeds and slash commands arrive wrapped in tags and Markdown; what the
    user recognises is the sentence, so the wrapping goes and the rest is cut
    to :data:`_PREVIEW_CHARS`.
    """
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^[#>*\-\s]+", "", text)
    text = " ".join(text.split())
    return text[:_PREVIEW_CHARS].strip()


def _read_transcript(path: Path) -> tuple[str | None, str | None, str]:
    """``(cwd, started, preview)`` from the head of a transcript.

    Reads line by line and stops as soon as both the working directory and a
    first user message are known -- a transcript grows into the megabytes and
    only its opening matters here.
    """
    cwd: str | None = None
    started: str | None = None
    preview = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > _SCAN_LINES or (cwd and preview):
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if cwd is None and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                # Sidechains are subagent turns, not what the user typed.
                if not preview and obj.get("type") == "user" and not obj.get("isSidechain"):
                    message = obj.get("message")
                    if isinstance(message, dict):
                        preview = _clean_preview(_text_of(message.get("content")))
                        started = obj.get("timestamp") or started
    except OSError:
        return None, None, ""
    return cwd, started, preview


def _bound_tasks(session_ids: list[str]) -> dict[str, int]:
    """Session id -> task already pointing at it. Empty on any DB trouble."""
    if not session_ids:
        return {}
    try:
        from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle

        placeholders = ",".join("?" * len(session_ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, session_id FROM tasks WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchall()
        return {row["session_id"]: row["id"] for row in rows}
    except Exception:  # noqa: BLE001 -- listing must not fail on a DB hiccup
        return {}


def discover_for_project(
    project: str | None, claude_home: str | os.PathLike | None = None
) -> list[DiscoveredSession]:
    """Terminal sessions recorded for a project, newest first.

    A project can span several working directories (its root and any
    subdirectory a session was started in), each with its own transcript
    folder -- :func:`ntasker.projects.discover_claude_project_dirs` already
    maps project names to exactly those folders. ``None`` lists nothing: a
    cross-project task has no directory to look in.
    """
    if not project:
        return []
    dirs = discover_claude_project_dirs(claude_home).get(project) or []
    found: list[tuple[float, Path]] = []
    for transcript_dir, _project_dir in dirs:
        with contextlib.suppress(OSError):
            for entry in transcript_dir.glob("*.jsonl"):
                if re.fullmatch(SESSION_ID_RE, entry.stem):
                    found.append((entry.stat().st_mtime, entry))
    found.sort(key=lambda item: item[0], reverse=True)
    sessions = []
    for mtime, path in found[:_MAX_PER_PROJECT]:
        cwd, started, preview = _read_transcript(path)
        if not preview:
            continue  # nothing was ever asked -- an empty session helps nobody
        sessions.append(
            DiscoveredSession(
                session_id=path.stem,
                cwd=cwd,
                started=started,
                last_active=mtime,
                preview=preview,
                pid=live_pid(path.stem),
            )
        )
    bound = _bound_tasks([s.session_id for s in sessions])
    for session in sessions:
        session.task_id = bound.get(session.session_id)
    return sessions
