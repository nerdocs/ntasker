"""What a run changed: the working trees vs. the commits the run started on.

The Diff view (``GET /api/tasks/{id}/diff``) shows every file the agent
touched since its run was spawned, across every directory the run holds --
the project's own plus its directory locks (see :mod:`ntasker.locks`).
Tracked changes are a ``git diff`` against the base commit recorded per
directory at spawn (so the agent's own commits still count), untracked files
an all-additions patch. Not a git repo, git missing or any git error degrades
to "no diff", never to an exception.

The baselines live on the task (``tasks.run_baselines``): a JSON object
``{directory: sha | null}`` -- ``null`` for a repo without commits yet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

GIT_TIMEOUT = 15


def _git(cwd: str, *args: str, ok: tuple[int, ...] = (0,)) -> str | None:
    """Run ``git -C cwd args``; stdout when the exit code is in ``ok``, else ``None``."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode in ok else None


def git_head(cwd: str) -> str | None:
    """``HEAD``'s sha, or ``None`` (not a repo, no commits yet, git missing)."""
    return (_git(cwd, "rev-parse", "HEAD") or "").strip() or None


def _is_repo(cwd: str) -> bool:
    return (_git(cwd, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def _split_patch(patch: str) -> list[str]:
    """One ``git diff`` output -> one chunk per file (each starts with ``diff --git``)."""
    return [c for c in re.split(r"^(?=diff --git )", patch, flags=re.M) if c.strip()]


def _describe(chunk: str, *, untracked: bool = False) -> dict:
    """Summarise one per-file patch chunk: path, status and +/- line counts."""
    header, _, body = chunk.partition("\n@@")
    path = None
    m = re.search(r"^\+\+\+ b/(.+)$", header, flags=re.M) or re.search(r"^--- a/(.+)$", header, flags=re.M)
    if m:
        path = m.group(1)
    else:  # binary / mode-only change: fall back to the diff --git line
        m = re.match(r"diff --git a/(.+?) b/", header)
        path = m.group(1) if m else "?"
    if untracked:
        status = "untracked"
    elif re.search(r"^new file mode", header, flags=re.M):
        status = "added"
    elif re.search(r"^deleted file mode", header, flags=re.M):
        status = "deleted"
    elif re.search(r"^rename from ", header, flags=re.M):
        status = "renamed"
    else:
        status = "modified"
    lines = ("@@" + body).splitlines() if body else []
    return {
        "path": path,
        "status": status,
        "additions": sum(1 for line in lines if line.startswith("+")),
        "deletions": sum(1 for line in lines if line.startswith("-")),
        "diff": chunk,
    }


def baselines_for(dirs: list[str]) -> str:
    """Record the run's baselines: ``{dir: HEAD sha | None}`` as the column's JSON."""
    return json.dumps({d: git_head(d) for d in dirs})


def parse_baselines(raw: str | None) -> dict[str, str | None]:
    """Decode a stored ``run_baselines`` column; anything unreadable is ``{}``."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def run_diff(baselines: dict[str, str | None]) -> dict:
    """The run's changes over all its directories.

    ``{"git": bool, "files": [{dir, path, status, additions, deletions, diff}]}``
    -- ``dir`` is the directory's basename (the project), files keep the
    baselines' directory order. ``git`` is false when none of the directories
    is a repository.
    """
    files: list[dict] = []
    any_git = False
    for cwd, base in baselines.items():
        result = changed_files(cwd, base)
        any_git = any_git or result["git"]
        name = os.path.basename(cwd.rstrip("/")) or cwd
        files.extend({"dir": name, **f} for f in result["files"])
    return {"git": any_git, "files": files}


def changed_files(cwd: str, base: str | None) -> dict:
    """One directory's changes: ``{"git": bool, "files": [{path, status, additions, deletions, diff}]}``.

    ``base`` is the sha recorded at spawn (see :func:`baselines_for`); without
    one (a repo with no commits yet) only untracked files can be reported.
    """
    if not _is_repo(cwd):
        return {"git": False, "files": []}
    files: list[dict] = []
    if base:
        patch = _git(cwd, "diff", "--find-renames", base, "--") or ""
        files.extend(_describe(c) for c in _split_patch(patch))
    untracked = (_git(cwd, "ls-files", "--others", "--exclude-standard") or "").splitlines()
    for path in untracked:
        # --no-index exits 1 when the files differ -- the expected case here.
        chunk = _git(cwd, "diff", "--no-index", "--", "/dev/null", path, ok=(0, 1))
        if chunk:
            files.append(_describe(chunk, untracked=True))
    files.sort(key=lambda f: f["path"])
    return {"git": True, "files": files}
