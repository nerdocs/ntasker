"""A run's conversation, read from the agent's own session transcript.

The run view's *Conversation* pane shows what the agent was asked and what it
answered -- a readable alternative to the raw terminal. Claude Code writes every
session to ``<home>/projects/<cwd-slug>/<session-id>.jsonl`` (one JSON event per
line); ntasker forces and stores that session id at spawn (see
:mod:`ntasker.claude_runner`), so the file can be located without knowing the
slug scheme.

The events are folded into *turns*: one user prompt plus everything the agent
produced until the next prompt -- its text replies, the tools it called (a short
activity trail) and the tokens it used. The last text of a finished turn is its
*answer*; the texts before it are progress notes. Tool results, meta/sidechain
events and compaction summaries are not turns of their own.

Only agents that store such a transcript are supported (Claude Code today); for
the others :func:`conversation_for` reports ``available=False`` and the UI falls
back to the terminal.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

from ntasker.claude_runner import SESSION_ID_RE

# The queue seed (see claude_runner.queue_seed_for_task) starts with a heading and
# a facts line and ends in the run rules (plus the fasttrack rules). Heading and
# facts are shown as a task card instead; the rules are boilerplate for the agent,
# not the user's question -- both are cut off the first prompt. The caller passes
# the rendered rule texts; the heading is the fallback for rules that were edited
# after the run started.
_SEED_HEAD = "# nTasker task #"
_SEED_RULES = "\n## Tracker rules"

# A slash command lands in the transcript as tagged text; show just the command.
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Wrapped harness output that is not something the user typed.
_NOISE_PREFIXES = ("<local-command", "<system-reminder>", "<bash-", "<task-notification>")

# Keep an activity entry to one readable line.
_DETAIL_MAX = 120

# Tool name -> step kind, for the step summary ("12 files read, 5 edited ...").
# Anything not listed is "other" (MCP tools, TodoWrite, ...).
_TOOL_KINDS = {
    "Read": "read", "NotebookRead": "read",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Bash": "bash", "BashOutput": "bash", "KillShell": "bash",
    "Grep": "search", "Glob": "search", "LS": "search", "ToolSearch": "search",
    "WebFetch": "web", "WebSearch": "web",
    "Agent": "agent", "Task": "agent",
    "Skill": "skill",
    "AskUserQuestion": "ask",
}

# How much of the file end :func:`last_activity` reads -- a few events' worth.
_TAIL_BYTES = 64 * 1024

# Parsed transcripts by path, each with the (mtime_ns, size, rules) it was
# parsed from: the pane polls every few seconds, but the file only changes
# while the agent works. Bounded to the most recently stored entries.
_parsed: dict[Path, tuple[tuple, dict]] = {}
_PARSED_KEEP = 16


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


def _strip_seed(text: str, rules: Iterable[str]) -> tuple[str, bool]:
    """Reduce a queue seed to its body; ``(text, is_seed)``.

    Drops the heading + facts line (the UI shows the task as a card) and the run
    rules. Any other prompt comes back unchanged.
    """
    if not text.startswith(_SEED_HEAD):
        return text, False
    for rule in rules:
        rule = (rule or "").strip()
        if rule and rule in text:
            text = text.replace(rule, "")
    cut = text.find(_SEED_RULES)
    if cut > 0:
        text = text[:cut]
    lines = text.split("\n")
    body = lines[1:]
    # The facts line ("Project: x | Status: open | Priority: normal").
    while body and not body[0].strip():
        body = body[1:]
    if body and "Priority:" in body[0]:
        body = body[1:]
    body_text = "\n".join(body).strip()
    # The card already says it is the task; its description needs no heading.
    if body_text.startswith("## Description"):
        body_text = body_text[len("## Description"):].lstrip()
    return body_text, True


def _one_line(val: str) -> str:
    line = " ".join(val.split())
    return line if len(line) <= _DETAIL_MAX else line[: _DETAIL_MAX - 1] + "…"


def _tool_detail(block: dict) -> str:
    """A one-line hint of what a tool call did (command, file, pattern ...)."""
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "command", "file_path", "notebook_path", "pattern", "path",
                "url", "query", "prompt", "skill"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return _one_line(val)
    return ""


def _questions(inp: dict) -> list[dict]:
    """An ``AskUserQuestion`` call's questions as ``{question, options, multi}``."""
    out = []
    for q in inp.get("questions") or []:
        if not isinstance(q, dict):
            continue
        opts = [o.get("label", "") for o in q.get("options") or [] if isinstance(o, dict)]
        out.append({
            "question": str(q.get("question") or ""),
            "options": [str(o) for o in opts],
            "multi": bool(q.get("multiSelect")),
        })
    return out


def _tool_entry(block: dict) -> dict:
    """One step of the activity trail: ``{id, name, kind, detail, file}``."""
    name = block.get("name") or "?"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    kind = _TOOL_KINDS.get(name, "other")
    file = inp.get("file_path") or inp.get("notebook_path")
    entry = {
        "id": block.get("id"),
        "name": name,
        "kind": kind,
        "detail": _tool_detail(block),
        "file": file if isinstance(file, str) and kind in ("read", "edit") else None,
    }
    if name == "AskUserQuestion":
        entry["questions"] = _questions(inp)
    return entry


def _new_usage() -> dict:
    return {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}


def _add_usage(total: dict, usage: dict) -> None:
    """Fold one API response's ``usage`` into ``total``.

    ``input`` counts every prompt token the model saw -- fresh, written to and
    read from the cache -- so it is comparable across turns; ``cache_read`` is
    the (cheap) share of it served from the cache.
    """
    def num(key: str) -> int:
        val = usage.get(key)
        return val if isinstance(val, int) else 0

    fresh, write, read = num("input_tokens"), num("cache_creation_input_tokens"), num("cache_read_input_tokens")
    total["input"] += fresh + write + read
    total["cache_read"] += read
    total["cache_write"] += write
    total["output"] += num("output_tokens")


def parse_transcript(lines, rules: Iterable[str] = ()) -> list[dict]:
    """Fold transcript events into turns.

    Each turn is a dict with

    * ``prompt`` / ``prompt_at`` -- what was asked, and when; ``seed`` marks the
      queue seed (task card in the UI, heading and rules cut, see
      :func:`_strip_seed`);
    * ``answer`` -- the final text of a finished turn ("" while it runs);
      ``progress`` -- the texts before it (all texts while it runs);
    * ``done`` -- the agent ended the turn (or a later prompt followed);
    * ``tools`` -- the activity trail, see :func:`_tool_entry`; ``pending`` --
      the last tool call without a result yet (awaiting permission, a question
      or just still running), or ``None``;
    * ``usage`` -- tokens, see :func:`_add_usage`; ``answer_at`` -- the last
      agent event's timestamp.

    Agent output before the first prompt is dropped. Malformed lines are
    skipped -- the file is being appended to while we read it. Claude Code
    writes one line per content block and repeats the response's ``usage`` on
    each, so usage is counted once per message id.
    """
    rules = tuple(rules)
    turns: list[dict] = []
    seen_msgs: set[str] = set()
    resolved: set[str] = set()
    texts: list[list[str]] = []
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
        content = msg.get("content")
        if kind == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                        resolved.add(b["tool_use_id"])
            if ev.get("isCompactSummary"):
                continue
            text = _user_text(content)
            if text is None:
                continue
            seed = False
            if not turns:
                text, seed = _strip_seed(text, rules)
            turns.append({
                "prompt": text,
                "prompt_at": ev.get("timestamp"),
                "seed": seed,
                "answer": "",
                "progress": [],
                "done": False,
                "tools": [],
                "pending": None,
                "usage": _new_usage(),
                "answer_at": None,
            })
            texts.append([])
        elif kind == "assistant" and turns:
            turn = turns[-1]
            if not isinstance(content, list):
                continue
            mid = msg.get("id")
            if isinstance(msg.get("usage"), dict) and mid not in seen_msgs:
                if mid:
                    seen_msgs.add(mid)
                _add_usage(turn["usage"], msg["usage"])
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    texts[-1].append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    turn["tools"].append(_tool_entry(block))
            turn["done"] = msg.get("stop_reason") == "end_turn"
            turn["answer_at"] = ev.get("timestamp") or turn["answer_at"]
    for i, turn in enumerate(turns):
        last = i == len(turns) - 1
        if not last:
            turn["done"] = True
        open_calls = [t for t in turn["tools"] if t["id"] and t["id"] not in resolved]
        if last and open_calls:
            turn["done"] = False
            turn["pending"] = open_calls[-1]
        own = texts[i]
        if turn["done"] and own:
            turn["answer"], turn["progress"] = own[-1], own[:-1]
        else:
            turn["progress"] = own
        for t in turn["tools"]:
            t.pop("id", None)
    return turns


def conversation_for(home: Path, session_id: str | None, rules: Iterable[str] = ()) -> dict:
    """``{"available", "turns", "usage", "updated"}`` for a stored session id.

    ``available`` is ``False`` when there is no id or no transcript (yet) -- a
    freshly spawned session writes its file only after the first prompt.
    ``usage`` sums the turns' tokens. ``updated`` is the file's mtime so the
    client can skip unchanged polls. An unchanged file (same mtime and size,
    same rules) is served from :data:`_parsed` without parsing it again.
    """
    empty = {"available": False, "turns": [], "usage": _new_usage(), "updated": None}
    path = find_transcript(home, session_id or "")
    if path is None:
        return empty
    rules = tuple(rules)
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size, rules)
        hit = _parsed.get(path)
        if hit and hit[0] == key:
            return hit[1]
        with path.open(encoding="utf-8", errors="replace") as fh:
            turns = parse_transcript(fh, rules)
    except OSError:
        return empty
    total = _new_usage()
    for turn in turns:
        for k in total:
            total[k] += turn["usage"][k]
    result = {"available": True, "turns": turns, "usage": total, "updated": st.st_mtime}
    _parsed.pop(path, None)
    _parsed[path] = (key, result)
    while len(_parsed) > _PARSED_KEEP:
        del _parsed[next(iter(_parsed))]
    return result


def last_activity(home: Path, session_id: str | None) -> dict | None:
    """What the session did last, for the board card of a running task.

    ``{"name", "kind", "detail", "file"}`` of the last tool call, ``{"text"}`` of the
    last text reply -- whichever came later -- or ``None``. Reads only the file
    end, so polling it for every live session stays cheap.
    """
    path = find_transcript(home, session_id or "")
    if path is None:
        return None
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(tail.splitlines()):
        try:
            ev = json.loads(raw)
        except ValueError:
            continue   # also the first, cut-off line
        if not isinstance(ev, dict) or ev.get("type") != "assistant" or ev.get("isSidechain"):
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                entry = _tool_entry(block)
                return {k: entry[k] for k in ("name", "kind", "detail", "file")}
            if block.get("type") == "text" and (block.get("text") or "").strip():
                first = next(ln for ln in block["text"].strip().splitlines() if ln.strip())
                return {"text": _one_line(first.lstrip("#").strip())}
    return None
