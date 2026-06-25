"""Claude Code asset installer for ntasker.

ntasker ships its Claude Code skill (``SKILL.md``) and slash-command
loader (``task.md`` + ``_ntasker_loader.py``) inside the package. This
module is the single source of truth for those assets and exposes:

* :func:`render_command` -- substitute ``{COMMAND_NAME}`` / ``{HELPER_PATH}``
  in the slash-command template.
* :func:`expected_files` -- compute target paths + expected SHA256 hashes
  for a given Claude home + command name.
* :func:`scan_status` -- read-only check of installed assets vs. packaged
  expectations (used by ``--check`` and the ``/api/claude-assets/status``
  endpoint).
* :func:`install_assets` -- perform the install with conflict handling,
  ``--dry-run`` support, and timestamped ``.bak`` backups.

Design notes:

* All asset reads go through :func:`importlib.resources.files` -- never
  ``open(__file__)``. The package is the canonical source.
* The slash-command file name is configurable via ``--command-name``;
  the helper file name is fixed (``_ntasker_loader.py``) and only the
  helper path inside ``task.md`` references it. So renaming the slash
  command does not touch the helper.
* No HTTP write endpoint -- installs are user-initiated via the CLI.
  The ``/api/claude-assets/status`` API and the Settings UI card are
  read-only on purpose.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fixed helper filename. The ``--command-name`` flag changes the slash
#: command file (``<name>.md``) but never the helper -- ``task.md`` always
#: points at the same ``_ntasker_loader.py`` so users can rename their
#: slash command without rewriting the helper path.
HELPER_FILENAME = "_ntasker_loader.py"

#: Regex guarding ``--command-name`` against path traversal / injection.
#: We accept ASCII alnum + underscore + hyphen, no slashes, no dots.
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Default ``~/.claude`` home. Override via ``--claude-home`` or
#: ``NTASKER_CLAUDE_HOME``.
DEFAULT_CLAUDE_HOME = Path.home() / ".claude"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_command_name(name: str) -> str:
    """Reject anything that could escape the commands directory.

    Returns the normalised name on success, raises ``ValueError`` on bad input.
    """
    if not name or not _COMMAND_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid command name {name!r}: must match {_COMMAND_NAME_RE.pattern}"
        )
    return name


def resolve_claude_home(override: str | os.PathLike | None) -> Path:
    """Resolve the Claude home directory.

    Precedence: explicit ``override`` > ``NTASKER_CLAUDE_HOME`` env var >
    ``~/.claude``. The result is expanduser-expanded + made absolute, but
    we deliberately do NOT enforce that it equals ``~/.claude`` -- tests
    and ad-hoc setups need to redirect it.

    Symlinks are NOT resolved: when ``~/.claude`` is a symlink (e.g. to a
    dotfiles repo under ``~/Projekte/claude``), we keep the logical
    ``~/.claude`` path rather than its target -- that is the location users
    expect to see and write through.
    """
    if override is not None:
        raw = str(override)
    else:
        raw = os.environ.get("NTASKER_CLAUDE_HOME", str(DEFAULT_CLAUDE_HOME))
    return Path(os.path.abspath(os.path.expanduser(raw)))


# ---------------------------------------------------------------------------
# Asset reads (via importlib.resources)
# ---------------------------------------------------------------------------


def _asset_root():
    """Return the package-data root for ``ntasker/claude_assets``.

    Using ``files("ntasker") / "claude_assets"`` is more robust than
    ``files("ntasker.claude_assets")`` because the asset directory has no
    ``__init__.py`` (it is a data dir, not a sub-package). The latter form
    falls back to the parent package's path on Python 3.12 and yields
    surprising contents.
    """
    return files("ntasker") / "claude_assets"


def read_skill_md() -> str:
    """Return the packaged ``SKILL.md`` content (verbatim, UTF-8)."""
    return (_asset_root() / "skill" / "SKILL.md").read_text(encoding="utf-8")


def read_command_template() -> str:
    """Return the packaged slash-command template (with ``{COMMAND_NAME}`` /
    ``{HELPER_PATH}`` placeholders intact).
    """
    return (_asset_root() / "command" / "task.md.template").read_text(encoding="utf-8")


def read_helper_py() -> str:
    """Return the packaged ``_ntasker_loader.py`` content (verbatim, UTF-8)."""
    return (_asset_root() / "command" / HELPER_FILENAME).read_text(encoding="utf-8")


def render_command(template: str, command_name: str, helper_path: Path | str) -> str:
    """Substitute ``{COMMAND_NAME}`` and ``{HELPER_PATH}`` in the template.

    ``helper_path`` is rendered as-is (no expansion), so callers control
    whether the path lands as ``~/.claude/...`` (literal tilde, what
    Claude Code expects in slash-command frontmatter) or absolute.
    """
    return template.replace("{COMMAND_NAME}", command_name).replace(
        "{HELPER_PATH}", str(helper_path)
    )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256(text: str) -> str:
    """Hex SHA256 of ``text`` encoded as UTF-8. Prefixed ``sha256:``."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str | None:
    """Return ``sha256:<hex>`` of the file or ``None`` if missing."""
    try:
        return sha256(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        # Binary or otherwise undecodable -- treat as drift.
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------
# Plan: which files go where, with what content + expected hash
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetFile:
    """One file in the install plan."""

    path: Path  # absolute target path
    content: str  # rendered content to write
    label: str  # short tag for status output ("skill", "command", "helper")

    @property
    def expected_hash(self) -> str:
        return sha256(self.content)


def _helper_path_for_command(command_name: str) -> str:
    """Path string we substitute into ``task.md`` (literal ``~`` for portability)."""
    # Keep the literal ``~`` so the rendered slash-command file stays
    # user-readable / portable. The actual file is written to the resolved
    # home, but the in-file reference uses the canonical tilde form that
    # Claude Code accepts in ``allowed-tools`` / shell invocations.
    return f"~/.claude/commands/{HELPER_FILENAME}"


def expected_files(claude_home: Path, command_name: str) -> list[AssetFile]:
    """Return the install plan: 3 :class:`AssetFile` entries.

    Order: ``skill``, ``command``, ``helper`` -- so output and tests are
    deterministic.
    """
    validate_command_name(command_name)
    skills_dir = claude_home / "skills" / "ntasker"
    commands_dir = claude_home / "commands"

    helper_ref = _helper_path_for_command(command_name)
    rendered_command = render_command(
        read_command_template(),
        command_name=command_name,
        helper_path=helper_ref,
    )

    return [
        AssetFile(
            path=skills_dir / "SKILL.md",
            content=read_skill_md(),
            label="skill",
        ),
        AssetFile(
            path=commands_dir / f"{command_name}.md",
            content=rendered_command,
            label="command",
        ),
        AssetFile(
            path=commands_dir / HELPER_FILENAME,
            content=read_helper_py(),
            label="helper",
        ),
    ]


# ---------------------------------------------------------------------------
# Status: read-only inspection
# ---------------------------------------------------------------------------


@dataclass
class FileStatus:
    """Status of a single file relative to the packaged expectation."""

    path: Path
    label: str
    installed: bool
    drift: bool
    expected_hash: str
    actual_hash: str | None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "label": self.label,
            "installed": self.installed,
            "drift": self.drift,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
        }


@dataclass
class InstallStatus:
    """Aggregate status across all 3 asset files."""

    installed: bool  # all 3 files exist
    drift: bool  # at least one installed file differs from packaged
    files: list[FileStatus]

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "drift": self.drift,
            "files": [f.to_dict() for f in self.files],
        }


def scan_status(claude_home: Path, command_name: str = "task") -> InstallStatus:
    """Inspect the filesystem and report installed/drift state.

    Read-only. Used by both the ``--check`` CLI mode and the
    ``/api/claude-assets/status`` endpoint.
    """
    plan = expected_files(claude_home, command_name)
    files_status: list[FileStatus] = []
    all_installed = True
    any_drift = False
    for af in plan:
        actual = file_sha256(af.path)
        installed = actual is not None
        drift = installed and actual != af.expected_hash
        if not installed:
            all_installed = False
        if drift:
            any_drift = True
        files_status.append(
            FileStatus(
                path=af.path,
                label=af.label,
                installed=installed,
                drift=drift,
                expected_hash=af.expected_hash,
                actual_hash=actual,
            )
        )
    return InstallStatus(installed=all_installed, drift=any_drift, files=files_status)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


@dataclass
class InstallAction:
    """One action the installer performed (or would perform in dry-run)."""

    path: Path
    label: str
    action: str  # "skip" | "write" | "backup-and-write" | "blocked"
    backup_path: Path | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "label": self.label,
            "action": self.action,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "reason": self.reason,
        }


@dataclass
class InstallResult:
    """Aggregate install outcome."""

    success: bool  # no blocked files
    actions: list[InstallAction]
    dry_run: bool

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "dry_run": self.dry_run,
            "actions": [a.to_dict() for a in self.actions],
        }


def _backup_suffix(now: datetime | None = None) -> str:
    """``.bak.YYYYMMDD-HHMMSS`` -- timestamp only, never user input."""
    when = now or datetime.now()
    return ".bak." + when.strftime("%Y%m%d-%H%M%S")


def install_assets(
    claude_home: Path,
    command_name: str = "task",
    *,
    force: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    """Install (or simulate installing) the 3 asset files.

    Conflict handling per file:

    * No file at target          -> ``write``
    * File exists, hash matches  -> ``skip`` (already up to date)
    * File exists, hash differs:
        - ``force=False``  -> ``blocked``
        - ``force=True``   -> ``backup-and-write`` (backup gets timestamped suffix)

    ``dry_run`` records the action but never touches the filesystem
    (no mkdir, no writes, no backups). Useful for ``--dry-run`` and tests.
    """
    plan = expected_files(claude_home, command_name)
    actions: list[InstallAction] = []
    success = True

    if not dry_run:
        # Make sure parent dirs exist; harmless if they do.
        (claude_home / "skills" / "ntasker").mkdir(parents=True, exist_ok=True)
        (claude_home / "commands").mkdir(parents=True, exist_ok=True)

    for af in plan:
        actual = file_sha256(af.path)
        if actual is None:
            actions.append(
                InstallAction(path=af.path, label=af.label, action="write")
            )
            if not dry_run:
                af.path.write_text(af.content, encoding="utf-8")
            continue

        if actual == af.expected_hash:
            actions.append(
                InstallAction(
                    path=af.path,
                    label=af.label,
                    action="skip",
                    reason="already up to date",
                )
            )
            continue

        # Drift case
        if not force:
            actions.append(
                InstallAction(
                    path=af.path,
                    label=af.label,
                    action="blocked",
                    reason="local content differs from packaged; rerun with --force",
                )
            )
            success = False
            continue

        backup = af.path.with_name(af.path.name + _backup_suffix())
        actions.append(
            InstallAction(
                path=af.path,
                label=af.label,
                action="backup-and-write",
                backup_path=backup,
                reason="forced overwrite of drifted file",
            )
        )
        if not dry_run:
            af.path.replace(backup)
            af.path.write_text(af.content, encoding="utf-8")

    return InstallResult(success=success, actions=actions, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Boot drift warning (best-effort)
# ---------------------------------------------------------------------------


def boot_drift_warning() -> str | None:
    """Return a warning string if installed Claude assets are stale.

    Best-effort: catches everything and returns ``None`` on any error so a
    broken claude-home cannot prevent ``ntasker serve`` from booting. The
    caller (``ntasker.cli.cmd_serve``) prints the result to stderr.
    """
    try:
        claude_home = resolve_claude_home(None)
        status = scan_status(claude_home, command_name="task")
    except Exception:
        return None
    # Stay quiet for users who do not use Claude Code at all.
    if not status.installed:
        return None
    if not status.drift:
        return None
    from ntasker import __version__ as VERSION  # noqa: PLC0415

    return (
        f"[ntasker] Claude Code assets out of date for v{VERSION}. "
        "Run `ntasker install-claude-assets --force` to update."
    )


# ---------------------------------------------------------------------------
# Iteration helper
# ---------------------------------------------------------------------------


def iter_asset_paths(claude_home: Path, command_name: str = "task") -> Iterable[Path]:
    """Yield the 3 absolute target paths in the standard order."""
    for af in expected_files(claude_home, command_name):
        yield af.path
