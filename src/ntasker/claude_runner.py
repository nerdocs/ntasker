"""Drive a Claude Code agent run from the web UI over a WebSocket.

This is the first (and only) async/WebSocket surface in ntasker. It wraps the
`claude-agent-sdk` ``query()`` async iterator: Claude's message stream flows
*down* to the browser as structured events, while the browser can stop the run
*up* the same socket. (A WebSocket rather than a one-way ``StreamingResponse``
so a stop reaches the running agent promptly.)

The SDK (and the ``claude`` CLI it shells out to) are an **optional** dependency.
When either is missing :func:`runner_available` reports the reason and the WS
endpoint refuses cleanly; the rest of ntasker is unaffected.

Run state is purely in-memory and per-connection -- there is no ``claude_runs``
table. A run lives exactly as long as its WebSocket; closing the socket ends it.

Permission model
----------------
Permissions are fixed for the run at start time, because that is the only model
this CLI/SDK combination supports reliably in headless mode:

* ``permission_mode`` -- ``plan`` (read-only), ``default`` (only pre-allowed
  tools run; others are denied), ``acceptEdits`` (file edits auto-approved), or
  ``bypassPermissions`` (everything runs, no gate).
* ``allowed_tools`` / ``disallowed_tools`` -- explicit whitelist / blacklist,
  passed straight through to the CLI's ``--allowedTools`` / ``--disallowedTools``.

Interactive per-call approval is intentionally absent: the SDK's ``can_use_tool``
control callback never fires against the headless CLI (there is no human at the
subprocess to prompt), and mid-run ``set_permission_mode`` does not take effect.
Both were verified not to work, so the gate is established up front instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

try:  # optional dependency -- feature degrades to "unavailable" without it
    import claude_agent_sdk as _sdk

    _SDK_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the SDK
    _sdk = None  # type: ignore[assignment]
    _SDK_IMPORT_ERROR = str(exc)


# Permission modes accepted from the UI, mapped straight onto the SDK's
# ``permission_mode``. ``plan`` is a read-only dry run ("see what it would do").
PERMISSION_MODES = ("plan", "default", "acceptEdits", "bypassPermissions")
DEFAULT_PERMISSION_MODE = "default"


def runner_available() -> tuple[bool, str | None]:
    """Return ``(available, reason_if_not)``.

    The feature needs both the Python SDK *and* the ``claude`` CLI on PATH (the
    SDK shells out to it). Either missing -> unavailable with a human-readable
    reason the UI surfaces instead of offering a dead button.
    """
    if _sdk is None:
        return False, f"claude-agent-sdk is not installed ({_SDK_IMPORT_ERROR})"
    if shutil.which("claude") is None:
        return False, "the `claude` CLI was not found on PATH"
    return True, None


def default_cwd_for_project(project: str | None) -> str | None:
    """Best-effort working directory for a task's ``project`` name.

    Inverse of :func:`ntasker.projects._path_to_name`: an absolute project name
    is used verbatim; a relative one is resolved under ``projects_base`` (or the
    home directory when unset). The result is only a *suggestion* -- the UI shows
    it pre-filled and editable, so a wrong guess costs one edit, not a failed run.
    """
    if not project:
        return None
    p = Path(project).expanduser()
    if p.is_absolute():
        return str(p)
    # Local import: keep this module importable before the DB exists.
    from ntasker.settings import get_setting  # noqa: PLC0415

    raw = get_setting("projects_base", env_var="NTASKER_PROJECTS_BASE")
    base = Path(os.path.expanduser(raw)) if raw else Path.home()
    return str(base / project)


def default_prompt_for_task(task: dict) -> str:
    """Compose a starter prompt that triggers the ntasker skill.

    The ``#<id>`` token makes Claude Code auto-load the packaged ntasker skill
    (SKILL.md), which knows the tracker workflow. Kept short and editable -- the
    user tweaks it in the start dialog before launching.
    """
    lines = [f"Please work on ntasker task #{task['id']}: {task['title']}"]
    desc = (task.get("description") or "").strip()
    if desc:
        lines += ["", desc]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Message serialisation -- SDK dataclasses -> plain JSON the UI can render
# ---------------------------------------------------------------------------


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """Coerce arbitrary SDK payloads into JSON-serialisable primitives."""
    if _depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, _depth + 1) for v in value]
    return str(value)


def _serialize_block(block: Any) -> dict:
    """One content block -> ``{type, ...}`` for the run view."""
    if isinstance(block, _sdk.TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, _sdk.ThinkingBlock):
        return {"type": "thinking", "text": block.thinking}
    if isinstance(block, _sdk.ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name,
                "input": _json_safe(block.input)}
    if isinstance(block, _sdk.ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id,
                "content": _json_safe(block.content), "is_error": bool(block.is_error)}
    return {"type": "other", "repr": str(block)[:1000]}


def _serialize_message(message: Any) -> dict:
    """One SDK message -> a compact ``{kind, ...}`` event for the browser."""
    if isinstance(message, _sdk.AssistantMessage):
        return {"kind": "assistant",
                "blocks": [_serialize_block(b) for b in message.content]}
    if isinstance(message, _sdk.UserMessage):
        content = message.content
        blocks = content if isinstance(content, list) else [_sdk.TextBlock(text=str(content))]
        return {"kind": "user", "blocks": [_serialize_block(b) for b in blocks]}
    if isinstance(message, _sdk.SystemMessage):
        return {"kind": "system", "subtype": message.subtype, "data": _json_safe(message.data)}
    if isinstance(message, _sdk.ResultMessage):
        return {"kind": "result", "is_error": bool(message.is_error),
                "result": message.result, "num_turns": message.num_turns,
                "duration_ms": message.duration_ms,
                "total_cost_usd": message.total_cost_usd}
    return {"kind": "other", "type": type(message).__name__}


def _str_list(value: Any) -> list[str]:
    """Filter an arbitrary payload down to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()]


# ---------------------------------------------------------------------------
# Session driver
# ---------------------------------------------------------------------------


async def run_session(websocket: WebSocket, task: dict) -> None:
    """Drive one Claude run for ``task`` over an accepted WebSocket.

    Protocol (JSON messages):

    * client -> ``{"type":"start", "prompt", "cwd", "permission_mode",
      "allowed_tools":[...], "disallowed_tools":[...]}``
    * client -> ``{"type":"stop"}``
    * server -> ``{"type":"started", "cwd", "permission_mode"}`` / ``event`` /
      ``done`` / ``error``

    Two coroutines run concurrently: the query consumer (streams events) and the
    command reader (waits for a stop or a disconnect). Whichever finishes first
    cancels the other, so a stop/disconnect ends the run promptly.
    """
    try:
        start = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError):
        return
    if start.get("type") != "start":
        await websocket.send_json({"type": "error", "error": "expected a start message"})
        return

    prompt = (start.get("prompt") or "").strip() or default_prompt_for_task(task)
    cwd = (start.get("cwd") or "").strip() or None
    mode = start.get("permission_mode")
    if mode not in PERMISSION_MODES:
        mode = DEFAULT_PERMISSION_MODE

    options = _sdk.ClaudeAgentOptions(
        cwd=cwd,
        permission_mode=mode,
        allowed_tools=_str_list(start.get("allowed_tools")),
        disallowed_tools=_str_list(start.get("disallowed_tools")),
        # Isolation: ignore the user's ambient ~/.claude permission settings so
        # the UI's mode + allow/deny lists are the *only* gate -- otherwise a
        # global allow rule would silently override (or block) the UI choice.
        # Trade-off: no project CLAUDE.md auto-load. Skills are re-enabled below.
        setting_sources=[],
        skills="all",
    )

    await websocket.send_json({"type": "started", "cwd": cwd, "permission_mode": mode})

    async def consume() -> dict:
        """Iterate the query stream; return a terminal ``done`` payload."""
        try:
            async for message in _sdk.query(prompt=prompt, options=options):
                await websocket.send_json({"type": "event", "data": _serialize_message(message)})
            return {"type": "done", "status": "completed"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface SDK/CLI failures to the UI
            return {"type": "done", "status": "error", "error": str(exc)}

    async def read_commands() -> None:
        """Wait for a stop command (or a disconnect, which raises)."""
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") == "stop":
                return

    consumer_task = asyncio.create_task(consume())
    reader_task = asyncio.create_task(read_commands())
    done_payload: dict = {"type": "done", "status": "stopped"}
    try:
        finished, _pending = await asyncio.wait(
            {consumer_task, reader_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if consumer_task in finished:
            done_payload = consumer_task.result()
    except WebSocketDisconnect:
        done_payload = {"type": "done", "status": "disconnected"}
    finally:
        for t in (consumer_task, reader_task):
            t.cancel()
        await asyncio.gather(consumer_task, reader_task, return_exceptions=True)

    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await websocket.send_json(done_payload)
