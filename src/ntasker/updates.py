"""PyPI update check.

Queries the PyPI JSON API for the latest released ``ntasker`` version and
compares it against the running :data:`ntasker.__version__`. The result is
cached in memory for :data:`_TTL` seconds, so a long-running (production)
server hits PyPI at most once a day, while a freshly (re)started server --
e.g. a ``ntasker serve --reload`` dev server -- re-checks on every boot
because the in-memory cache starts empty.

Network failures are non-fatal: offline simply means "no update info"
(``latest`` stays ``None`` and ``error`` carries the reason).
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable

import httpx

from ntasker import __version__

PYPI_URL = "https://pypi.org/pypi/ntasker/json"
_TTL = 24 * 60 * 60  # re-check at most once a day on a long-running server
_TIMEOUT = 4.0  # keep a slow/offline PyPI from stalling the caller

_lock = threading.Lock()
_cache: dict | None = None
_checked_at: float = 0.0


def _parse(v: str) -> tuple[int, ...]:
    """Best-effort version parse: leading dotted integer components.

    ``"2.11.0"`` -> ``(2, 11, 0)``; a trailing pre-release/local suffix
    (``"2.12.0rc1"``) truncates the parse at the first non-integer chunk.
    Good enough for the simple ``x.y.z`` SemVer scheme ntasker ships under.
    """
    parts: list[int] = []
    for chunk in v.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _parse(latest) > _parse(current)


def _fetch() -> dict:
    """Hit PyPI once and build the result dict. Never raises."""
    result: dict = {
        "current": __version__,
        "latest": None,
        "update_available": False,
        "error": None,
    }
    try:
        resp = httpx.get(
            PYPI_URL, timeout=_TIMEOUT, headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        latest = resp.json()["info"]["version"]
        result["latest"] = latest
        result["update_available"] = _is_newer(latest, __version__)
    except Exception as exc:  # noqa: BLE001 -- offline / parse errors stay non-fatal
        result["error"] = str(exc)
    return result


def check(force: bool = False) -> dict:
    """Return cached update info, refreshing from PyPI when stale.

    Thread-safe. With ``force=True`` the TTL is ignored and PyPI is queried
    unconditionally. The returned dict is a copy, safe for the caller to
    mutate or serialise.
    """
    global _cache, _checked_at
    with _lock:
        # Inlined rather than held in a `fresh` flag so the None-check
        # actually narrows `_cache` for the return below.
        if _cache is not None and not force and (time.time() - _checked_at) < _TTL:
            return dict(_cache)
        _cache = _fetch()
        _checked_at = time.time()
        return dict(_cache)


# The one background self-update (the top bar's "Install update" button;
# ``ntasker self-update`` runs the same command in the foreground). Mirrors
# ``plugins.start_install``: one at a time, the last result stays until the
# next start or a server restart.
_job_lock = threading.Lock()
_job: dict | None = None


def update_job() -> dict | None:
    """The running or last self-update: ``{state, cmd, output, restart}``
    with ``state`` in ``running`` / ``done`` / ``failed``."""
    with _job_lock:
        return dict(_job) if _job else None


def start_update(restart_ok: Callable[[], bool]) -> dict:
    """Upgrade the package in the background, then restart the supervised
    service (``restart`` in the job says whether one is installed -- without
    it the user has to restart ntasker by hand). ``restart_ok`` is asked
    again right before the restart: a task session started during the
    upgrade must not be killed by it -- then ``restart`` flips to ``False``
    and the user restarts by hand. Raises ``RuntimeError`` while another
    update runs."""
    global _job
    from ntasker import service  # noqa: PLC0415
    from ntasker.settings import get_setting  # noqa: PLC0415

    cmd = service.resolve_update_command(get_setting("update_command"))
    restart = service.service_installed()
    with _job_lock:
        if _job and _job["state"] == "running":
            raise RuntimeError("an update is already running")
        _job = {"state": "running", "cmd": " ".join(cmd), "output": "", "restart": restart}
        job = dict(_job)
    threading.Thread(target=_run_update, args=(cmd, restart, restart_ok), daemon=True).start()
    return job


def _run_update(cmd: list[str], restart: bool, restart_ok: Callable[[], bool]) -> None:
    from ntasker import service  # noqa: PLC0415

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603 -- user-configured / auto-detected
        state, output = ("done" if proc.returncode == 0 else "failed"), proc.stderr or proc.stdout
    except OSError as exc:
        state, output = "failed", str(exc)
    restart = restart and state == "done" and restart_ok()
    with _job_lock:
        if _job:
            _job.update(state=state, output=output.strip()[-2000:], restart=restart)
    # The package is upgraded on disk; the supervisor re-spawns us on the new
    # code. Being torn down here is expected (KillMode=control-group).
    if restart:
        service.restart_service()
