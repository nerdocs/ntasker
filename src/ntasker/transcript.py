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
import shlex
from collections.abc import Iterable
from pathlib import Path

from ntasker.claude_runner import SESSION_ID_RE
from ntasker.messages import FOOTER as _MSG_FOOTER
from ntasker.messages import HEAD_PREFIX as _MSG_HEAD
from ntasker.messages import HEAD_RE as _MSG_HEAD_RE

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

# Claude Code writes an API failure as a synthetic assistant event flagged
# ``isApiErrorMessage`` whose ``error`` names the cause. Mapped to what the user
# has to do about it; anything else is a plain "error" (network, server).
_BLOCKER_KINDS = {
    "rate_limit": "limit",
    "authentication_failed": "auth",
    "billing_error": "billing",
    "invalid_request": "error",
}

# How much of the file end :func:`last_activity` reads -- a few events' worth.
_TAIL_BYTES = 64 * 1024

# Parsed transcripts by path, each with the (mtime_ns, size, rules) it was
# parsed from: the pane polls every few seconds, but the file only changes
# while the agent works. Bounded to the most recently stored entries.
_parsed: dict[Path, tuple[tuple, dict]] = {}
_PARSED_KEEP = 16

# Context window sizes, guessed from the model id when the session's statusline
# has not reported the real one (see ``ntasker hook statusline``): a model id
# with the ``[1m]`` marker runs with the 1M-token window, anything else 200k.
CONTEXT_WINDOW = 200_000
CONTEXT_WINDOW_1M = 1_000_000


def context_window(model: str | None) -> int:
    """Token size of the context window ``model`` runs with (see :data:`CONTEXT_WINDOW`)."""
    return CONTEXT_WINDOW_1M if "1m" in (model or "").lower() else CONTEXT_WINDOW


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


def _typed_mid_turn(ev: dict) -> str | None:
    """The text of a prompt the user typed while the agent worked, or ``None``.

    Claude Code queues such a prompt and hands it to the agent at its next
    step, inside the running turn -- not as a user event but as a
    ``queued_command`` attachment. Background-task notices travel the same
    way; only a human ``prompt`` counts.
    """
    if ev.get("type") != "attachment":
        return None
    att = ev.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command" or att.get("isMeta"):
        return None
    origin = att.get("origin")
    if att.get("commandMode") != "prompt" or not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    return _user_text(att.get("prompt"))


def _peer_message(ev: dict) -> dict | None:
    """A ``SendMessage`` from another Claude session: ``{name, text}``, or ``None``.

    Claude Code records it with ``origin.kind == "peer"`` -- as a (meta) user
    event when it woke an idle session, as a ``queued_command`` attachment when
    it arrived mid-turn. ``origin.body`` is the bare message.
    """
    holder = ev.get("attachment") if ev.get("type") == "attachment" else ev
    origin = holder.get("origin") if isinstance(holder, dict) else None
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return None
    text = origin.get("body")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"name": str(origin.get("name") or origin.get("from") or "?"), "text": text.strip(), "via": "claude"}


def _broker_message(text: str) -> tuple[str, dict] | None:
    """A prompt ntasker typed in for the broker (see :func:`ntasker.messages.render`).

    ``(text without header lines and footer, {name, via})`` -- ``name`` lists
    the senders named in the header lines -- or ``None`` for any other prompt. Claude Code
    records a multi-line paste wrapped in ``<pasted_content id=...>`` tags;
    the wrapper goes.
    """
    m = _PASTE_RE.fullmatch(text.strip())
    if m:
        text = m.group(1).strip()
    if not text.startswith(_MSG_HEAD):
        return None
    names, body = [], []
    for line in text.replace(_MSG_FOOTER, "").splitlines():
        m = _MSG_HEAD_RE.match(line.strip())
        if m is None:
            body.append(line)
        elif m.group(2) not in names:
            names.append(m.group(2))
    return "\n".join(body).strip(), {"name": ", ".join(names), "via": "ntasker"}


_PASTE_RE = re.compile(r'<pasted_content id="[^"]*">\s*(.*?)\s*</pasted_content id="[^"]*">', re.S)

# ``ntasker msg <id> ...`` / ``ntasker wait-for <id> ...`` inside a Bash call.
_SEND_RE = re.compile(r"(?:^|[;&|(]\s*)ntasker\s+(msg|wait-for)\s+#?(\d+)(.*)", re.S)


def _sent_by_bash(command: str) -> dict | None:
    """An outgoing broker message in a Bash call: ``{to, text, kind}``, or ``None``.

    The text is what follows the task id -- unquoted -- or a heredoc's body.
    """
    m = _SEND_RE.search(command or "")
    if not m:
        return None
    verb, to, rest = m.group(1), m.group(2), m.group(3).strip()
    if rest.startswith("<<"):
        lines = rest.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    else:
        try:
            tokens = shlex.split(rest.splitlines()[0] if rest else "")
        except ValueError:
            tokens = rest.split()
        words, skip = [], False
        for tok in tokens:
            if skip:
                skip = False
            elif tok in ("--task", "--host", "--port"):
                skip = True
            elif tok in ("&&", "||", ";", "|"):
                break
            else:
                words.append(tok)
        text = " ".join(words)
    return {"to": f"#{to}", "text": text.strip(), "kind": "wait" if verb == "wait-for" else "note"}


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


def question_entries(questions) -> list[dict]:
    """``AskUserQuestion`` questions as ``{question, header, options, multi}``.

    ``options`` are ``{label, description}``; ``multi`` is the tool's
    ``multiSelect``. Shared by the transcript (the call's input) and the
    ``ask`` hook (the same input, reported live) so both sides agree.
    """
    out = []
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        opts = [
            {"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
            for o in q.get("options") or [] if isinstance(o, dict) and o.get("label")
        ]
        out.append({
            "question": str(q.get("question")),
            "header": str(q.get("header") or ""),
            "options": opts,
            "multi": bool(q.get("multiSelect")),
        })
    return out


def _answers(ev: dict) -> dict | None:
    """What the user answered an ``AskUserQuestion`` with: ``{question: answer}``.

    Claude Code stores the dialog's outcome as the result event's
    ``toolUseResult.answers`` (a multi-select joins its picks with ", "). A
    cancelled dialog has none.
    """
    res = ev.get("toolUseResult")
    answers = res.get("answers") if isinstance(res, dict) else None
    if not isinstance(answers, dict) or not answers:
        return None
    return {str(k): str(v) for k, v in answers.items()}


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
        entry["questions"] = question_entries(inp.get("questions"))
        entry["answers"] = None
    return entry


def _blocker(ev: dict, content) -> dict:
    """What stopped the agent: ``{kind, text, at}`` of an API error event.

    ``kind`` is ``limit`` (usage limit reached), ``auth`` (not logged in / login
    expired), ``billing`` or ``error`` (network, server); ``text`` is the
    agent's own message ("You've hit your session limit · resets 3pm").
    """
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return {
        "kind": _BLOCKER_KINDS.get(str(ev.get("error") or ""), "error"),
        "text": " ".join(p.strip() for p in parts if p and p.strip()),
        "at": ev.get("timestamp"),
    }


def _sent_message(block: dict) -> dict | None:
    """A message to another session in a tool call: ``{to, text, kind, via}``, or ``None``."""
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if block.get("name") == "SendMessage":
        return {"to": str(inp.get("to") or "?"), "text": str(inp.get("message") or ""),
                "kind": "note", "via": "claude"}
    if block.get("name") == "Bash":
        sent = _sent_by_bash(str(inp.get("command") or ""))
        return {**sent, "via": "ntasker"} if sent else None
    return None


def _new_turn(prompt: str, at: str | None, *, seed: bool = False, peer: dict | None = None) -> dict:
    """A fresh turn, see :func:`parse_transcript` for the fields."""
    return {
        "prompt": prompt,
        "prompt_at": at,
        "seed": seed,
        "peer": peer,
        "answer": "",
        "progress": [],
        "done": False,
        "tools": [],
        "pending": None,
        "usage": _new_usage(),
        "context": 0,
        "answer_at": None,
        "blocker": None,
        "injected": [],
        "sent": [],
    }


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
    * ``tools`` -- the activity trail, see :func:`_tool_entry`; a question
      (``AskUserQuestion``) carries its ``questions`` (see
      :func:`question_entries`) and, once answered, ``answers`` (see
      :func:`_answers`); ``pending`` -- the last tool call without a result yet
      (awaiting permission, a question or just still running), or ``None``;
    * ``usage`` -- tokens, see :func:`_add_usage`; ``answer_at`` -- the last
      agent event's timestamp;
    * ``context`` -- tokens in the context window after the turn's last API
      response (its prompt plus output), the figure the Claude Code statusline
      shows; ``0`` before the first response;
    * ``blocker`` -- what keeps the agent from working (usage limit, not logged
      in, API unreachable ...), see :func:`_blocker`, or ``None``. It is the
      turn's last word only: output after it means the work went on;
    * ``injected`` -- ``[{text, at}]``: what the user typed while the turn ran
      and the agent took in mid-turn (see :func:`_typed_mid_turn`); an entry
      with ``peer`` set came from another Claude session (:func:`_peer_message`);
    * ``peer`` -- ``{name, via}`` when the prompt is a message from another
      agent session rather than the user: ``via`` ``claude`` (``SendMessage``,
      :func:`_peer_message`) or ``ntasker`` (the broker, :func:`_broker_message`);
    * ``sent`` -- ``[{to, text, kind, via, at}]``: messages the agent sent other
      sessions in this turn -- ``SendMessage`` calls (``to`` is the address) and
      ``ntasker msg`` / ``wait-for`` (``to`` is ``#<id>``).

    Agent output before the first prompt is dropped. Malformed lines are
    skipped -- the file is being appended to while we read it. Claude Code
    writes one line per content block and repeats the response's ``usage`` on
    each, so usage is counted once per message id.
    """
    rules = tuple(rules)
    turns: list[dict] = []
    seen_msgs: set[str] = set()
    resolved: set[str] = set()
    asked: dict[str, dict] = {}
    texts: list[list[str]] = []
    for raw in lines:
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict) or ev.get("isSidechain"):
            continue
        peer = _peer_message(ev)
        if peer is not None:
            if ev.get("type") == "attachment":
                if turns:
                    turns[-1]["injected"].append(
                        {"text": peer["text"], "at": ev.get("timestamp"), "peer": peer["name"]}
                    )
            else:
                turns.append(_new_turn(peer["text"], ev.get("timestamp"),
                                       peer={"name": peer["name"], "via": "claude"}))
                texts.append([])
            continue
        if ev.get("isMeta"):
            continue
        typed = _typed_mid_turn(ev)
        if typed is not None:
            if turns:
                turns[-1]["injected"].append({"text": typed, "at": ev.get("timestamp")})
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
                        if b["tool_use_id"] in asked:
                            asked[b["tool_use_id"]]["answers"] = _answers(ev)
            if ev.get("isCompactSummary"):
                continue
            text = _user_text(content)
            if text is None:
                continue
            seed = False
            if not turns:
                text, seed = _strip_seed(text, rules)
            broker = _broker_message(text)
            if broker is not None:
                text, from_broker = broker
            turns.append(_new_turn(text, ev.get("timestamp"), seed=seed,
                                   peer=from_broker if broker is not None else None))
            texts.append([])
        elif kind == "assistant" and turns:
            turn = turns[-1]
            if not isinstance(content, list):
                continue
            if ev.get("isApiErrorMessage"):
                # Not an answer -- the agent could not reach the model at all.
                turn["blocker"] = _blocker(ev, content)
                turn["done"] = False
                continue
            turn["blocker"] = None
            mid = msg.get("id")
            if isinstance(msg.get("usage"), dict) and mid not in seen_msgs:
                if mid:
                    seen_msgs.add(mid)
                _add_usage(turn["usage"], msg["usage"])
                ctx = _new_usage()
                _add_usage(ctx, msg["usage"])
                if ctx["input"] + ctx["output"]:
                    turn["context"] = ctx["input"] + ctx["output"]
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    texts[-1].append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    entry = _tool_entry(block)
                    turn["tools"].append(entry)
                    sent = _sent_message(block)
                    if sent is not None:
                        turn["sent"].append({**sent, "at": ev.get("timestamp")})
                    if entry["kind"] == "ask" and entry["id"]:
                        asked[entry["id"]] = entry
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


def conversation_for(home: Path, session_id: str | None, rules: Iterable[str] = (),
                     window: int | None = None) -> dict:
    """``{"available", "turns", "usage", "context", "blocker", "updated"}`` for a stored session id.

    ``available`` is ``False`` when there is no id or no transcript (yet) -- a
    freshly spawned session writes its file only after the first prompt.
    ``usage`` sums the turns' tokens. ``context`` is the session's current
    context fill ``{used, limit}`` -- the last turn's ``context`` against the
    session's ``window`` size (default :data:`CONTEXT_WINDOW`) -- or ``None`` before the
    first response. ``blocker`` is the last turn's (see
    :func:`parse_transcript`) -- what stops the session right now, or ``None``.
    ``updated`` is the file's mtime so the client can skip unchanged polls. An
    unchanged file (same mtime and size, same rules) is served from
    :data:`_parsed` without parsing it again.
    """
    empty = {"available": False, "turns": [], "usage": _new_usage(), "context": None, "blocker": None,
             "updated": None}
    path = find_transcript(home, session_id or "")
    if path is None:
        return empty
    rules = tuple(rules)
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size, rules, window)
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
    blocker = turns[-1]["blocker"] if turns else None
    used = next((t["context"] for t in reversed(turns) if t["context"]), 0)
    context = {"used": used, "limit": window or CONTEXT_WINDOW} if used else None
    result = {"available": True, "turns": turns, "usage": total, "context": context, "blocker": blocker,
              "updated": st.st_mtime}
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


def limit_stop(home: Path, session_id: str | None) -> dict | None:
    """Whether a usage limit stopped the session's work: ``{"text", "at"}``, else ``None``.

    Claude Code says so in two ways, both structured. Near the limit it adds
    a note flagged ``usageLimitNote: "wrap_up"`` -- the agent gets a short
    grace allowance to wrap up, and its turn then ends like any other; the
    note stays in force until a ``"release"`` note follows (the limit has
    reset and the user went on). A turn cut off by the limit leaves an API
    error event (``isApiErrorMessage``, ``error: rate_limit``) whose text names
    the reset ("... resets 3pm"); a later regular reply means it recovered.
    Reads the whole file, but parses only the few lines that can matter.
    """
    path = find_transcript(home, session_id or "")
    if path is None:
        return None
    wrap_up: dict | None = None
    api_error: dict | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if '"usageLimitNote"' not in raw and '"assistant"' not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("isSidechain"):
                    continue
                note = ev.get("usageLimitNote")
                if note == "wrap_up":
                    wrap_up = {"text": "", "at": ev.get("timestamp")}
                elif note == "release":
                    wrap_up = api_error = None
                elif ev.get("type") == "assistant":
                    content = (ev.get("message") or {}).get("content")
                    if ev.get("isApiErrorMessage") and ev.get("error") == "rate_limit":
                        api_error = _blocker(ev, content if isinstance(content, list) else [])
                    elif not ev.get("isApiErrorMessage"):
                        api_error = None
    except OSError:
        return None
    if api_error is not None:
        return {"text": api_error["text"], "at": api_error["at"]}
    return wrap_up
