"""ntasker — lightweight local task tracker.

Single-user FastAPI + SQLite app, bound to 127.0.0.1. Distributed as a
PyPA-standard src-layout package; CLI entry point ``ntasker`` is wired
in ``pyproject.toml`` and dispatches to :mod:`ntasker.cli`.
"""

from __future__ import annotations

__version__ = "1.2.2"

__all__ = ["__version__"]
