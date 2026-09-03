"""Workspace inventory -- skills, knowledge base, team personas, tooling.

Read-only filesystem scanners powering the ``/workspace`` page. ntasker is a
task tracker, but the tasks it tracks live inside a wider working
environment: the Claude Code skills that automate work, a knowledge base of
notes, a set of agent personas, and the MCP servers wiring it all together.
None of that is a task, so none of it belongs in the tasks table -- yet
having it one click away turns the tracker into the actual entry point of a
workspace.

The scanners are **read-only and best-effort**. A missing directory is a
normal state (most users configure none of these), never an error: each
scanner returns an empty result plus a ``configured`` / ``exists`` flag so
the UI can distinguish "not set up" from "set up but empty".

Editing lives in the mutation half of this module (``write_file``,
``create_entry``, ``rename_entry``, ``delete_entry``, ``reveal``). Those do
change the filesystem, under two rules that are never relaxed:

* Every operation is confined to :func:`allowed_roots` -- the directories
  the user explicitly configured, and nothing else. That list *is* the
  security boundary; ntasker binds to localhost, but "local" is not "safe".
* Nothing is ever destroyed. A delete moves the entry to the OS trash, or
  failing that into a timestamped folder inside the owning root.

Directories come from settings (see :mod:`ntasker.settings`):

* ``workspace_skills_dir`` -- defaults to ``~/.claude/skills``
* ``workspace_wiki_dir``   -- no default; unset means the card stays hidden
* ``workspace_team_dir``   -- no default; unset means the card stays hidden
* ``workspace_docs_dir``   -- no default; unset means the card stays hidden

Design notes:

* **No caching.** These scans are cheap (a few dozen ``stat`` calls) and the
  page is opened rarely. Stale data would be worse than a millisecond.
* **Bounded work.** Scanners cap how deep and how wide they walk so pointing
  a setting at ``/`` degrades gracefully instead of hanging the event loop.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ntasker.i18n import N_, _

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fallback skills directory when ``workspace_skills_dir`` is unset. This is
#: where Claude Code looks for user-level skills, so it is the right default.
DEFAULT_SKILLS_DIR = "~/.claude/skills"

#: Claude Code's global config file -- source of the MCP server list.
CLAUDE_CONFIG_PATH = "~/.claude.json"

#: Max directory entries any single scan will look at. Guards against a
#: setting that accidentally points somewhere enormous.
MAX_ENTRIES = 500

#: Max depth for the knowledge-base area walk. Notes nest a few levels; we
#: only need per-area counts, not a full tree.
MAX_WIKI_DEPTH = 6

#: File suffixes counted as notes in the knowledge base.
NOTE_SUFFIXES = (".md", ".markdown")

#: Directory names skipped everywhere -- noise, caches, VCS internals.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".obsidian",
        ".trash",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".DS_Store",
    }
)

#: Front-matter delimiter for Markdown notes and SKILL.md files.
_FM_DELIM = "---"

#: Matches a top-level Markdown heading, used as a title fallback.
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

#: Tools whose presence decides whether MCP servers can actually start. Each
#: entry maps a command to the servers that break without it. Purposes are
#: marked with ``N_`` (module-level constant) and translated at scan time.
TOOLING_PROBES: tuple[tuple[str, str], ...] = (
    ("node", N_("JavaScript runtime -- required by every npx-based MCP server")),
    ("npx", N_("Node package runner -- launches npx-based MCP servers")),
    ("uvx", N_("Python tool runner -- launches uvx-based MCP servers")),
    ("docker", N_("Container runtime -- required by docker-based MCP servers")),
    ("git", N_("Version control")),
    ("gh", N_("GitHub CLI")),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expand(path: str | None) -> Path | None:
    """Expand ``~`` and env vars into an absolute :class:`Path`.

    Returns ``None`` for empty input so callers can treat "unset" and
    "invalid" the same way.
    """
    if not path or not str(path).strip():
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(path).strip()))).resolve()


def _is_noise(name: str) -> bool:
    """True for directory entries no scanner should ever descend into."""
    return name in SKIP_DIRS or name.startswith(".")


def _iter_dirs(root: Path) -> Iterable[Path]:
    """Yield immediate sub-directories of ``root``, sorted, noise filtered.

    Symlinks are followed (a skills dir is often a symlink farm) but the
    entry count is capped at :data:`MAX_ENTRIES`.
    """
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return
    for entry in entries[:MAX_ENTRIES]:
        if _is_noise(entry.name):
            continue
        try:
            if entry.is_dir():
                yield entry
        except OSError:
            continue


def _read_head(path: Path, limit: int = 8192) -> str:
    """Read at most ``limit`` bytes of text; empty string on any failure.

    Front matter and the first heading both live at the top of a file, so a
    partial read is enough and keeps a stray 100 MB file from stalling us.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _parse_front_matter(text: str) -> dict[str, str]:
    """Parse a leading ``---`` YAML block into a flat ``str -> str`` dict.

    Deliberately minimal: ntasker has no YAML dependency, and skill/note
    front matter is flat ``key: value`` in practice. Nested mappings and
    list items are skipped rather than guessed at. Keys are lower-cased;
    values keep their original case.

    Block scalars *are* handled -- ``description: >`` followed by indented
    lines is how long skill descriptions are written, and treating one as
    the literal value ``">"`` would silently blank the most important field
    on the page. Folded (``>``) joins its lines with spaces, literal (``|``)
    keeps the newlines.
    """
    if not text.startswith(_FM_DELIM):
        return {}
    end = text.find(f"\n{_FM_DELIM}", len(_FM_DELIM))
    if end == -1:
        return {}
    lines = text[len(_FM_DELIM) : end].splitlines()

    out: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() or line.lstrip().startswith("- "):
            continue  # nested value or list item -- out of scope
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()

        if value in (">", "|", ">-", "|-", ">+", "|+"):
            folded = value.startswith(">")
            collected: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                # A block scalar runs until the next non-indented, non-blank
                # line -- that one belongs to the parent mapping.
                if nxt.strip() and not nxt[:1].isspace():
                    break
                collected.append(nxt.strip())
                index += 1
            joiner = " " if folded else "\n"
            out[key] = joiner.join(p for p in collected if p).strip()
            continue

        out[key] = value.strip("\"'")
    return out


def _first_heading(text: str) -> str:
    """Return the first Markdown H1, or an empty string."""
    match = _H1_RE.search(text)
    return match.group(1).strip() if match else ""


def _truncate(text: str, limit: int = 240) -> str:
    """Shorten ``text`` to ``limit`` chars on a word boundary."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@dataclass
class SkillEntry:
    """One entry in the skills directory and whether it actually loads.

    ``kind`` separates the two shapes that legitimately live here:

    * ``skill`` -- a directory with a ``SKILL.md``, the plain case.
    * ``plugin`` -- a directory with a ``.claude-plugin/plugin.json``. A
      plugin bundles its skills under ``skills/<name>/SKILL.md`` and its
      slash-commands under ``commands/``, so it has no SKILL.md of its own.
      Judging one by the skill rules reports a working plugin as broken.

    ``bundled`` lists what a plugin brings along (``"skill:foo"`` /
    ``"command:bar"``), so the UI can say what is inside without a
    second scan.
    """

    name: str
    path: str
    loads: bool
    description: str = ""
    problem: str = ""
    kind: str = "skill"
    bundled: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "loads": self.loads,
            "description": self.description,
            "problem": self.problem,
            "kind": self.kind,
            "bundled": self.bundled,
        }


def scan_skills(skills_dir: str | None = None) -> dict[str, Any]:
    """Inventory skill directories and diagnose which ones fail to load.

    A skill loads only if it contains a ``SKILL.md`` carrying a ``name`` and
    a ``description`` in its front matter. Every other shape -- a bare
    ``README.md``, a ``commands/`` folder, an empty directory -- is silently
    ignored by Claude Code, which makes a broken skill invisible rather than
    noisy. Surfacing the reason is the whole point of this scan.

    Returns ``{configured, exists, path, skills, total, loading, broken}``.
    """
    raw = skills_dir or DEFAULT_SKILLS_DIR
    root = _expand(raw)
    result: dict[str, Any] = {
        "configured": bool(raw),
        "path": str(root) if root else "",
        "exists": bool(root and root.is_dir()),
        "skills": [],
        "total": 0,
        "loading": 0,
        "broken": 0,
    }
    if not root or not root.is_dir():
        return result

    entries: list[SkillEntry] = []
    for directory in _iter_dirs(root):
        entries.append(_inspect_skill(directory))

    entries.sort(key=lambda s: (not s.loads, s.name.lower()))
    result["skills"] = [s.as_dict() for s in entries]
    result["total"] = len(entries)
    result["loading"] = sum(1 for s in entries if s.loads)
    result["broken"] = sum(1 for s in entries if not s.loads)
    return result


def _inspect_skill(directory: Path) -> SkillEntry:
    """Classify a single entry, naming the defect only when truly broken."""
    # A plugin is checked first: it legitimately has no SKILL.md of its own,
    # so running the skill rules over it would report something that works
    # perfectly well as broken.
    if (directory / ".claude-plugin" / "plugin.json").is_file():
        return _inspect_plugin(directory)

    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        # Distinguish the common near-misses so the fix is obvious.
        if (directory / "commands").is_dir():
            problem = _(
                "No SKILL.md -- holds a commands/ folder. That is a "
                "slash-command layout; add a SKILL.md to make it load as a skill."
            )
        elif any(directory.glob("*.md")):
            loose = _loose_skill_names(directory)
            if loose:
                # The most misleading shape there is: the files carry proper
                # skill front matter, so they look finished -- but Claude Code
                # only ever reads <dir>/SKILL.md, so none of them is loaded.
                problem = _(
                    "No SKILL.md -- but {n} Markdown files here carry skill "
                    "front matter ({names}). Each needs its own sub-directory "
                    "with the file renamed to SKILL.md, otherwise none of them "
                    "is ever loaded."
                ).format(n=len(loose), names=", ".join(loose[:6]))
            else:
                problem = _(
                    "No SKILL.md -- Markdown files present but none named SKILL.md."
                )
        else:
            problem = _("No SKILL.md in this directory.")
        return SkillEntry(
            name=directory.name, path=str(directory), loads=False, problem=problem
        )

    head = _read_head(skill_md)
    front = _parse_front_matter(head)
    description = front.get("description", "")
    if not front:
        return SkillEntry(
            name=directory.name,
            path=str(directory),
            loads=False,
            problem=_("SKILL.md has no front matter (needs a leading --- block)."),
        )
    if not front.get("name") or not description:
        missing = [k for k in ("name", "description") if not front.get(k)]
        return SkillEntry(
            name=front.get("name") or directory.name,
            path=str(directory),
            loads=False,
            description=_truncate(description),
            problem=_("SKILL.md front matter is missing: {fields}.").format(
                fields=", ".join(missing)
            ),
        )

    return SkillEntry(
        name=front["name"],
        path=str(directory),
        loads=True,
        description=_truncate(description),
    )


def _loose_skill_names(directory: Path) -> list[str]:
    """Names of Markdown files here that carry usable skill front matter.

    Used to tell "a folder with a README in it" apart from "six finished
    skills that silently never load because of their layout" -- the second
    deserves a very different sentence.
    """
    names: list[str] = []
    for entry in sorted(directory.glob("*.md")):
        if entry.name == "SKILL.md":
            continue
        front = _parse_front_matter(_read_head(entry))
        if front.get("name") and front.get("description"):
            names.append(front["name"])
    return names


def _inspect_plugin(directory: Path) -> SkillEntry:
    """Describe a Claude Code plugin directory and what it bundles.

    The manifest carries the name and description; the skills and commands
    live beside it. A plugin with neither is reported as not loading -- an
    empty manifest is real breakage, just a different kind than a missing
    SKILL.md.
    """
    manifest = directory / ".claude-plugin" / "plugin.json"
    name, description = directory.name, ""
    try:
        with manifest.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            name = str(data.get("name") or directory.name)
            description = str(data.get("description") or "")
    except (OSError, json.JSONDecodeError):
        return SkillEntry(
            name=directory.name,
            path=str(directory),
            loads=False,
            kind="plugin",
            problem=_("plugin.json cannot be read or is not valid JSON."),
        )

    bundled: list[str] = []
    skills_root = directory / "skills"
    if skills_root.is_dir():
        for sub in _iter_dirs(skills_root):
            if (sub / "SKILL.md").is_file():
                bundled.append(f"skill:{sub.name}")
    commands_root = directory / "commands"
    if commands_root.is_dir():
        try:
            for cmd in sorted(commands_root.glob("*.md"))[:MAX_ENTRIES]:
                bundled.append(f"command:{cmd.stem}")
        except OSError:
            pass

    return SkillEntry(
        name=name,
        path=str(directory),
        loads=bool(bundled),
        description=_truncate(description),
        kind="plugin",
        bundled=bundled,
        problem="" if bundled else _(
            "Plugin manifest present but it bundles no skills and no commands."
        ),
    )


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


def _count_notes(area: Path, depth: int = 0) -> int:
    """Count Markdown notes under ``area``, depth-capped and noise-filtered."""
    if depth > MAX_WIKI_DEPTH:
        return 0
    total = 0
    try:
        entries = list(area.iterdir())
    except OSError:
        return 0
    for entry in entries[:MAX_ENTRIES]:
        if _is_noise(entry.name):
            continue
        try:
            if entry.is_dir():
                total += _count_notes(entry, depth + 1)
            elif entry.suffix.lower() in NOTE_SUFFIXES:
                total += 1
        except OSError:
            continue
    return total


def scan_wiki(wiki_dir: str | None) -> dict[str, Any]:
    """Summarize a Markdown knowledge base by top-level area.

    Reports one entry per area (sub-directory) with its note count, plus any
    index files sitting at the root. Obsidian is the common editor for these
    vaults, so each area also carries an ``obsidian`` URI the UI can link to.

    Returns ``{configured, exists, path, vault, areas, total_notes, indexes}``.
    """
    root = _expand(wiki_dir)
    result: dict[str, Any] = {
        "configured": bool(wiki_dir),
        "path": str(root) if root else "",
        "exists": bool(root and root.is_dir()),
        "vault": root.name if root else "",
        "areas": [],
        "indexes": [],
        "total_notes": 0,
    }
    if not root or not root.is_dir():
        return result

    areas: list[dict[str, Any]] = []
    for directory in _iter_dirs(root):
        count = _count_notes(directory)
        areas.append(
            {
                "name": directory.name,
                "path": str(directory),
                "notes": count,
                "uri": _obsidian_uri(root.name, directory.relative_to(root)),
            }
        )
    areas.sort(key=lambda a: (-a["notes"], a["name"].lower()))

    indexes: list[dict[str, Any]] = []
    try:
        for entry in sorted(root.glob("*.md"))[:MAX_ENTRIES]:
            indexes.append(
                {
                    "name": entry.stem,
                    "path": str(entry),
                    "uri": _obsidian_uri(root.name, entry.relative_to(root)),
                }
            )
    except OSError:
        pass

    result["areas"] = areas
    result["indexes"] = indexes
    result["total_notes"] = sum(a["notes"] for a in areas) + len(indexes)
    return result


def _obsidian_uri(vault: str, rel: Path) -> str:
    """Build an ``obsidian://`` deep link for a path inside ``vault``."""
    from urllib.parse import quote

    return f"obsidian://open?vault={quote(vault)}&file={quote(rel.as_posix())}"


# ---------------------------------------------------------------------------
# Team personas
# ---------------------------------------------------------------------------

#: Front-matter keys and inline labels that carry a persona's role.
_ROLE_KEYS = ("rolle", "role", "aufgabe", "funktion")
#: Matches ``**Rolle:** text``, ``**Role**: text`` and the unadorned form --
#: the emphasis markers sit on either side of the colon depending on who
#: wrote the file, so both placements are stripped.
_ROLE_INLINE_RE = re.compile(
    r"^\s*\**\s*(?:Rolle|Role|Aufgabe)\s*\**\s*:\s*\**\s*(.+?)\s*\**\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def scan_team(team_dir: str | None) -> dict[str, Any]:
    """Inventory agent personas stored as one Markdown file each.

    A persona file is expected to state its role either in front matter
    (``role:`` / ``rolle:``) or as a bold inline label near the top --
    both spellings appear in the wild, so both are accepted.

    Returns ``{configured, exists, path, members, total}``.
    """
    root = _expand(team_dir)
    result: dict[str, Any] = {
        "configured": bool(team_dir),
        "path": str(root) if root else "",
        "exists": bool(root and root.is_dir()),
        "members": [],
        "total": 0,
    }
    if not root or not root.is_dir():
        return result

    members: list[dict[str, Any]] = []
    try:
        files = sorted(root.glob("*.md"), key=lambda p: p.name.lower())
    except OSError:
        files = []

    for entry in files[:MAX_ENTRIES]:
        head = _read_head(entry)
        front = _parse_front_matter(head)
        role = ""
        for key in _ROLE_KEYS:
            if front.get(key):
                role = front[key]
                break
        if not role:
            match = _ROLE_INLINE_RE.search(head)
            if match:
                role = match.group(1)
        members.append(
            {
                "name": front.get("name") or entry.stem,
                "path": str(entry),
                "role": _truncate(role, 160),
                "title": front.get("title") or _first_heading(head),
            }
        )

    result["members"] = members
    result["total"] = len(members)
    return result


# ---------------------------------------------------------------------------
# Generated documents
# ---------------------------------------------------------------------------

#: Suffixes the previewer can render as rich text or a table.
PREVIEWABLE = frozenset({".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".log"})

#: Coarse file classes the UI turns into an icon + preview mode.
_KIND_BY_SUFFIX: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "text",
    ".log": "text",
    ".json": "text",
    ".pdf": "pdf",
    ".docx": "doc",
    ".doc": "doc",
    ".xlsx": "sheet",
    ".xls": "sheet",
    ".pptx": "slides",
    ".ppt": "slides",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".svg": "image",
}

#: Leading ``YYYY-MM-DD`` or trailing ``_YYYY-MM-DD`` in a generated filename.
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def classify(path: Path) -> str:
    """Map a file to a coarse kind the UI can icon and preview by."""
    return _KIND_BY_SUFFIX.get(path.suffix.lower(), "other")


def scan_docs(docs_dir: str | None) -> dict[str, Any]:
    """List generated documents, newest first.

    A flat output folder that agents write finished artefacts into --
    offers, reviews, briefings, exported CSVs. Sorted by modification time
    because recency is what matters when hunting for "the thing I generated
    yesterday". The date embedded in most filenames is surfaced separately
    so the UI can group by it without reparsing.

    Returns ``{configured, exists, path, docs, total, kinds}``.
    """
    root = _expand(docs_dir)
    result: dict[str, Any] = {
        "configured": bool(docs_dir),
        "path": str(root) if root else "",
        "exists": bool(root and root.is_dir()),
        "docs": [],
        "total": 0,
        "kinds": {},
    }
    if not root or not root.is_dir():
        return result

    docs: list[dict[str, Any]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        entries = []

    for entry in entries[:MAX_ENTRIES]:
        if _is_noise(entry.name):
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        kind = classify(entry)
        match = _DATE_RE.search(entry.stem)
        docs.append(
            {
                "name": entry.name,
                "stem": entry.stem,
                "path": str(entry),
                "suffix": entry.suffix.lower().lstrip("."),
                "kind": kind,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "date": match.group(1) if match else "",
                "previewable": entry.suffix.lower() in PREVIEWABLE,
            }
        )

    docs.sort(key=lambda d: d["modified"], reverse=True)
    kinds: dict[str, int] = {}
    for doc in docs:
        kinds[doc["kind"]] = kinds.get(doc["kind"], 0) + 1

    result["docs"] = docs
    result["total"] = len(docs)
    result["kinds"] = dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    return result


# ---------------------------------------------------------------------------
# File preview
# ---------------------------------------------------------------------------

#: Hard cap on previewed file size. Past this the UI shows metadata only --
#: rendering a 10 MB Markdown file would lock up the browser anyway.
MAX_PREVIEW_BYTES = 1_000_000


class PreviewError(Exception):
    """Raised when a file may not be previewed.

    ``reason`` is one of ``forbidden`` (outside every configured root),
    ``not_found``, or ``too_large`` -- the caller maps these to HTTP codes.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def allowed_roots(
    skills_dir: str | None = None,
    wiki_dir: str | None = None,
    team_dir: str | None = None,
    docs_dir: str | None = None,
) -> list[Path]:
    """Resolve the configured directories a preview may read from.

    This list *is* the security boundary for :func:`read_file`. Only
    directories the user explicitly configured are readable -- ntasker
    binds to localhost, but "local" is not "safe": anything that can reach
    the port could otherwise walk the whole filesystem through the preview
    endpoint.
    """
    candidates = [
        skills_dir or DEFAULT_SKILLS_DIR,
        wiki_dir,
        team_dir,
        docs_dir,
    ]
    roots: list[Path] = []
    for candidate in candidates:
        resolved = _expand(candidate)
        if resolved and resolved.is_dir():
            roots.append(resolved)
    return roots


def within_roots(path: Path, roots: list[Path]) -> bool:
    """True if ``path`` lies inside one of ``roots``.

    Both sides are fully resolved first, so ``..`` segments and symlinks
    pointing out of a root are rejected rather than followed. Public because
    the task-context endpoint applies the very same boundary before storing
    a path -- an attachment is a pointer the agent briefing later reads out,
    so it may only ever point into the configured workspace.
    """
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def read_file(path: str, roots: list[Path]) -> dict[str, Any]:
    """Read one file for previewing, confined to ``roots``.

    Raises :class:`PreviewError` when the path escapes every configured
    root, does not exist, or exceeds :data:`MAX_PREVIEW_BYTES`. Binary
    kinds return metadata with ``text=None``: the UI links them out to the
    OS instead of trying to render them.
    """
    if not roots:
        raise PreviewError(
            "forbidden",
            _("No workspace directories are configured, so nothing may be read."),
        )

    try:
        target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    except (OSError, RuntimeError) as exc:
        raise PreviewError(
            "not_found", _("Path cannot be resolved: {error}").format(error=exc)
        ) from exc

    if not within_roots(target, roots):
        raise PreviewError(
            "forbidden",
            _("Path lies outside every configured workspace directory."),
        )
    return describe_file(target)


def describe_file(target: Path) -> dict[str, Any]:
    """Preview payload for an already-resolved, already-authorised path.

    The root check lives in :func:`read_file`; this half is shared with the
    task-attachment preview, whose authorisation is "the user attached this
    exact path to a task" rather than "it lies under a workspace root".
    A directory is reported with ``kind="folder"`` and no text -- the UI
    hands it to the desktop instead of rendering it.
    """
    if target.is_dir():
        try:
            stat = target.stat()
        except OSError as exc:
            raise PreviewError("not_found", str(exc)) from exc
        return {
            "name": target.name,
            "path": str(target),
            "suffix": "",
            "kind": "folder",
            "size": 0,
            "modified": stat.st_mtime,
            "text": None,
            "truncated": False,
        }
    if not target.is_file():
        raise PreviewError("not_found", _("No such file."))

    try:
        stat = target.stat()
    except OSError as exc:
        raise PreviewError("not_found", str(exc)) from exc

    kind = classify(target)
    info: dict[str, Any] = {
        "name": target.name,
        "path": str(target),
        "suffix": target.suffix.lower().lstrip("."),
        "kind": kind,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "text": None,
        "truncated": False,
    }

    if target.suffix.lower() not in PREVIEWABLE:
        return info
    if stat.st_size > MAX_PREVIEW_BYTES:
        raise PreviewError(
            "too_large",
            _("File is {size} bytes; the preview limit is {limit}.").format(
                size=stat.st_size, limit=MAX_PREVIEW_BYTES
            ),
        )

    try:
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            info["text"] = fh.read(MAX_PREVIEW_BYTES)
    except OSError as exc:
        raise PreviewError("not_found", str(exc)) from exc

    info["truncated"] = stat.st_size > len(info["text"] or "")
    return info


# ---------------------------------------------------------------------------
# Directory browsing
# ---------------------------------------------------------------------------


def browse(path: str, roots: list[Path]) -> dict[str, Any]:
    """List one directory's contents, confined to ``roots``.

    The section scanners answer "what is in my workspace" at a summary
    level -- areas with note counts, personas, the document list. This
    answers the follow-up: actually walking into a knowledge base of a few
    hundred notes and opening one. Folders sort first, then files by name,
    because a vault is navigated by structure rather than by recency.

    ``parent`` is the directory one level up, or ``""`` when ``path`` *is*
    a configured root -- that is where "up" has to stop.
    """
    if not roots:
        raise PreviewError(
            "forbidden",
            _("No workspace directories are configured, so nothing may be read."),
        )
    try:
        target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    except (OSError, RuntimeError) as exc:
        raise PreviewError(
            "not_found", _("Path cannot be resolved: {error}").format(error=exc)
        ) from exc

    if not within_roots(target, roots):
        raise PreviewError(
            "forbidden",
            _("Path lies outside every configured workspace directory."),
        )
    return list_directory(target, is_root=any(target == root for root in roots))


def list_directory(target: Path, is_root: bool = False) -> dict[str, Any]:
    """Listing payload for an already-resolved, already-authorised directory.

    Shared by :func:`browse` (workspace roots) and the file picker's
    machine-wide browser, where the only boundary is the filesystem root.
    ``is_root`` marks the point where "up" stops (``parent`` is ``""``).
    """
    if not target.is_dir():
        raise PreviewError("not_found", _("No such directory."))

    entries: list[dict[str, Any]] = []
    try:
        raw = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise PreviewError("not_found", str(exc)) from exc

    for entry in raw[:MAX_ENTRIES]:
        if _is_noise(entry.name):
            continue
        try:
            is_dir = entry.is_dir()
            stat = entry.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "stem": entry.stem,
                "path": str(entry),
                "directory": is_dir,
                "suffix": "" if is_dir else entry.suffix.lower().lstrip("."),
                "kind": "folder" if is_dir else classify(entry),
                "size": 0 if is_dir else stat.st_size,
                "modified": stat.st_mtime,
                "previewable": not is_dir and entry.suffix.lower() in PREVIEWABLE,
                "editable": not is_dir and entry.suffix.lower() in EDITABLE,
            }
        )

    entries.sort(key=lambda e: (not e["directory"], e["name"].lower()))
    if target.parent == target:
        is_root = True
    return {
        "path": str(target),
        "name": target.name,
        "parent": "" if is_root else str(target.parent),
        "is_root": is_root,
        "entries": entries,
        "total": len(entries),
        "truncated": len(raw) > MAX_ENTRIES,
    }


# ---------------------------------------------------------------------------
# Mutations -- write, create, rename, delete
# ---------------------------------------------------------------------------

#: Suffixes the editor may write. Deliberately narrower than what the
#: previewer renders: a ``.csv`` is safe to hand-edit, a ``.xlsx`` is not
#: (writing text into it would corrupt the archive). Anything outside this
#: set can still be renamed and deleted -- just not rewritten as text.
EDITABLE = frozenset({".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".log"})

#: Hard cap on a written payload. Matches the read cap, so a file that came
#: out of the previewer always fits back in.
MAX_WRITE_BYTES = MAX_PREVIEW_BYTES

#: Fallback trash directory, created inside the owning root when the OS
#: trash is unavailable. Dot-prefixed, so every scanner already skips it.
TRASH_DIRNAME = ".ntasker-trash"

#: Characters a new file or directory name may never contain. Path
#: separators would let a name escape its parent; the rest are reserved on
#: at least one platform ntasker runs on.
_BAD_NAME_CHARS = set('/\\:*?"<>|\0')


class WriteError(Exception):
    """Raised when a mutation is refused.

    ``reason`` is one of ``forbidden`` (outside every configured root, or a
    root itself), ``not_found``, ``exists``, ``too_large``, or ``invalid``
    -- the caller maps these to HTTP codes.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _resolve(path: str) -> Path:
    """Expand and fully resolve ``path``; raise ``WriteError`` on failure."""
    try:
        return Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    except (OSError, RuntimeError) as exc:
        raise WriteError(
            "not_found", _("Path cannot be resolved: {error}").format(error=exc)
        ) from exc


def _guard(target: Path, roots: list[Path]) -> Path:
    """Confine ``target`` to ``roots`` and refuse the roots themselves.

    A configured root is the boundary, not an object inside it: renaming or
    deleting one would take the whole knowledge base with it, and no UI
    gesture should be able to do that by accident.
    """
    if not roots:
        raise WriteError(
            "forbidden",
            _("No workspace directories are configured, so nothing may be changed."),
        )
    if not within_roots(target, roots):
        raise WriteError(
            "forbidden",
            _("Path lies outside every configured workspace directory."),
        )
    if any(target == root for root in roots):
        raise WriteError(
            "forbidden",
            _("This is a configured workspace directory itself -- it cannot be "
              "renamed or deleted from here."),
        )
    return target


def _check_name(name: str) -> str:
    """Validate a single path component (no separators, no traversal)."""
    clean = (name or "").strip()
    if not clean:
        raise WriteError("invalid", _("The name must not be empty."))
    if clean in (".", ".."):
        raise WriteError("invalid", _("That name is reserved."))
    if any(ch in _BAD_NAME_CHARS for ch in clean):
        raise WriteError(
            "invalid",
            _("The name must not contain any of: {chars}").format(
                chars=" ".join(sorted(_BAD_NAME_CHARS - {"\0"}))
            ),
        )
    if len(clean) > 255:
        raise WriteError("invalid", _("The name is too long (max 255 characters)."))
    return clean


def write_file(path: str, text: str, roots: list[Path]) -> dict[str, Any]:
    """Overwrite an existing text file with ``text``.

    Only suffixes in :data:`EDITABLE` may be written -- see the constant for
    why. The write goes to a sibling temp file first and is then moved into
    place, so a crash mid-write cannot leave a half-written note behind.

    Returns the same shape as :func:`read_file` for the fresh content.
    """
    target = _guard(_resolve(path), roots)
    if not target.is_file():
        raise WriteError("not_found", _("No such file."))
    if target.suffix.lower() not in EDITABLE:
        raise WriteError(
            "invalid",
            _("Files of type {suffix} cannot be edited here.").format(
                suffix=target.suffix or "?"
            ),
        )

    payload = text or ""
    size = len(payload.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        raise WriteError(
            "too_large",
            _("File is {size} bytes; the write limit is {limit}.").format(
                size=size, limit=MAX_WRITE_BYTES
            ),
        )

    _atomic_write(target, payload)
    return read_file(str(target), roots)


def _atomic_write(target: Path, payload: str) -> None:
    """Write ``payload`` to ``target`` via a temp file + ``os.replace``.

    ``os.replace`` is atomic within a filesystem, and the temp file is a
    sibling precisely so that holds. The dot prefix keeps a leftover temp
    invisible to the scanners if the process dies between the two steps.
    """
    import tempfile  # noqa: PLC0415 -- only needed on the write path

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise WriteError("invalid", _("Could not write the file: {error}").format(error=exc)) from exc


def create_entry(
    parent: str, name: str, roots: list[Path], directory: bool = False
) -> dict[str, Any]:
    """Create an empty file (or a directory) inside ``parent``.

    ``parent`` must already lie inside a configured root -- unlike the other
    mutations it *may* be a root itself, since new notes have to land
    somewhere. Refuses to clobber an existing entry.
    """
    if not roots:
        raise WriteError(
            "forbidden",
            _("No workspace directories are configured, so nothing may be changed."),
        )
    base = _resolve(parent)
    if not within_roots(base, roots):
        raise WriteError(
            "forbidden", _("Path lies outside every configured workspace directory.")
        )
    if not base.is_dir():
        raise WriteError("not_found", _("No such directory."))

    clean = _check_name(name)
    if not directory and not Path(clean).suffix:
        # A note without a suffix is almost always a slip, and it would be
        # neither previewable nor editable afterwards.
        clean += ".md"
    target = base / clean
    if target.exists():
        raise WriteError("exists", _("{name} already exists here.").format(name=clean))

    try:
        if directory:
            target.mkdir()
        else:
            target.touch()
    except OSError as exc:
        raise WriteError(
            "invalid", _("Could not create it: {error}").format(error=exc)
        ) from exc

    return {
        "name": target.name,
        "path": str(target),
        "directory": directory,
        "kind": "folder" if directory else classify(target),
    }


def rename_entry(path: str, new_name: str, roots: list[Path]) -> dict[str, Any]:
    """Rename a file or directory in place (same parent)."""
    target = _guard(_resolve(path), roots)
    if not target.exists():
        raise WriteError("not_found", _("No such file or directory."))

    clean = _check_name(new_name)
    destination = target.parent / clean
    if destination == target:
        return {"name": target.name, "path": str(target)}
    if destination.exists():
        raise WriteError("exists", _("{name} already exists here.").format(name=clean))

    try:
        target.rename(destination)
    except OSError as exc:
        raise WriteError(
            "invalid", _("Could not rename it: {error}").format(error=exc)
        ) from exc
    return {"name": destination.name, "path": str(destination)}


def delete_entry(path: str, roots: list[Path]) -> dict[str, Any]:
    """Move a file or directory to the trash. Never unlinks outright.

    Deleting from a browser has no undo, so nothing here is destroyed: on
    macOS the entry goes to the Finder trash (recoverable with Cmd-Z-style
    "Put Back"), everywhere else -- and whenever the Finder route fails --
    into a timestamped folder under :data:`TRASH_DIRNAME` inside the owning
    root. ``method`` in the result says which route was taken so the UI can
    tell the user where their file went.
    """
    target = _guard(_resolve(path), roots)
    if not target.exists():
        raise WriteError("not_found", _("No such file or directory."))

    if _trash_via_finder(target):
        return {"path": str(target), "name": target.name, "method": "os"}

    root = next((r for r in roots if within_roots(target, [r])), target.parent)
    return {
        "path": str(target),
        "name": target.name,
        "method": "folder",
        "trashed_to": _trash_into_folder(target, root),
    }


def _trash_via_finder(target: Path) -> bool:
    """Ask the macOS Finder to trash ``target``. False if that is not possible.

    Only attempted on Darwin, and only with a short timeout: if the Finder
    is not running or is busy, falling through to the folder-based trash is
    much better than blocking the request.
    """
    import subprocess  # noqa: PLC0415 -- only needed on the delete path
    import sys  # noqa: PLC0415

    if sys.platform != "darwin":
        return False
    script = (
        'tell application "Finder" to delete POSIX file '
        f'"{str(target).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
    )
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed binary, path passed as data
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and not target.exists()


def _trash_into_folder(target: Path, root: Path) -> str:
    """Move ``target`` into ``root/.ntasker-trash/<stamp>/`` and return the path.

    The timestamp folder keeps same-named deletions from colliding, and
    keeps the trash browsable by when things were thrown away.
    """
    from datetime import datetime  # noqa: PLC0415 -- only needed on the delete path

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bin_dir = root / TRASH_DIRNAME / stamp
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        destination = bin_dir / target.name
        # Belt and braces: within one second two deletes of the same name
        # would otherwise land on each other.
        counter = 1
        while destination.exists():
            destination = bin_dir / f"{target.stem}-{counter}{target.suffix}"
            counter += 1
        shutil.move(str(target), str(destination))
    except OSError as exc:
        raise WriteError(
            "invalid", _("Could not move it to the trash: {error}").format(error=exc)
        ) from exc
    return str(destination)


def reveal(path: str, roots: list[Path]) -> dict[str, Any]:
    """Open ``path`` in the OS default application.

    This is the "execute" half of the workspace: a .docx or .pdf has no
    in-browser story, and for those the right move is handing the file to
    the desktop rather than half-rendering it. Confined to the same roots
    as every other operation -- the point is opening the user's own notes,
    not turning ntasker into a launcher for arbitrary paths.
    """
    target = _resolve(path)
    if not roots or not within_roots(target, roots):
        raise WriteError(
            "forbidden", _("Path lies outside every configured workspace directory.")
        )
    return open_with_desktop(target)


def open_with_desktop(target: Path) -> dict[str, Any]:
    """Hand an already-authorised path to the desktop's default application.

    Shared by :func:`reveal` (workspace roots) and the task-attachment
    endpoint (paths the user attached explicitly).
    """
    import subprocess  # noqa: PLC0415 -- only needed on this path
    import sys  # noqa: PLC0415

    if not target.exists():
        raise WriteError("not_found", _("No such file or directory."))

    if sys.platform == "darwin":
        argv = ["/usr/bin/open", str(target)]
    elif sys.platform.startswith("win"):
        argv = ["cmd", "/c", "start", "", str(target)]
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise WriteError("invalid", _("No desktop opener (xdg-open) is available."))
        argv = [opener, str(target)]

    try:
        subprocess.Popen(  # noqa: S603 -- fixed opener, path passed as data
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        raise WriteError(
            "invalid", _("Could not open it: {error}").format(error=exc)
        ) from exc
    return {"path": str(target), "opened": True}


# ---------------------------------------------------------------------------
# Places -- Finder-style shortcuts for the machine-wide file browser
# ---------------------------------------------------------------------------


def fs_places(roots: list[Path] | None = None) -> list[dict[str, str]]:
    """Sidebar entries for the file picker: home folders, cloud drives,
    the configured workspace roots and mounted volumes -- only those that
    exist. Mirrors what the Finder sidebar shows, because that is the
    mental map the user already has for where a file lives."""
    import sys  # noqa: PLC0415

    home = Path.home()
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str, path: Path, icon: str) -> None:
        try:
            if not path.is_dir():
                return
            key = str(path.resolve())
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        out.append({"name": name, "path": key, "icon": icon})

    add(N_("Home"), home, "ti-home")
    add(N_("Desktop"), home / "Desktop", "ti-device-desktop")
    add(N_("Documents"), home / "Documents", "ti-file-text")
    add(N_("Downloads"), home / "Downloads", "ti-download")
    add("Code", home / "Code", "ti-code")
    add("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs", "ti-cloud")
    cloud = home / "Library" / "CloudStorage"
    if cloud.is_dir():
        try:
            for entry in sorted(cloud.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith("."):
                    add(entry.name, entry, "ti-cloud")
        except OSError:
            pass
    for root in roots or []:
        add(root.name or str(root), root, "ti-folder-star")
    if sys.platform == "darwin":
        volumes = Path("/Volumes")
        if volumes.is_dir():
            try:
                for entry in sorted(volumes.iterdir(), key=lambda p: p.name.lower()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        add(entry.name, entry, "ti-database")
            except OSError:
                pass
    add("/", Path("/"), "ti-server")
    return out


# ---------------------------------------------------------------------------
# Native file dialog
# ---------------------------------------------------------------------------

#: How long a native file dialog may stay open before the request gives up.
PICK_TIMEOUT_SECONDS = 600


def picker_available() -> bool:
    """True if this machine can show a native file dialog for the browser.

    The server runs on the same desktop as the browser, which is what makes
    this possible at all: a web page cannot learn a picked file's path, but
    a local process can open the OS dialog and report it. macOS has
    ``osascript``; Linux needs ``zenity``. Anything else falls back to the
    pasted-path input.
    """
    import sys  # noqa: PLC0415

    if sys.platform == "darwin":
        return os.path.exists("/usr/bin/osascript")
    if sys.platform.startswith("linux"):
        return shutil.which("zenity") is not None
    return False


def pick_paths(folder: bool = False, prompt: str = "") -> list[str]:
    """Open the OS file (or folder) dialog and return the chosen absolute paths.

    Blocks until the user picks or cancels; a cancel is an empty list, not
    an error. Raises :class:`WriteError` (``invalid``) when no dialog is
    available on this platform or it failed to start.
    """
    import subprocess  # noqa: PLC0415 -- only needed on this path
    import sys  # noqa: PLC0415

    if not picker_available():
        raise WriteError("invalid", _("No native file dialog is available here."))

    if sys.platform == "darwin":
        # `activate` first, so the dialog comes to the front instead of
        # hiding behind the browser that asked for it. `choose file` returns
        # a list of aliases; POSIX path is the form every other endpoint uses.
        safe_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"')
        chooser = (
            f'choose folder with prompt "{safe_prompt}"'
            if folder
            else f'choose file with prompt "{safe_prompt}" '
            "with multiple selections allowed"
        )
        script = "\n".join(
            [
                "activate",
                f"set picked to {chooser}",
                "if class of picked is not list then set picked to {picked}",
                'set out to ""',
                "repeat with f in picked",
                "set out to out & POSIX path of f & linefeed",
                "end repeat",
                "return out",
            ]
        )
        argv = ["/usr/bin/osascript", "-e", script]
    else:
        argv = [shutil.which("zenity") or "zenity", "--file-selection", "--separator=\n"]
        if folder:
            argv.append("--directory")
        else:
            argv.append("--multiple")
        if prompt:
            argv.append(f"--title={prompt}")

    try:
        proc = subprocess.run(  # noqa: S603 -- fixed binary, prompt passed as data
            argv,
            capture_output=True,
            text=True,
            timeout=PICK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WriteError(
            "invalid", _("Could not open the file dialog: {error}").format(error=exc)
        ) from exc

    if proc.returncode != 0:
        # osascript exits 1 with "User canceled. (-128)"; zenity exits 1 on
        # cancel as well. Either way there is nothing to attach.
        return []
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # `POSIX path of` yields a trailing slash for folders; normalise.
        paths.append(line.rstrip("/") or "/")
    return paths


# ---------------------------------------------------------------------------
# Tooling / MCP
# ---------------------------------------------------------------------------


@dataclass
class ToolStatus:
    """A external command ntasker's environment may or may not provide."""

    name: str
    purpose: str
    path: str = ""
    available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "path": self.path,
            "available": self.available,
        }


def scan_tooling() -> dict[str, Any]:
    """Report configured MCP servers and whether their runtimes exist.

    An MCP server declared in ``~/.claude.json`` starts only if its
    ``command`` resolves on ``PATH``. When it does not, Claude Code simply
    shows no tools from that server -- with no error anywhere. Pairing each
    server with a resolved-or-not verdict turns that silence into a
    diagnosis.

    Secrets are never returned: env values are reduced to their key names
    plus a flag marking whether the value is inlined or expanded from the
    environment.

    Returns ``{config_path, config_found, servers, tools, missing_runtimes}``.
    """
    config = _expand(CLAUDE_CONFIG_PATH)
    result: dict[str, Any] = {
        "config_path": str(config) if config else "",
        "config_found": bool(config and config.is_file()),
        "servers": [],
        "tools": [],
        "missing_runtimes": [],
    }

    tools = [_probe_tool(name, purpose) for name, purpose in TOOLING_PROBES]
    result["tools"] = [t.as_dict() for t in tools]
    available = {t.name for t in tools if t.available}

    if not config or not config.is_file():
        return result

    try:
        with config.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        result["config_found"] = False
        return result

    servers_raw = data.get("mcpServers")
    if not isinstance(servers_raw, dict):
        return result

    servers: list[dict[str, Any]] = []
    missing: set[str] = set()
    for name, spec in sorted(servers_raw.items()):
        if not isinstance(spec, dict):
            continue
        entry = _describe_server(name, spec, available)
        if entry["runtime"] and not entry["runtime_ok"]:
            missing.add(entry["runtime"])
        servers.append(entry)

    result["servers"] = servers
    result["missing_runtimes"] = sorted(missing)
    return result


def _probe_tool(name: str, purpose: str) -> ToolStatus:
    """Resolve one command on ``PATH``."""
    found = shutil.which(name)
    return ToolStatus(
        name=name, purpose=_(purpose), path=found or "", available=bool(found)
    )


def _describe_server(
    name: str, spec: dict[str, Any], available: set[str]
) -> dict[str, Any]:
    """Reduce one MCP server entry to a safe, UI-ready summary.

    ``runtime`` is the bare command name (``npx`` for
    ``/usr/local/bin/npx``), which is what decides whether the server can
    start. HTTP servers have no runtime at all and are reported as reachable
    by definition -- ntasker does not make network calls to verify them.
    """
    transport = str(spec.get("type") or ("http" if spec.get("url") else "stdio"))
    command = str(spec.get("command") or "")
    runtime = Path(command).name if command else ""

    if transport == "http" or spec.get("url"):
        runtime_ok = True
        runtime = ""
    elif not runtime:
        runtime_ok = False
    elif runtime in available:
        runtime_ok = True
    else:
        # Absolute path in the config: trust the file itself over PATH.
        runtime_ok = bool(command) and os.path.isabs(command) and os.access(command, os.X_OK)

    env_raw = spec.get("env")
    env_keys: list[dict[str, Any]] = []
    if isinstance(env_raw, dict):
        for key, value in sorted(env_raw.items()):
            text = str(value or "")
            env_keys.append(
                {
                    "key": key,
                    # A ${VAR} placeholder is resolved from the environment;
                    # anything else is a literal sitting in the config file.
                    "from_env": text.startswith("${") and text.endswith("}"),
                    "empty": not text,
                }
            )

    return {
        "name": name,
        "transport": transport,
        "command": command,
        "runtime": runtime,
        "runtime_ok": runtime_ok,
        "env": env_keys,
    }


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def collect(
    skills_dir: str | None = None,
    wiki_dir: str | None = None,
    team_dir: str | None = None,
    docs_dir: str | None = None,
) -> dict[str, Any]:
    """Run every scanner and return the combined workspace inventory."""
    return {
        "skills": scan_skills(skills_dir),
        "wiki": scan_wiki(wiki_dir),
        "team": scan_team(team_dir),
        "docs": scan_docs(docs_dir),
        "tooling": scan_tooling(),
    }
