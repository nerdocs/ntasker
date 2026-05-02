"""Lifespan tests -- pin the DB-path resolution under uvicorn's various boot modes.

Regression tests for the v1.3.0 bug where ``uvicorn ntasker.app:app --reload``
crashed at startup because the worker subprocess imported ``ntasker.app``
without re-running the CLI's :func:`ntasker.cli.cmd_serve`, leaving
:data:`ntasker.db.DB_PATH` unbound.

Three boot paths are exercised:

* **Test A** -- import ``ntasker.app:app`` in a fresh subprocess, build a
  TestClient, hit ``/``. No prior ``set_db_path()`` call. Mirrors the
  ``uvicorn ntasker.app:app`` (no ``--reload``) direct-import path.

* **Test B** -- spawn ``python -m uvicorn ntasker.app:app --port <free>``
  as a real subprocess with ``NTASKER_DB`` in the env, poll ``/`` until
  it returns 200, then kill it and inspect stderr for ``Application startup failed``.
  Mirrors the actual server boot path that 1.3.0 broke.

* **Test C** (intentionally skipped) -- ``--reload`` spawns a WatchFiles
  parent + worker. The worker subprocess covers exactly the boot path
  Test B already exercises (fresh import, ENV-only DB path); the parent
  process does not run the lifespan. Adding ``--reload`` here only adds
  flakiness from the WatchFiles inotify loop without testing anything
  Test B does not already cover. Verified manually instead (see commit).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path


def _free_port() -> int:
    """Bind to port 0 to let the kernel pick a free TCP port, then close."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http_200(url: str, timeout: float = 15.0) -> int | None:
    """Poll ``url`` until it returns 200 or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
                return int(resp.status)
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return None


# ---------------------------------------------------------------------------
# Test A -- direct app import in a fresh subprocess (no CLI involvement)
# ---------------------------------------------------------------------------


def test_a_direct_import() -> None:
    """Mirror ``uvicorn ntasker.app:app`` (no --reload, no CLI bootstrap).

    The script runs in a fresh interpreter so ``DB_PATH`` is unbound at
    import time -- exactly the situation the 1.3.0 bug crashed in. With
    the lifespan-safe fix, the startup hook re-resolves and the GET /
    must return 200.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "tasks.db"
        env = {**os.environ, "NTASKER_DB": str(tmp_db)}
        env.pop("PYTHONDONTWRITEBYTECODE", None)

        script = textwrap.dedent(
            """
            import sys
            from fastapi.testclient import TestClient

            # NB: do NOT call ntasker.db.set_db_path() here -- the whole
            # point of this test is that the app boots without a prior bind.
            from ntasker.app import app

            with TestClient(app) as client:
                r = client.get("/")
                if r.status_code != 200:
                    print(f"FAIL status={r.status_code} body={r.text[:200]}")
                    sys.exit(1)
                if "ntasker" not in r.text:
                    print("FAIL: brand string missing from /")
                    sys.exit(1)
            print("OK")
            """
        ).strip()

        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if proc.returncode != 0 or "OK" not in proc.stdout:
            raise AssertionError(
                "Test A failed -- direct import + TestClient must boot the lifespan.\n"
                f"  rc={proc.returncode}\n"
                f"  stdout={proc.stdout!r}\n"
                f"  stderr={proc.stderr!r}"
            )
        # The DB file MUST have been created -- proves the lifespan resolved
        # the ENV path and hit init_db().
        if not tmp_db.exists():
            raise AssertionError(
                f"Test A: app booted but {tmp_db} not created -- DB path resolution broken"
            )
    print("OK Test A: direct import + TestClient (no prior set_db_path) -> 200")


# ---------------------------------------------------------------------------
# Test B -- real uvicorn subprocess with ENV-only DB path
# ---------------------------------------------------------------------------


def test_b_uvicorn_subprocess() -> None:
    """Spawn ``python -m uvicorn ntasker.app:app`` and wait for HTTP 200.

    This is the closest thing to ``ntasker serve`` we can run inside a
    test without binding the project's default port. ``NTASKER_DB`` is
    the only DB hint; the worker has no CLI ancestor.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "tasks.db"
        port = _free_port()
        env = {**os.environ, "NTASKER_DB": str(tmp_db)}

        proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ntasker.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        try:
            status = _wait_for_http_200(f"http://127.0.0.1:{port}/", timeout=15.0)
            if status != 200:
                # Capture whatever uvicorn already wrote to stderr.
                try:
                    proc.terminate()
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                raise AssertionError(
                    f"Test B failed -- /  did not respond 200 within 15s "
                    f"(got {status!r}).\n  stdout={out!r}\n  stderr={err!r}"
                )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
            else:
                out, err = proc.communicate()

        # Lifespan errors print this exact phrase; pin it so a regression
        # that boots-then-crashes-on-first-request is still caught.
        if "Application startup failed" in err:
            raise AssertionError(
                f"Test B: uvicorn reported startup failure.\n  stderr={err!r}"
            )
        if not tmp_db.exists():
            raise AssertionError(
                f"Test B: server responded but {tmp_db} not created -- "
                "lifespan did not bind the ENV-supplied DB path"
            )
    print(f"OK Test B: uvicorn subprocess on :{port} -> GET / 200, no startup error")


def main() -> int:
    test_a_direct_import()
    test_b_uvicorn_subprocess()
    print("\nAll lifespan tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
