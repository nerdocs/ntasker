"""Inbox triage -- one stateless ``claude -p`` call per raw note.

The user drops a raw idea into the inbox (topbar field, ``ntasker in``,
``POST /api/inbox``). :func:`worker` picks up one ``pending`` row per tick and
turns it into a *proposed* task (``tasks.proposed = 1``): title, prompt,
priority, tags and -- from a catalog of one-paragraph project summaries -- the
project. The user confirms with one click; a corrected project becomes a
few-shot example for the next triage (``triage_examples``).

Why a dedicated module instead of :class:`ntasker.agents.AgentSpec`: that
class models interactive spawns (permission flags, session id, hooks, seed).
The triage needs six ``-p``-only flags and nothing of that, so it borrows only
:func:`ntasker.agents.resolve_binary` and the agent's ``strip_env`` list.

The call (see :func:`_argv`):

* ``--tools ""`` -- nothing executes; the only bound needed is the timeout.
* ``--json-schema`` + ``--output-format json`` -- the result object carries
  ``structured_output`` (parsed) next to ``result`` (string). Parsed first,
  ``json.loads(result)`` as fallback, then re-validated by :func:`parse_result`.
* ``--system-prompt`` (replace) -- with no tools the Claude Code coding persona
  is noise, and a fully owned prompt is a stable cache prefix.
* ``--no-session-persistence`` and a neutral cwd (the temp dir) -- no transcript
  lands under ``~/.claude/projects``.
* ``NTASKER_TASK_ID=inbox`` in the env -- the user's ``session_discovery`` hooks
  fire in ``-p`` mode too; the hook returns early when that variable is set, so
  no phantom live session is registered.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import tempfile
from itertools import islice
from pathlib import Path

from ntasker import locks
from ntasker.agents import get_spec, resolve_binary
from ntasker.db import get_conn, normalize_tags, set_task_tags, title_from_description
from ntasker.projects import discover_claude_project_dirs, stale_claude_projects
from ntasker.settings import get_triage_enabled, get_triage_model

TRIAGE_TICK = 2.0
"""Seconds between two worker ticks."""

TIMEOUT = 120.0
"""Seconds one ``claude -p`` call may take before the row is marked failed."""

PRIORITIES = ("critical", "high", "normal", "low")
MAX_CANDIDATES = 3
MAX_EXAMPLES = 20
DOC_LINES = 80
"""Lines read from the top of CLAUDE.md / README.md for a project summary."""
DIR_ENTRIES = 40
"""Top-level directory entries listed for a project summary."""

ORIGINAL_HEADING = "## Original"
"""Heading under which the raw note is appended to a proposal's description."""

SENTINELS = frozenset({"__none__", "__null__"})


class TriageError(RuntimeError):
    """The triage call failed or returned something unusable."""


# ---------------------------------------------------------------------------
# Catalog + summaries
# ---------------------------------------------------------------------------
def catalog() -> list[tuple[str, str | None]]:
    """``[(project, summary | None)]`` -- the projects the triage may choose from.

    Task-derived and discovered projects, minus hidden ones (the user's veto),
    stale ones and names without an existing directory; sorted casefold so
    the prompt is stable.
    """
    with get_conn() as conn:
        names = locks.known_projects(conn)
        hidden = {r["project"] for r in conn.execute("SELECT project FROM hidden_projects")}
        summaries = {
            r["project"]: r["summary"]
            for r in conn.execute("SELECT project, summary FROM project_summaries")
        }
    stale = stale_claude_projects(discover_claude_project_dirs())
    keep = [
        n
        for n in names
        if n not in hidden
        and n not in stale
        and n not in SENTINELS
        and os.path.isdir(locks.resolve_dir(n))
    ]
    return [(n, summaries.get(n)) for n in sorted(keep, key=str.casefold)]


def set_summary(name: str, summary: str | None) -> None:
    """Store (or, with an empty *summary*, delete) a project's summary."""
    text = (summary or "").strip()
    with get_conn() as conn:
        if text:
            conn.execute(
                "INSERT INTO project_summaries (project, summary, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(project) DO UPDATE SET summary = excluded.summary, "
                "updated_at = excluded.updated_at",
                (name, text),
            )
        else:
            conn.execute("DELETE FROM project_summaries WHERE project = ?", (name,))


def _head(path: Path) -> str:
    """The first :data:`DOC_LINES` lines of *path*, empty when unreadable."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return "".join(islice(fh, DOC_LINES))
    except OSError:
        return ""


def _project_material(name: str) -> str:
    """What the summary call gets to read: top of CLAUDE.md and README.md
    plus the top-level listing, so a doc-less repo still yields a sentence."""
    root = Path(locks.resolve_dir(name))
    if not root.is_dir():
        raise TriageError(f"project {name!r} has no directory")
    parts = [f"Project name: {name}"]
    for doc in ("CLAUDE.md", "README.md"):
        text = _head(root / doc) if (root / doc).is_file() else ""
        parts.append(f"## {doc}\n\n{text.strip() or '(missing)'}")
    try:
        entries = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
    except OSError:
        entries = []
    listing = "\n".join(entries[:DIR_ENTRIES]) or "(empty)"
    parts.append(f"## Top-level directory\n\n{listing}")
    return "\n\n".join(parts)


SUMMARY_SYSTEM_PROMPT = (
    "You write one-paragraph summaries of software projects for a task router. "
    "Given a project's name, the top of its CLAUDE.md and README.md and its "
    "top-level directory listing, describe in 2-4 sentences what the project is, "
    "what it is built with and what kind of work belongs to it. Plain text, no "
    "Markdown, no preamble. Answer only through the structured output."
)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "minLength": 1}},
    "required": ["summary"],
}


def summarize_project(name: str) -> str:
    """Generate the project's summary with ``claude -p``, store and return it.

    Raises :class:`TriageError` when the project has no directory or the call
    fails; nothing is stored then.
    """
    material = _project_material(name)
    obj = run_claude(_argv(SUMMARY_SYSTEM_PROMPT, SUMMARY_SCHEMA), material)
    summary = obj.get("summary") if isinstance(obj, dict) else None
    if not isinstance(summary, str) or not summary.strip():
        raise TriageError("schema: summary missing")
    set_summary(name, summary)
    return summary.strip()


# ---------------------------------------------------------------------------
# Prefix, prompt, schema
# ---------------------------------------------------------------------------
def split_prefix(text: str, names: list[str]) -> tuple[str | None, str]:
    """Peel a leading project override off *text*.

    ``#<name> rest`` or ``<name>: rest`` where ``<name>`` (no whitespace)
    matches a catalog name case-insensitively -> ``(name, rest)``; anything
    else -> ``(None, text)``. Slashes in names are fine (``medux/online: ...``).
    """
    stripped = text.strip()
    head, _sep, rest = stripped.partition(" ")
    token: str | None = None
    if head.startswith("#") and len(head) > 1:
        token = head[1:]
    elif head.endswith(":") and len(head) > 1:
        token = head[:-1]
    if token is None:
        return None, text
    folded = token.casefold()
    for name in names:
        if name.casefold() == folded:
            return name, rest.strip()
    return None, text


def system_prompt(cat: list[tuple[str, str | None]], examples: list[tuple[str, str | None]]) -> str:
    """The triage system prompt: rules, the catalog, the user's corrections.

    Built from stable inputs (catalog sorted, examples in insertion order) so
    the prompt only changes when a summary or an example changes and the
    prompt cache stays warm.
    """
    lines = [
        "You are nTasker's inbox triage. The user typed a raw note; turn it into one",
        "well-formed task proposal. Answer only through the structured output -- no prose.",
        "",
        "Rules",
        "- title: imperative, <= 70 characters, no trailing period.",
        "- prompt: what an AI coding agent should do, as Markdown; keep the user's intent",
        "  and wording, add structure, add nothing the note does not say. Unclear points go",
        '  into "question", never invented into the prompt.',
        "- project: exactly one name from the catalog below, or null when the note spans",
        "  projects or fits none. Pick by the summaries; a project name mentioned in the",
        "  note wins. Do not invent names.",
        "- candidates: up to 3 plausible projects, best first, each with a one-line reason.",
        '  The first must equal "project" (or the list is empty when project is null).',
        "- priority: critical only for outages/data loss, high for bugs and blockers,",
        "  normal by default, low for nice-to-haves.",
        "- tags: 0-3 short lowercase words already common for such tasks (bug, feature,",
        "  docs, refactor, ...).",
        "- confidence: 0..1 for the project choice.",
        "- question: a single clarifying question when the note is too vague to act on,",
        "  else null.",
        "",
        "Catalog",
    ]
    for name, summary in cat:
        lines.append(f"- {name}: {summary or '(no summary)'}")
    if examples:
        lines += ["", "Examples of the user's corrections (note -> project)"]
        for text, project in examples:
            note = " ".join(text.split())
            lines.append(f'- "{note}" -> {project or "cross-project"}')
    return "\n".join(lines) + "\n"


def _schema(names: list[str]) -> dict:
    enum = [*names, None]
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "prompt": {"type": "string"},
            "project": {"enum": enum},
            "candidates": {
                "type": "array",
                "maxItems": MAX_CANDIDATES,
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {"enum": names},
                        "reason": {"type": "string"},
                    },
                    "required": ["project", "reason"],
                },
            },
            "priority": {"enum": list(PRIORITIES)},
            "tags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "question": {"type": ["string", "null"]},
        },
        "required": [
            "title",
            "prompt",
            "project",
            "candidates",
            "priority",
            "tags",
            "confidence",
            "question",
        ],
    }


# ---------------------------------------------------------------------------
# The subprocess call
# ---------------------------------------------------------------------------
def _argv(system: str, schema: dict) -> list[str]:
    """``claude -p`` argv for one structured call; the user message goes on stdin."""
    binary = resolve_binary(get_spec("claude"))
    if binary is None:
        raise TriageError("claude CLI not found")
    return [
        binary,
        "-p",
        "--model",
        get_triage_model(),
        "--tools",
        "",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--system-prompt",
        system,
        "--no-session-persistence",
    ]


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in get_spec("claude").strip_env}
    env["NTASKER_TASK_ID"] = "inbox"
    return env


def run_claude(argv: list[str], stdin: str) -> dict:
    """Run one ``claude -p`` call and return its structured result.

    The single subprocess wrapper -- tests monkeypatch this. Raises
    :class:`TriageError` on a non-zero exit, a timeout, unparsable output or
    an ``is_error`` result.
    """
    try:
        proc = subprocess.run(  # argv is built here, the note goes on stdin
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=tempfile.gettempdir(),
            env=_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TriageError(f"timeout after {TIMEOUT:g}s") from exc
    except OSError as exc:
        raise TriageError(str(exc)) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise TriageError("\n".join(tail) or f"claude exited with {proc.returncode}")
    try:
        obj = json.loads(proc.stdout)
    except ValueError as exc:
        raise TriageError(f"unparsable output: {proc.stdout[:200]!r}") from exc
    if not isinstance(obj, dict):
        raise TriageError("unexpected output shape")
    if obj.get("is_error"):
        raise TriageError(str(obj.get("result") or obj.get("subtype") or "claude error"))
    structured = obj.get("structured_output")
    if isinstance(structured, dict):
        return structured
    try:
        parsed = json.loads(obj.get("result") or "")
    except ValueError as exc:
        raise TriageError("no structured output") from exc
    if not isinstance(parsed, dict):
        raise TriageError("no structured output")
    return parsed


# ---------------------------------------------------------------------------
# Result validation
# ---------------------------------------------------------------------------
def parse_result(obj: object, names: list[str]) -> dict:
    """Re-check everything the schema promises and normalise the result.

    A model can still return junk through ``result`` when ``structured_output``
    is absent, so nothing is trusted: unknown projects drop, candidates are
    filtered to catalog names and cut to three, a bad priority becomes
    ``normal``, tags are normalised, confidence is clamped, an empty title is
    derived from the prompt. Anything unrecoverable raises :class:`TriageError`.
    """
    if not isinstance(obj, dict):
        raise TriageError("schema: not an object")
    prompt = obj.get("prompt")
    if not isinstance(prompt, str):
        raise TriageError("schema: prompt missing")
    prompt = prompt.strip()
    title = obj.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        title = title_from_description(prompt)
    if not title:
        raise TriageError("schema: no title")

    project = obj.get("project")
    if project not in names:
        project = None

    candidates: list[dict] = []
    raw_candidates = obj.get("candidates")
    if isinstance(raw_candidates, list):
        for cand in raw_candidates:
            if not isinstance(cand, dict) or cand.get("project") not in names:
                continue
            reason = cand.get("reason")
            candidates.append(
                {"project": cand["project"], "reason": reason if isinstance(reason, str) else ""}
            )
    candidates = candidates[:MAX_CANDIDATES]

    priority = obj.get("priority")
    if priority not in PRIORITIES:
        priority = "normal"

    raw_tags = obj.get("tags")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        tags = normalize_tags([t for t in raw_tags if isinstance(t, str)])


    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = 0.0
    confidence = min(1.0, max(0.0, float(confidence)))

    question = obj.get("question")
    question = question.strip() if isinstance(question, str) and question.strip() else None

    return {
        "title": title,
        "prompt": prompt,
        "project": project,
        "candidates": candidates,
        "priority": priority,
        "tags": tags,
        "confidence": confidence,
        "question": question,
        "prefix": False,
    }


# ---------------------------------------------------------------------------
# Triage one note
# ---------------------------------------------------------------------------
def _examples() -> list[tuple[str, str | None]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT text, project FROM triage_examples ORDER BY id DESC LIMIT ?", (MAX_EXAMPLES,)
        ).fetchall()
    return [(r["text"], r["project"]) for r in reversed(rows)]


def triage_text(text: str) -> dict:
    """Catalog (lazy summaries) -> prefix -> ``claude -p`` -> validated proposal.

    A project whose summary cannot be generated drops out of this run's
    catalog and is retried next time. Returns the :func:`parse_result` dict
    plus ``raw`` (the note) and ``prefix`` (whether the note named the project).
    """
    cat: list[tuple[str, str]] = []
    for name, summary in catalog():
        if summary is None:
            try:
                summary = summarize_project(name)
            except TriageError:
                continue
        cat.append((name, summary))
    names = [name for name, _s in cat]
    prefix, body = split_prefix(text, names)
    prompt = system_prompt(cat, _examples())
    obj = parse_result(run_claude(_argv(prompt, _schema(names)), body), names)
    if prefix is not None:
        obj["project"] = prefix
        others = [c for c in obj["candidates"] if c["project"] != prefix]
        obj["candidates"] = [{"project": prefix, "reason": "prefix"}, *others][:MAX_CANDIDATES]
        obj["prefix"] = True
    obj["raw"] = text
    return obj


def tick() -> None:
    """Triage the oldest pending inbox row: proposed task or ``failed``. Never raises."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, text FROM inbox WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        return
    try:
        proposal = triage_text(row["text"])
    except Exception as exc:  # any failure lands on the row, not in the loop
        with get_conn() as conn:
            conn.execute(
                "UPDATE inbox SET status = 'failed', error = ? WHERE id = ?",
                (str(exc) or exc.__class__.__name__, row["id"]),
            )
        return
    description = f"{proposal['prompt']}\n\n{ORIGINAL_HEADING}\n\n{proposal['raw']}"
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (project, title, description, phase, priority, proposed, triage, "
            "sort_order) VALUES (?, ?, ?, 'planned', ?, 1, ?, "
            "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tasks))",
            (
                proposal["project"],
                proposal["title"],
                description,
                proposal["priority"],
                json.dumps(proposal, ensure_ascii=False),
            ),
        )
        task_id = int(cur.lastrowid)
        set_task_tags(conn, task_id, proposal["tags"])
        conn.execute(
            "UPDATE inbox SET status = 'triaged', task_id = ?, error = NULL WHERE id = ?",
            (task_id, row["id"]),
        )


async def worker() -> None:
    """Background loop driving :func:`tick` off the event loop. Started by the app.

    The ``triage_enabled`` setting is read per tick, so switching it off
    pauses the worker without a restart. ``tick`` blocks on ``subprocess.run``
    for up to :data:`TIMEOUT` seconds, hence the thread.
    """
    while True:
        await asyncio.sleep(TRIAGE_TICK)
        with contextlib.suppress(Exception):
            if get_triage_enabled():
                await asyncio.to_thread(tick)
