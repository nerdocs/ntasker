"""Stop must end a session even when a stray process keeps its PTY open."""

import asyncio
import os
import subprocess

import pytest

from ntasker import claude_runner as cr


def _spawn(cmd: str) -> cr.TermSession:
    master, slave = os.openpty()
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdin=slave, stdout=slave, stderr=slave,
        preexec_fn=cr._child_setup, close_fds=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    sess = cr.TermSession(task_id=999, proc=proc, master_fd=master)
    cr.SESSIONS[999] = sess
    cr._attach_reader(sess)
    return sess


async def _stop_and_wait(sess: cr.TermSession, timeout: float) -> bool:
    cr._stop(sess)
    for _ in range(int(timeout / 0.1)):
        await asyncio.sleep(0.1)
        if not sess.alive:
            return True
    return False


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    cr.SESSIONS.pop(999, None)
    subprocess.run(["pkill", "-9", "-f", "sleep 987"], check=False)


def test_stop_kills_a_sigterm_ignoring_agent(monkeypatch):
    monkeypatch.setattr(cr, "STOP_GRACE_SECONDS", 0.3)

    async def run():
        sess = _spawn("trap '' TERM; sleep 987")
        await asyncio.sleep(0.2)
        assert sess.alive
        assert await _stop_and_wait(sess, 3.0)
        assert sess.exit_code == -9

    asyncio.run(run())


def test_stop_ends_session_held_open_by_detached_grandchild(monkeypatch):
    """Agent exited, but a setsid'd job still holds the PTY: Stop still ends it."""
    monkeypatch.setattr(cr, "STOP_GRACE_SECONDS", 0.3)

    async def run():
        sess = _spawn("setsid sleep 987 & sleep 0.2; exit 0")
        await asyncio.sleep(0.6)
        assert sess.alive  # PTY never closed -- the grandchild holds it
        assert await _stop_and_wait(sess, 3.0)

    asyncio.run(run())
