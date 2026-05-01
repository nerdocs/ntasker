"""ntasker — lightweight local task tracker for the nerdocs HQ.

Single-user FastAPI + SQLite app, bound to 127.0.0.1. Distributed as a
PyPA-standard src-layout package; CLI entry point ``ntasker`` is wired
in ``pyproject.toml`` and dispatches to :mod:`ntasker.cli`.
"""

from __future__ import annotations

__version__ = "1.1.0"

__all__ = ["__version__"]
