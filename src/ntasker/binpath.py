"""Find external tools when the server runs with a minimal ``PATH``.

A supervised ``ntasker serve`` does not inherit the login shell's ``PATH``:
launchd starts LaunchAgents with ``/usr/bin:/bin:/usr/sbin:/sbin`` and a
systemd user unit gets a similarly bare one. Tools installed by Homebrew,
uv, cargo & co. (``pandoc``, ``git`` from Homebrew, ``uv``, ``node``) are
then invisible to ``shutil.which`` -- the Word export reported "pandoc is not
installed" although it was.

Two layers fix that for good, independent of how the service was installed:

* :func:`extend_path` -- called once when ``serve`` starts -- appends the
  well-known install directories that exist to ``os.environ["PATH"]``. Every
  lookup and every child process (agent sessions included) sees them.
* :func:`find_tool` -- for lookups outside ``serve`` -- checks ``PATH`` first,
  then the same directories.

Directories are *appended*, so whatever the user's ``PATH`` already lists
keeps precedence.
"""

from __future__ import annotations

import os
import shutil

#: Install directories of the common package managers, best first.
#: ``~`` is expanded at call time.
TOOL_DIRS: tuple[str, ...] = (
    "~/.local/bin",  # uv, pipx, native installers
    "/opt/homebrew/bin",  # Homebrew on Apple silicon
    "/opt/homebrew/sbin",
    "/usr/local/bin",  # Homebrew on Intel, manual installs
    "/opt/local/bin",  # MacPorts
    "/home/linuxbrew/.linuxbrew/bin",  # Homebrew on Linux
    "~/.cargo/bin",
    "~/.cabal/bin",  # pandoc via cabal
    "/snap/bin",
    "/usr/bin",
    "/bin",
)


def tool_dirs() -> list[str]:
    """:data:`TOOL_DIRS` with ``~`` expanded."""
    return [os.path.expanduser(d) for d in TOOL_DIRS]


def extend_path(environ: dict[str, str] | None = None) -> list[str]:
    """Append the existing :data:`TOOL_DIRS` missing from ``PATH``.

    Mutates ``environ`` (default ``os.environ``) and returns the directories
    it added. Idempotent: a second call adds nothing.
    """
    env = os.environ if environ is None else environ
    current = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    seen = {os.path.normpath(p) for p in current}
    added = []
    for d in tool_dirs():
        if os.path.normpath(d) not in seen and os.path.isdir(d):
            added.append(d)
            seen.add(os.path.normpath(d))
    if added:
        env["PATH"] = os.pathsep.join(current + added)
    return added


def find_tool(name: str) -> str | None:
    """Absolute path of executable ``name``: ``PATH`` first, then
    :data:`TOOL_DIRS`. ``None`` when it is nowhere."""
    found = shutil.which(name)
    if found:
        return found
    for d in tool_dirs():
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None
