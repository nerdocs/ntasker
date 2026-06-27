#!/usr/bin/env python3
"""Loader for /task <id> slash command.

Fetches a task from ntasker and prints it as Markdown for injection into
the Claude session prompt. Tries the running server first, then falls
back to the ``ntasker`` CLI (which itself resolves the DB path via
``--db`` / ``NTASKER_DB`` / ``platformdirs`` -- so this loader does not
need to know anything about the filesystem layout).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

SERVER_URL = "http://127.0.0.1:8766/api/tasks/{tid}"


def load_via_server(tid: str) -> dict | None:
    try:
        with urllib.request.urlopen(SERVER_URL.format(tid=tid), timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def set_wip_via_server(tid: str) -> bool:
    """PATCH the task to ``phase=wip`` via the running server."""
    try:
        req = urllib.request.Request(
            SERVER_URL.format(tid=tid),
            data=json.dumps({"phase": "wip"}).encode("utf-8"),
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


def set_wip_via_cli(tid: str) -> bool:
    """PATCH the task to ``phase=wip`` via the ``ntasker`` CLI."""
    if shutil.which("ntasker") is None:
        return False
    try:
        proc = subprocess.run(
            ["ntasker", "patch", str(tid), "--phase", "wip"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return proc.returncode == 0


def load_via_cli(tid: str) -> dict | None:
    if shutil.which("ntasker") is None:
        return None
    try:
        proc = subprocess.run(
            ["ntasker", "show", str(tid), "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def try_autostart() -> bool:
    """Attempt to start the ntasker server in the background.

    Returns True if the server answers /healthz after the spawn (also
    True if a server was already running -- ``serve --detach`` is
    idempotent). Returns False if the CLI is missing or the spawn never
    produced a live server within the CLI's own deadline.
    """
    if shutil.which("ntasker") is None:
        return False
    try:
        proc = subprocess.run(
            ["ntasker", "serve", "--detach"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return proc.returncode == 0


def resolve_projects_base() -> str | None:
    """Resolve the ``projects_base`` setting: env override first, then the
    running server's settings endpoint. Returns ``None`` if unset or
    unreachable -- the mismatch check then degrades to home-relative only.
    """
    base = os.environ.get("NTASKER_PROJECTS_BASE")
    if base:
        return base
    try:
        url = "http://127.0.0.1:8766/api/settings/projects_base"
        with urllib.request.urlopen(url, timeout=2) as r:
            row = json.load(r)
        return (row.get("value") or None) if isinstance(row, dict) else None
    except Exception:
        return None


def _expected_dirs(project: str, base: str | None) -> list[str]:
    """Candidate absolute dirs a task's project *name* could map to.

    Mirrors :func:`ntasker.projects._path_to_name` in reverse: a name is
    either an absolute path, ``<projects_base>/<name>`` or ``~/<name>``.
    """
    if os.path.isabs(project):
        return [os.path.normpath(project)]
    home = os.path.expanduser("~")
    cands = []
    if base:
        cands.append(os.path.normpath(os.path.join(os.path.expanduser(base), project)))
    cands.append(os.path.normpath(os.path.join(home, project)))
    return cands


def project_mismatch(project: str | None, base: str | None) -> tuple[str, str] | None:
    """Detect that the cwd is not inside the task's project directory.

    Returns ``(expected_dir, cwd)`` on a real mismatch, else ``None``.
    Stays silent when no project is set or when the project cannot be
    located on disk -- never cry wolf on a label-only project name.
    """
    if not project:
        return None
    cwd = os.path.normpath(os.getcwd())
    existing = [c for c in _expected_dirs(project, base) if os.path.isdir(c)]
    if not existing:
        return None  # can't tell where this project lives -> stay quiet
    for cand in existing:
        if cwd == cand or cwd.startswith(cand + os.sep):
            return None  # already inside the right tree
    return (existing[0], cwd)


def render_warning(project: str, expected: str, cwd: str) -> str:
    """Blockquote callout prepended to the loaded card on a mismatch.

    Offers the concrete ``/cd`` command so the agent can suggest the exact
    line for the user to type -- the session's working directory can only
    be moved by the user via ``/cd``, not by a Bash ``cd`` from the agent.
    """
    return (
        f"> WARNUNG -- Projekt-Mismatch: dieser Task ist Projekt `{project}`\n"
        f"> zugeordnet (Verzeichnis `{expected}`), aber du arbeitest gerade in\n"
        f"> `{cwd}`. Frage den User, ob das ok ist, **bevor** du startest --\n"
        f"> oder biete an, ins richtige Verzeichnis zu wechseln: schlage ihm\n"
        f"> vor, `/cd {expected}` einzugeben, damit die Session dort\n"
        f"> weiterläuft.\n"
    )


def render(data: dict) -> str:
    lines = [
        f'## #{data["id"]} {data["title"]}',
        "",
        f'- **Projekt:** {data.get("project") or "(cross-project)"}',
        f'- **Status:** {data["status"]} | '
        f'**Phase:** {data.get("phase") or "-"} | '
        f'**Prioritaet:** {data.get("priority") or "normal"}',
        f'- **Tags:** {", ".join(data.get("tags") or []) or "-"}',
        f'- **Archiviert:** {bool(data.get("archived"))}',
        f'- **Erstellt:** {data.get("created_at") or "-"}',
    ]
    if data.get("completed_at"):
        lines.append(f'- **Abgeschlossen:** {data["completed_at"]}')
    lines.extend(
        [
            "",
            "### Beschreibung",
            "",
            data.get("description") or "_(keine Beschreibung)_",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: _ntasker_loader.py <id>", file=sys.stderr)
        return 2
    raw = argv[1].strip()
    # Accept both "187" and "#187" -- the slash-command argument may keep
    # the leading "#" from how task IDs are referenced everywhere else.
    if not re.fullmatch(r"#?\d+", raw):
        print(f"Invalid task id: {raw!r}", file=sys.stderr)
        return 2
    tid = raw.lstrip("#")
    via = "server"
    data = load_via_server(tid)
    if data is None:
        # Server not reachable: try to spawn it in the background so this
        # AND future calls in the same session are fast, plus the web UI
        # at http://127.0.0.1:8766 becomes available. Idempotent: a no-op
        # if a server was already up by the time we got here.
        if try_autostart():
            data = load_via_server(tid)
        if data is None:
            data = load_via_cli(tid)
            via = "cli"
    if data is None:
        print(
            f"Task #{tid} not found.\n"
            "  - Is the server reachable at http://127.0.0.1:8766?\n"
            "    -> start it with `ntasker serve`.\n"
            "  - If ntasker is not installed yet:\n"
            "    -> `uv tool install ntasker` (or `pip install --user ntasker`)\n"
            "  - For a non-default DB path: `NTASKER_DB=/path/to/tasks.db ntasker show "
            f"{tid} --json`",
            file=sys.stderr,
        )
        return 1
    # Detect a project mismatch BEFORE marking the task in progress: if the
    # current directory is not inside the task's project, the agent must ask
    # the user first -- so defer phase=wip until they confirm.
    mismatch = None
    if data.get("project"):
        mismatch = project_mismatch(data["project"], resolve_projects_base())

    # Starting work via /task moves the task to "in progress" (phase=wip).
    # Best-effort: skip archived/closed tasks (don't resurrect them), skip
    # on a project mismatch (wait for the user's OK), no-op if already wip;
    # a failed write must never break the load.
    if (
        mismatch is None
        and not data.get("archived")
        and data.get("status") != "done"
        and data.get("phase") != "wip"
    ):
        ok = set_wip_via_cli(tid) if via == "cli" else set_wip_via_server(tid)
        if ok:
            data["phase"] = "wip"

    out = render(data)
    if mismatch is not None:
        expected, cwd = mismatch
        out = render_warning(data["project"], expected, cwd) + "\n" + out
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
