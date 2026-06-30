"""Discovery of Claude Code project directories as ntasker project names.

Claude Code keeps per-project session state under
``<claude_home>/projects/<encoded-path>/`` where ``<encoded-path>`` is the
project's absolute working directory with every ``/`` replaced by ``-`` --
e.g. ``/home/u/Projekte/medux-online`` becomes
``-home-u-Projekte-medux-online``. That encoding is lossy (a literal ``-`` in
a path component is indistinguishable from a former ``/``), so the real path
is recovered in two escalating steps:

1. The authoritative ``cwd`` field stored on (near) the first line of any
   session ``*.jsonl`` inside the directory. Works even for repos that no
   longer exist on disk.
2. A filesystem-assisted greedy decode of the encoded name -- disambiguates
   ``medux-online`` from ``medux/online`` by checking which directories
   actually exist. Best-effort fallback when no session file carries a ``cwd``.

The resulting absolute path is turned into a project *name* using ``/``
separators, relative to the ``projects_base`` setting when set (``~/Projekte``
-> ``medux-online``), otherwise relative to the user's home directory
(``Projekte/medux-online``). Paths outside that base keep their absolute
(POSIX) form.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ntasker.agents import AGENTS, resolve_home

# Cap how many lines of a session file we scan for a ``cwd`` -- it lives on the
# first line in practice; the cap just bounds the read for pathological files.
_CWD_SCAN_LINES = 200


def _cwd_from_session(directory: Path) -> str | None:
    """Return the ``cwd`` recorded in the first session ``*.jsonl``, or ``None``."""
    try:
        sessions = sorted(directory.glob("*.jsonl"))
    except OSError:
        return None
    for jf in sessions:
        try:
            with jf.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > _CWD_SCAN_LINES:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        cwd = obj.get("cwd")
                        if isinstance(cwd, str) and cwd:
                            return cwd
        except OSError:
            continue
    return None


def _greedy_decode(name: str) -> Path | None:
    """Reconstruct an absolute path from an encoded dir name, FS-assisted.

    Walks the ``-``-joined segments from the filesystem root, greedily taking
    the longest leading run that forms an existing directory. This recovers a
    literal ``-`` inside a path component (e.g. ``medux-online``) as long as
    that directory still exists. Once no existing run remains, the rest is
    joined as a single component -- the result then no longer exists on disk
    and the caller discards it (see :func:`_decode_dir`).
    """
    parts = name.lstrip("-").split("-")
    if not parts or parts == [""]:
        return None
    path = Path("/")
    i = 0
    n = len(parts)
    while i < n:
        matched = False
        for j in range(n, i, -1):
            candidate = path / "-".join(parts[i:j])
            if candidate.is_dir():
                path = candidate
                i = j
                matched = True
                break
        if not matched:
            path = path / "-".join(parts[i:])
            break
    return path


def _decode_dir(entry: Path) -> Path | None:
    """Resolve a ``projects/<encoded>`` directory to the real working dir.

    The session ``cwd`` is authoritative (kept even if the repo was since
    deleted). A purely greedy-decoded path is only trusted when it still
    exists on disk -- otherwise the encoding is ambiguous (a hyphenated leaf
    is indistinguishable from a nested path) and we drop the stale entry
    rather than surface a wrong name.
    """
    cwd = _cwd_from_session(entry)
    if cwd:
        return Path(cwd)
    decoded = _greedy_decode(entry.name)
    if decoded is not None and decoded.is_dir():
        return decoded
    return None


def _resolve_projects_base() -> Path | None:
    """Read the ``projects_base`` setting as an absolute ``Path`` (or ``None``).

    Guarded by design: a missing DB, missing key or bad value all collapse to
    ``None`` (home-relative naming) so discovery never breaks. The ENV override
    ``NTASKER_PROJECTS_BASE`` takes precedence over the DB row. The local import
    keeps this module free of a DB dependency at import time.
    """
    try:
        from ntasker.settings import get_setting  # noqa: PLC0415

        raw = get_setting("projects_base", env_var="NTASKER_PROJECTS_BASE")
    except Exception:
        return None
    if not raw:
        return None
    return Path(os.path.abspath(os.path.expanduser(raw)))


def _path_to_name(path: Path, home: Path, base: Path | None = None) -> str | None:
    """Turn an absolute path into a project name (``/``-joined).

    Relativized against ``base`` when the path lives under it, else against
    ``home``; paths under neither keep their absolute POSIX form. A path equal
    to ``base`` or ``home`` maps to ``None`` (no meaningful project name).
    """
    for root in (base, home):
        if root is None:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        posix = rel.as_posix()
        if not posix or posix == ".":
            return None
        return posix
    return path.as_posix()


def discover_claude_projects(
    claude_home: str | os.PathLike | None = None,
    home: Path | None = None,
    base: Path | None = None,
) -> list[str]:
    """Return sorted, unique project names discovered under ``<claude_home>/projects``.

    Names are relativized against ``base`` (the ``projects_base`` setting) when
    given, else against ``home``. When ``base`` is left ``None`` it is read from
    the setting; pass an explicit ``Path`` to override (or ``home`` only).

    Resilient by design: any unreadable entry is skipped and a missing
    ``projects`` directory yields ``[]`` -- discovery must never break the
    sidebar feed it serves.
    """
    if home is None:
        home = Path.home()
    if base is None:
        base = _resolve_projects_base()
    try:
        root = resolve_home(AGENTS["claude"], claude_home) / "projects"
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        return []

    names: set[str] = set()
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            path = _decode_dir(entry)
            if path is None:
                continue
            name = _path_to_name(path, home, base)
            if name:
                names.add(name)
        except OSError:
            continue
    return sorted(names, key=str.casefold)
