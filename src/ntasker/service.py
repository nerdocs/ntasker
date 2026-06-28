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

SYSTEMD_SERVICE = "ntasker.service"
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


def serve_command(host: str, port: int, db_path: str | None) -> list[str]:
    """Argv for the supervised server. Absolute interpreter, no shell."""
    cmd = [sys.executable, "-m", "ntasker"]
    if db_path:
        cmd += ["--db", db_path]
    cmd += ["serve", "--host", host, "--port", str(port)]
    return cmd


def self_update_command() -> list[str]:
    """Argv the periodic updater runs."""
    return [sys.executable, "-m", "ntasker", "self-update"]


def resolve_update_command(setting: str | None) -> list[str]:
    """Pick the package-upgrade command.

    Explicit ``update_command`` setting wins (parsed with ``shlex``).
    Otherwise auto-detect:

    * a ``uv tool`` install lives under a ``uv/tools`` venv -> ``uv tool
      upgrade ntasker``;
    * the current interpreter has ``pip`` -> ``pip install -U`` against it,
      so we hit the right environment;
    * no ``pip`` (typical for ``uv``-managed venvs, e.g. ``uv run``) -> fall
      back to ``uv pip install`` targeting this very interpreter; only if
      ``uv`` is also missing do we emit the ``pip`` command anyway, so the
      failure carries a clear message.
    """
    if setting and setting.strip():
        return shlex.split(setting)
    exe = sys.executable.replace("\\", "/").lower()
    if "/uv/tools/" in exe:
        return ["uv", "tool", "upgrade", "ntasker"]
    if _has_pip():
        return [sys.executable, "-m", "pip", "install", "-U", "ntasker"]
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, "-U", "ntasker"]
    return [sys.executable, "-m", "pip", "install", "-U", "ntasker"]


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


def _systemd_units(host: str, port: int, db_path: str | None, auto_update: bool) -> list[UnitFile]:
    d = systemd_user_dir()
    exec_serve = " ".join(shlex.quote(a) for a in serve_command(host, port, db_path))
    units = [
        UnitFile(
            d / SYSTEMD_SERVICE,
            f"""\
[Unit]
Description=ntasker -- local task tracker
After=network.target

[Service]
Type=simple
ExecStart={exec_serve}
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
Description=ntasker -- upgrade from PyPI and restart

[Service]
Type=oneshot
ExecStart={exec_update}
""",
            ),
            UnitFile(
                d / SYSTEMD_UPDATE_TIMER,
                """\
[Unit]
Description=ntasker -- daily auto-update

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


def _launchd_units(host: str, port: int, db_path: str | None, auto_update: bool) -> list[UnitFile]:
    d = launchd_dir()
    serve_plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": serve_command(host, port, db_path),
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    units = [UnitFile(d / f"{LAUNCHD_LABEL}.plist", _plist_dumps(serve_plist))]
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


def install(host: str, port: int, db_path: str | None, auto_update: bool) -> list[str]:
    """Write + enable the units. Returns human-readable log lines."""
    mgr = detect_manager()
    if mgr is None:
        raise RuntimeError(_("Unsupported platform: only systemd (Linux) and launchd (macOS)."))
    log: list[str] = []
    if mgr == "systemd":
        units = _systemd_units(host, port, db_path, auto_update)
        systemd_user_dir().mkdir(parents=True, exist_ok=True)
        for u in units:
            u.path.write_text(u.content)
            log.append(_("wrote {path}").format(path=u.path))
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", SYSTEMD_SERVICE)
        log.append(_("enabled + started {unit}").format(unit=SYSTEMD_SERVICE))
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
        units = _launchd_units(host, port, db_path, auto_update)
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
        for name in (SYSTEMD_SERVICE, SYSTEMD_UPDATE_SERVICE, SYSTEMD_UPDATE_TIMER):
            p = systemd_user_dir() / name
            if p.exists():
                p.unlink()
                log.append(_("removed {path}").format(path=p))
        _systemctl("daemon-reload")
    else:
        for label in (LAUNCHD_LABEL, LAUNCHD_UPDATE_LABEL):
            p = launchd_dir() / f"{label}.plist"
            if p.exists():
                _launchctl("unload", str(p))
                p.unlink()
                log.append(_("removed {path}").format(path=p))
    if not log:
        log.append(_("nothing to remove -- no ntasker units installed."))
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
        for name in (SYSTEMD_SERVICE, SYSTEMD_UPDATE_TIMER):
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
        for label in (LAUNCHD_LABEL, LAUNCHD_UPDATE_LABEL):
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
