"""Input for the PTY must arrive whole, however long it is."""

import asyncio
import os
import subprocess

import pytest

from ntasker import claude_runner as cr


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    cr.SESSIONS.pop(998, None)


def test_long_paste_reaches_the_agent_complete(tmp_path):
    """A paste beyond the PTY's input queue keeps its tail (the end marker)."""
    out = tmp_path / "received"
    master, slave = os.openpty()
    proc = subprocess.Popen(
        ["bash", "-c", f"stty raw -echo; sleep 0.3; cat > {out}"],
        stdin=slave, stdout=slave, stderr=slave,
        preexec_fn=cr._child_setup, close_fds=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    sess = cr.TermSession(task_id=998, proc=proc, master_fd=master)
    paste = b"\x1b[200~" + b"a line of pasted text\n" * 2000 + b"\x1b[201~"

    async def run():
        await asyncio.sleep(0.1)   # let stty switch the PTY to raw
        await cr._write_pty(sess, paste)
        await asyncio.sleep(0.3)

    asyncio.run(run())
    proc.kill()
    proc.wait()
    os.close(master)
    assert out.read_bytes() == paste
