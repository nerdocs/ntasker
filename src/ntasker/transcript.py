"""A run's conversation, read from the agent's own session transcript.

The run view's *Conversation* pane shows what the agent was asked and what it
answered -- a readable alternative to the raw terminal. Claude Code writes every
session to ``<home>/projects/<cwd-slug>/<session-id>.jsonl`` (one JSON event per
line); ntasker forces and stores that session id at spawn (see
:mod:`ntasker.claude_runner`), so the file can be located without knowing the
slug scheme.

The events are folded into *turns*: one user prompt plus everything the agent
produced until the next prompt -- its text replies (the answer) and the tools it
called (a short activity trail). Tool results, meta/sidechain events and
compaction summaries are not turns of their own.

Only agents that store such a transcript are supported (Claude Code today); for
the others :func:`conversation_for` reports ``available=False`` and the UI falls
back to the terminal.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from ntasker.claude_runner import SESSION_ID_RE

# The queue seed (see claude_runner.queue_seed_for_task) ends in the run rules
# (plus the fasttrack rules). They are boilerplate for the agent, not the user's
# question -- cut them off the first prompt so the pane shows the task itself.
# The caller passes the rendered rule texts; the heading is the fallback for
# rules that were edited after the run started.
_SEED_HEAD = "# nTasker task #"
_SEED_RULES = "\n## Tracker rules"

# A slash command lands in the transcript as tagged text; show just the command.
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Wrapped harness output that is not something the user typed.
_NOISE_PREFIXES = ("<local-command", "<system-reminder>", "<bash-", "<task-notification>")

# Keep an activity entry to one readable line.
_DETAIL_MAX = 120


def find_transcript(home: Path, session_id: str) -> Path | None:
    """The session's ``.jsonl`` under ``<home>/projects``, or ``None``.

    The project folder is a slug of the run's cwd; globbing by the (unique)
    session id sidesteps reproducing the slug rules.
    """
    if not session_id or not re.match(SESSION_ID_RE, session_id):
        return None
    projects = home / "projects"
    if not projects.is_dir():
        return None
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def _user_text(content) -> str | None:
    """A user event's typed text, or ``None`` for tool results / harness noise."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    text = "\n\n".join(p for p in parts if p).strip()
    if not text:
        return None
    m = _COMMAND_RE.search(text)
    if m:
        args = _COMMAND_ARGS_RE.search(text)
        return (m.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
    if text.startswith(_NOISE_PREFIXES):
        return None
    return text


def _strip_seed(text: str, rules: Iterable[str]) -> str:
    """Drop the queue seed's run rules from a first prompt."""
    if not text.startswith(_SEED_HEAD):
        return text
    for rule in rules:
        rule = (rule or "").strip()
        if rule and rule in text:
            text = text.replace(rule, "")
    cut = text.find(_SEED_RULES)
    if cut > 0:
        text = text[:cut]
    return text.rstrip()


def _tool_detail(block: dict) -> str:
    """A one-line hint of what a tool call did (command, file, pattern ...)."""
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "command", "file_path", "pattern", "path", "url", "query", "prompt", "skill"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            line = " ".join(val.split())
            return line if len(line) <= _DETAIL_MAX else line[: _DETAIL_MAX - 1] + "…"
    return ""


def parse_transcript(lines, rules: Iterable[str] = ()) -> list[dict]:
    """Fold transcript events into turns.

    Each turn is ``{"prompt", "prompt_at", "answer", "tools", "answer_at"}``:
    ``answer`` is the agent's text replies joined as Markdown, ``tools`` a list of
    ``{"name", "detail"}`` in call order, ``answer_at`` the last agent event's
    timestamp. Agent output before the first prompt is dropped. Malformed lines
    are skipped -- the file is being appended to while we read it. ``rules``
    are the run-rule texts to cut from a queue seed (see :func:`_strip_seed`).
    """
    rules = tuple(rules)
    turns: list[dict] = []
    for raw in lines:
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict) or ev.get("isSidechain") or ev.get("isMeta"):
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        kind = ev.get("type")
        if kind == "user":
            if ev.get("isCompactSummary"):
                continue
            text = _user_text(msg.get("content"))
            if text is None:
                continue
            if not turns:
                text = _strip_seed(text, rules)
            turns.append({
                "prompt": text,
                "prompt_at": ev.get("timestamp"),
                "answer": "",
                "tools": [],
                "answer_at": None,
            })
        elif kind == "assistant" and turns:
            turn = turns[-1]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    sep = "\n\n" if turn["answer"] else ""
                    turn["answer"] += sep + block["text"].strip()
                elif block.get("type") == "tool_use":
                    turn["tools"].append({"name": block.get("name") or "?", "detail": _tool_detail(block)})
            turn["answer_at"] = ev.get("timestamp") or turn["answer_at"]
    return turns


def conversation_for(home: Path, session_id: str | None, rules: Iterable[str] = ()) -> dict:
    """``{"available", "turns", "updated"}`` for a stored session id.

    ``available`` is ``False`` when there is no id or no transcript (yet) -- a
    freshly spawned session writes its file only after the first prompt.
    ``updated`` is the file's mtime so the client can skip unchanged polls.
    """
    path = find_transcript(home, session_id or "")
    if path is None:
        return {"available": False, "turns": [], "updated": None}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            turns = parse_transcript(fh, rules)
        updated = path.stat().st_mtime
    except OSError:
        return {"available": False, "turns": [], "updated": None}
    return {"available": True, "turns": turns, "updated": updated}
