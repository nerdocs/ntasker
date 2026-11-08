"""OS service integration + self-update for ntasker.

ntasker ``serve`` is a long-running daemon, unlike a one-shot CLI -- so it
benefits from a process supervisor that starts it at login and restarts it
on crash. This module generates and installs the native unit files:

* **Linux** -> ``systemd --user`` units in ``~/.config/systemd/user/``.
* **macOS** -> ``launchd`` LaunchAgents in ``~/Library/LaunchAgents/``.

With ``--auto-update`` a second, periodic unit is installed that runs
``ntasker self-update`` daily (upgrade from PyPI, then restart the service).

We never overwrite a running daemon in place: ``self-update`` upgrades the
package, *then* restarts the supervised service so the new code is picked
up cleanly -- safe for the open SQLite DB.

Why ``sys.executable -m ntasker`` and not a bare ``ntasker`` path: the
former is an absolute interpreter path that stays valid across ``uv tool
upgrade`` / ``pipx upgrade`` (the tool venv keeps its location), and works
even when ``~/.local/bin`` is not on the unit's ``PATH``.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ntasker.i18n import _

# Stable reverse-DNS label for launchd; also the plist file stem.
LAUNCHD_LABEL = "at.nerdocs.ntasker"
LAUNCHD_UPDATE_LABEL = "at.nerdocs.ntasker-update"
# ``ntasker worker`` (team member's machine) -- a unit of its own, so a host
# can run a team server and a worker side by side.
LAUNCHD_WORKER_LABEL = "at.nerdocs.ntasker-worker"

SYSTEMD_SERVICE = "ntasker.service"
SYSTEMD_WORKER_SERVICE = "ntasker-worker.service"
SYSTEMD_UPDATE_SERVICE = "ntasker-update.service"
SYSTEMD_UPDATE_TIMER = "ntasker-update.timer"


# ---------------------------------------------------------------------------
# Platform + command helpers
# ---------------------------------------------------------------------------


def detect_manager() -> str | None:
    """Return ``"systemd"``, ``"launchd"`` or ``None`` for this OS.

    Linux is assumed to use systemd (the only supervisor we generate units
    for); ``None`` signals an unsupported platform so callers can bail with
    a clear message instead of writing files nobody reads.
    """
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return None


def serve_command(host: str, port: int, db_path: str | None, worker: bool = False) -> list[str]:
    """Argv for the supervised daemon -- the server, or with ``worker`` a team
    member's ``ntasker worker`` (server URL and key from its settings).
    Absolute interpreter, no shell."""
    cmd = [sys.executable, "-m", "ntasker"]
    if db_path:
        cmd += ["--db", db_path]
    if worker:
        return [*cmd, "worker"]
    cmd += ["serve", "--host", host, "--port", str(port)]
    return cmd


def self_update_command() -> list[str]:
    """Argv the periodic updater runs."""
    return [sys.executable, "-m", "ntasker", "self-update"]


def resolve_update_command(setting: str | None) -> list[str]:
    """Pick the package-upgrade command.

    Explicit ``update_command`` setting wins (parsed with ``shlex``).
    Otherwise auto-detect:

    * an editable install (a development checkout) is re-installed from
      that checkout -- never replaced by the PyPI release;
    * a ``uv tool`` install lives under a ``uv/tools`` venv -> ``uv tool
      upgrade ntasker``; if it was built from a local checkout (or git /
      URL), ``uv tool install --upgrade ntasker[extras]`` switches it back
      to PyPI;
    * the current interpreter has ``pip`` -> ``pip install -U`` against it,
      so we hit the right environment;
    * no ``pip`` (typical for ``uv``-managed venvs, e.g. ``uv run``) -> fall
      back to ``uv pip install`` targeting this very interpreter; only if
      ``uv`` is also missing do we emit the ``pip`` command anyway, so the
      failure carries a clear message.
    """
    if setting and setting.strip():
        return shlex.split(setting)
    if install_needs_restart():
        # A tool installed from a local checkout / git / URL is pinned to that
        # source: ``uv tool upgrade`` just rebuilds it and never sees PyPI.
        # Re-install from the index instead (keeping the extras); afterwards
        # the receipt is a plain registry one and ``upgrade`` works again.
        req = uv_tool_requirement()
        if req is not None and not _is_registry_requirement(req):
            extras = req.get("extras") or []
            spec = f"ntasker[{','.join(extras)}]" if extras else "ntasker"
            return ["uv", "tool", "install", "--upgrade", spec]
        return ["uv", "tool", "upgrade", "ntasker"]
    checkout = editable_source()
    if checkout:
        if _has_pip():
            return [sys.executable, "-m", "pip", "install", "-e", checkout]
        if find_uv():
            return ["uv", "pip", "install", "--python", sys.executable, "-e", checkout]
        return [sys.executable, "-m", "pip", "install", "-e", checkout]
    if _has_pip():
        return [sys.executable, "-m", "pip", "install", "-U", "ntasker"]
    if find_uv():
        return ["uv", "pip", "install", "--python", sys.executable, "-U", "ntasker"]
    return [sys.executable, "-m", "pip", "install", "-U", "ntasker"]


def resolve_install_command(extra: str, packages: list[str]) -> list[str]:
    """Command that installs the ``ntasker[extra]`` packages ``packages``
    into the running interpreter's environment -- the same detection as
    :func:`resolve_update_command`.

    Inside a ``uv tool`` environment the extra is added to the tool's receipt
    (``uv tool install 'ntasker[extra]'``) so ``uv tool upgrade`` keeps it;
    ``uv pip install`` would be dropped by the next upgrade. That command
    re-resolves ntasker itself too, so the caller should restart afterwards
    (:func:`install_needs_restart`).
    """
    if install_needs_restart():
        return ["uv", "tool", "install", f"ntasker[{extra}]"]
    if _has_pip():
        return [sys.executable, "-m", "pip", "install", *packages]
    if find_uv():
        return ["uv", "pip", "install", "--python", sys.executable, *packages]
    return [sys.executable, "-m", "pip", "install", *packages]


def _uv_candidates() -> list[str]:
    """Places the uv installers put ``uv`` -- checked when it is not on
    ``PATH``, as under launchd (``/usr/bin:/bin:/usr/sbin:/sbin``).

    ``$UV`` and ``$XDG_BIN_HOME`` first, then the shared
    :func:`ntasker.agents.well_known_bin_dirs`, then ``~/.cargo/bin``."""
    from ntasker.agents import well_known_bin_dirs  # noqa: PLC0415

    cands = [os.environ.get("UV", "")]
    if os.environ.get("XDG_BIN_HOME"):
        cands.append(os.path.join(os.environ["XDG_BIN_HOME"], "uv"))
    cands += [os.path.join(d, "uv") for d in well_known_bin_dirs()]
    cands.append(os.path.join(os.path.expanduser("~"), ".cargo", "bin", "uv"))
    return [c for c in cands if c]


def find_uv() -> str | None:
    """Absolute path of the ``uv`` binary, or ``None`` when there is none."""
    found = shutil.which("uv")
    if found:
        return found
    for cand in _uv_candidates():
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def runnable(cmd: list[str]) -> list[str]:
    """``cmd`` ready for ``subprocess``: a bare ``uv`` becomes its absolute
    path, so the update also works when the service's ``PATH`` lacks
    ``~/.local/bin``. Everything else passes through unchanged."""
    if cmd and cmd[0] == "uv":
        uv = find_uv()
        if uv:
            return [uv, *cmd[1:]]
    return cmd


def install_needs_restart() -> bool:
    """Whether :func:`resolve_install_command` may replace ntasker's own
    files (a ``uv tool`` environment), so the server should restart after."""
    return "/uv/tools/" in sys.executable.replace("\\", "/").lower()


# Receipt keys that pin a requirement to a non-index source.
_LOCAL_SOURCE_KEYS = ("directory", "path", "editable", "git", "url")


def uv_tool_requirement(prefix: str | None = None) -> dict | None:
    """The ``ntasker`` entry of the ``uv tool`` receipt, or ``None`` when
    there is none (not a uv tool env, unreadable receipt)."""
    import tomllib  # noqa: PLC0415

    receipt = Path(prefix or sys.prefix) / "uv-receipt.toml"
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if isinstance(req, dict) and req.get("name") == "ntasker":
            return req
    return None


def _is_registry_requirement(req: dict) -> bool:
    """Whether a receipt requirement resolves from the package index."""
    return not any(key in req for key in _LOCAL_SOURCE_KEYS)


def local_install_source(prefix: str | None = None) -> str | None:
    """The local checkout / git / URL a ``uv tool`` install of ntasker was
    built from, or ``None`` for a PyPI install (or no uv tool env)."""
    req = uv_tool_requirement(prefix)
    if req is None:
        return None
    for key in _LOCAL_SOURCE_KEYS:
        if key in req:
            return str(req[key])
    return None


def editable_source() -> str | None:
    """The checkout an editable install of ntasker points to (from its
    ``direct_url.json``, PEP 610), or ``None`` for a regular install."""
    import json  # noqa: PLC0415
    from importlib.metadata import PackageNotFoundError, distribution  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415
    from urllib.request import url2pathname  # noqa: PLC0415

    try:
        info = json.loads(distribution("ntasker").read_text("direct_url.json") or "{}")
    except (PackageNotFoundError, ValueError):
        return None
    url = info.get("url", "")
    if not info.get("dir_info", {}).get("editable") or not url.startswith("file:"):
        return None
    return url2pathname(urlparse(url).path)


def _has_pip() -> bool:
    """Whether ``pip`` is importable in the current interpreter."""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("pip") is not None


# ---------------------------------------------------------------------------
# Unit-file paths
# ---------------------------------------------------------------------------


def systemd_user_dir() -> Path:
    return Path(os.path.expanduser("~/.config/systemd/user"))


def launchd_dir() -> Path:
    return Path(os.path.expanduser("~/Library/LaunchAgents"))


@dataclass
class UnitFile:
    path: Path
    content: str


# ---------------------------------------------------------------------------
# systemd unit generation
# ---------------------------------------------------------------------------


def _systemd_units(
    host: str, port: int, db_path: str | None, auto_update: bool, worker: bool = False,
    allowed_host: str | None = None,
) -> list[UnitFile]:
    d = systemd_user_dir()
    exec_serve = " ".join(shlex.quote(a) for a in serve_command(host, port, db_path, worker))
    what = "worker for a team server" if worker else "local task tracker"
    # The public host name a reverse proxy forwards (see ntasker.middleware).
    env = f"Environment=NTASKER_ALLOWED_HOSTS={allowed_host}\n" if allowed_host else ""
    units = [
        UnitFile(
            d / (SYSTEMD_WORKER_SERVICE if worker else SYSTEMD_SERVICE),
            f"""\
[Unit]
Description=nTasker -- {what}
After=network.target

[Service]
Type=simple
{env}ExecStart={exec_serve}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
""",
        )
    ]
    if auto_update:
        exec_update = " ".join(shlex.quote(a) for a in self_update_command())
        units += [
            UnitFile(
                d / SYSTEMD_UPDATE_SERVICE,
                f"""\
[Unit]
Description=nTasker -- upgrade from PyPI and restart

[Service]
Type=oneshot
ExecStart={exec_update}
""",
            ),
            UnitFile(
                d / SYSTEMD_UPDATE_TIMER,
                """\
[Unit]
Description=nTasker -- daily auto-update

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
""",
            ),
        ]
    return units


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 -- fixed argv, user-scoped
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# launchd plist generation
# ---------------------------------------------------------------------------


def _launchd_units(
    host: str, port: int, db_path: str | None, auto_update: bool, worker: bool = False,
    allowed_host: str | None = None,
) -> list[UnitFile]:
    d = launchd_dir()
    label = LAUNCHD_WORKER_LABEL if worker else LAUNCHD_LABEL
    serve_plist = {
        "Label": label,
        "ProgramArguments": serve_command(host, port, db_path, worker),
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    if allowed_host:
        serve_plist["EnvironmentVariables"] = {"NTASKER_ALLOWED_HOSTS": allowed_host}
    units = [UnitFile(d / f"{label}.plist", _plist_dumps(serve_plist))]
    if auto_update:
        update_plist = {
            "Label": LAUNCHD_UPDATE_LABEL,
            "ProgramArguments": self_update_command(),
            # Daily at 04:00; Persistent-like catch-up is launchd's default
            # for missed calendar runs while asleep.
            "StartCalendarInterval": {"Hour": 4, "Minute": 0},
        }
        units.append(UnitFile(d / f"{LAUNCHD_UPDATE_LABEL}.plist", _plist_dumps(update_plist)))
    return units


def _plist_dumps(data: dict) -> str:
    return plistlib.dumps(data).decode("utf-8")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 -- fixed argv, user-scoped
        ["launchctl", *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Public ops: install / uninstall / status / restart
# ---------------------------------------------------------------------------


def install(
    host: str, port: int, db_path: str | None, auto_update: bool, worker: bool = False,
    allowed_host: str | None = None,
) -> list[str]:
    """Write + enable the units. ``worker``: the ``ntasker worker`` unit instead
    of the server's (both can live side by side). ``allowed_host``: the public
    host name a reverse proxy forwards to the server. Returns human-readable
    log lines.

    The worker is not restarted by ``self-update``: a restart would end the
    agent sessions it runs -- restart it by hand when no run is active."""
    mgr = detect_manager()
    if mgr is None:
        raise RuntimeError(_("Unsupported platform: only systemd (Linux) and launchd (macOS)."))
    log: list[str] = []
    if mgr == "systemd":
        units = _systemd_units(host, port, db_path, auto_update, worker, allowed_host)
        systemd_user_dir().mkdir(parents=True, exist_ok=True)
        for u in units:
            u.path.write_text(u.content)
            log.append(_("wrote {path}").format(path=u.path))
        _systemctl("daemon-reload")
        unit = SYSTEMD_WORKER_SERVICE if worker else SYSTEMD_SERVICE
        _systemctl("enable", "--now", unit)
        log.append(_("enabled + started {unit}").format(unit=unit))
        if auto_update:
            _systemctl("enable", "--now", SYSTEMD_UPDATE_TIMER)
            log.append(_("enabled auto-update timer {unit}").format(unit=SYSTEMD_UPDATE_TIMER))
        if not _linger_enabled():
            log.append(
                _(
                    "note: lingering is OFF -- the service stops at logout. Enable with: "
                    "loginctl enable-linger $USER"
                )
            )
    else:  # launchd
        units = _launchd_units(host, port, db_path, auto_update, worker, allowed_host)
        launchd_dir().mkdir(parents=True, exist_ok=True)
        for u in units:
            u.path.write_text(u.content)
            log.append(_("wrote {path}").format(path=u.path))
            _launchctl("unload", str(u.path))  # idempotent reload
            _launchctl("load", str(u.path))
        log.append(_("loaded launchd agents"))
    return log


def uninstall() -> list[str]:
    """Disable + remove all ntasker units. Idempotent."""
    mgr = detect_manager()
    if mgr is None:
        raise RuntimeError(_("Unsupported platform."))
    log: list[str] = []
    if mgr == "systemd":
        _systemctl("disable", "--now", SYSTEMD_UPDATE_TIMER)
        _systemctl("disable", "--now", SYSTEMD_SERVICE)
        _systemctl("disable", "--now", SYSTEMD_WORKER_SERVICE)
        for name in (SYSTEMD_SERVICE, SYSTEMD_WORKER_SERVICE, SYSTEMD_UPDATE_SERVICE, SYSTEMD_UPDATE_TIMER):
            p = systemd_user_dir() / name
            if p.exists():
                p.unlink()
                log.append(_("removed {path}").format(path=p))
        _systemctl("daemon-reload")
    else:
        for label in (LAUNCHD_LABEL, LAUNCHD_WORKER_LABEL, LAUNCHD_UPDATE_LABEL):
            p = launchd_dir() / f"{label}.plist"
            if p.exists():
                _launchctl("unload", str(p))
                p.unlink()
                log.append(_("removed {path}").format(path=p))
    if not log:
        log.append(_("nothing to remove -- no nTasker units installed."))
    return log


def service_installed() -> bool:
    """True iff this process is supervised by an installed ntasker service unit."""
    mgr = detect_manager()
    if mgr == "systemd":
        return (systemd_user_dir() / SYSTEMD_SERVICE).exists()
    if mgr == "launchd":
        return (launchd_dir() / f"{LAUNCHD_LABEL}.plist").exists()
    return False


def restart_service() -> bool:
    """Restart the supervised server if its unit is installed. Best-effort."""
    mgr = detect_manager()
    if mgr == "systemd":
        if not (systemd_user_dir() / SYSTEMD_SERVICE).exists():
            return False
        _systemctl("restart", SYSTEMD_SERVICE)
        return True
    if mgr == "launchd":
        p = launchd_dir() / f"{LAUNCHD_LABEL}.plist"
        if not p.exists():
            return False
        _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
        return True
    return False


def start_service() -> bool:
    """Start the supervised server if its unit is installed. Best-effort.

    Returns ``False`` when no unit is installed (so the caller can hint at
    ``service install``) or the platform is unsupported.
    """
    mgr = detect_manager()
    if mgr == "systemd":
        if not (systemd_user_dir() / SYSTEMD_SERVICE).exists():
            return False
        _systemctl("start", SYSTEMD_SERVICE)
        return True
    if mgr == "launchd":
        p = launchd_dir() / f"{LAUNCHD_LABEL}.plist"
        if not p.exists():
            return False
        _launchctl("start", LAUNCHD_LABEL)
        return True
    return False


def stop_service() -> bool:
    """Stop the supervised server if its unit is installed. Best-effort.

    Mirrors :func:`start_service`. On systemd this stops the unit for this
    session; the next login still starts it (use ``service uninstall`` to
    disable permanently).
    """
    mgr = detect_manager()
    if mgr == "systemd":
        if not (systemd_user_dir() / SYSTEMD_SERVICE).exists():
            return False
        _systemctl("stop", SYSTEMD_SERVICE)
        return True
    if mgr == "launchd":
        p = launchd_dir() / f"{LAUNCHD_LABEL}.plist"
        if not p.exists():
            return False
        _launchctl("stop", LAUNCHD_LABEL)
        return True
    return False


def status() -> list[str]:
    """Return human-readable status lines for installed units."""
    mgr = detect_manager()
    if mgr is None:
        return [_("Unsupported platform.")]
    lines = [_("manager: {mgr}").format(mgr=mgr)]
    if mgr == "systemd":
        for name in (SYSTEMD_SERVICE, SYSTEMD_WORKER_SERVICE, SYSTEMD_UPDATE_TIMER):
            p = systemd_user_dir() / name
            if not p.exists():
                lines.append(_("{name}: not installed").format(name=name))
                continue
            active = _systemctl("is-active", name).stdout.strip() or "unknown"
            enabled = _systemctl("is-enabled", name).stdout.strip() or "unknown"
            lines.append(
                _("{name}: {active} ({enabled})").format(name=name, active=active, enabled=enabled)
            )
    else:
        for label in (LAUNCHD_LABEL, LAUNCHD_WORKER_LABEL, LAUNCHD_UPDATE_LABEL):
            p = launchd_dir() / f"{label}.plist"
            state = _("installed") if p.exists() else _("not installed")
            lines.append(f"{label}: {state}")
    return lines


def _linger_enabled() -> bool:
    """True if ``loginctl`` reports lingering on for the current user."""
    r = subprocess.run(  # noqa: S603 -- fixed argv
        ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
        capture_output=True,
        text=True,
    )
    return "Linger=yes" in r.stdout
