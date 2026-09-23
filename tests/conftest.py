"""Shared test setup.

The runner keeps two in-memory registries of sessions it did not spawn
(:data:`ntasker.claude_runner.EXTERNAL` and :data:`ntasker.sessions.LIVE`).
They are module state, so an entry a test leaves behind marks a task busy --
or a project lane occupied -- for every later test in the run. Clearing them
around each test keeps that isolated.
"""

from __future__ import annotations

import pytest

from ntasker import claude_runner, sessions


@pytest.fixture(autouse=True)
def _clean_session_registries():
    claude_runner.EXTERNAL.clear()
    sessions.LIVE.clear()
    yield
    claude_runner.EXTERNAL.clear()
    sessions.LIVE.clear()
