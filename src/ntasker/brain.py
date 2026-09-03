"""JCBrain (OpenBrain) -- remote notes attached to tasks as context.

JCBrain is not a directory: it is a Supabase edge function speaking the
Model Context Protocol over HTTP, reachable through the ``open-brain`` MCP
server declared in ``~/.claude.json``. The other context kinds point at
files inside the workspace roots; a JCBrain attachment points at a
*thought* by UUID and is stored as ``brain://<uuid>`` in the same
``task_context.path`` column, so the unique-per-task rule and the chip UI
carry over unchanged.

No second secret is introduced. The server's URL and its ``x-brain-key``
header are read from the MCP entry Claude Code already uses -- the one
place the user maintains them. Which entry is chosen is the
``brain_server`` setting (default ``open-brain``).

The edge function answers ``tools/call`` statelessly (no ``initialize``
handshake needed) and replies either as plain JSON or as a single SSE
``message`` event; both are handled here. Only the two read-only tools are
used: ``search`` (semantic, returns ``{id, title, url}`` per hit) and
``fetch`` (full text + metadata for one id).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from ntasker.i18n import _
from ntasker.workspace import CLAUDE_CONFIG_PATH, _expand

#: Prefix that marks a ``task_context.path`` as a JCBrain thought id.
BRAIN_SCHEME = "brain://"

#: MCP server entry used when the ``brain_server`` setting is unset.
DEFAULT_SERVER = "open-brain"

#: Seconds before a search/fetch gives up -- the edge function embeds the
#: query via OpenRouter first, so a few seconds are normal.
TIMEOUT = 12.0

#: Upper bound on the note text inlined into an agent briefing. A thought
#: is usually a paragraph; a pasted report would otherwise swamp the prompt.
BRIEFING_MAX_CHARS = 6000

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class BrainError(Exception):
    """Anything that keeps a JCBrain call from returning a result.

    ``status`` is the HTTP status the API layer should map it to: 503 when
    the server is not configured or unreachable, 404 when a thought is
    gone, 502 for a malformed or error reply.
    """

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def is_brain_path(path: str | None) -> bool:
    return bool(path) and str(path).startswith(BRAIN_SCHEME)


def thought_path(thought_id: str) -> str:
    """``brain://<uuid>`` for a validated id."""
    return f"{BRAIN_SCHEME}{thought_id.strip().lower()}"


def thought_id(path: str) -> str:
    """The bare UUID from a ``brain://`` path (empty if not one)."""
    if not is_brain_path(path):
        return ""
    return path[len(BRAIN_SCHEME) :].strip()


def is_valid_id(value: str | None) -> bool:
    return bool(value) and bool(_UUID_RE.match(str(value).strip()))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def server_name() -> str:
    """The configured MCP server entry name (setting > default)."""
    from ntasker.settings import get_setting  # noqa: PLC0415 -- lazy: avoid cycle

    return (get_setting("brain_server") or "").strip() or DEFAULT_SERVER


def server_spec(name: str | None = None) -> dict[str, Any] | None:
    """The raw ``mcpServers[name]`` entry from ``~/.claude.json``, if usable.

    Usable means an HTTP transport with a URL. A stdio server cannot be
    called from here -- ntasker speaks plain HTTP, it does not spawn MCP
    processes.
    """
    name = name or server_name()
    config = _expand(CLAUDE_CONFIG_PATH)
    if not config or not config.is_file():
        return None
    try:
        with config.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    spec = servers.get(name)
    if not isinstance(spec, dict) or not spec.get("url"):
        return None
    return spec


def status() -> dict[str, Any]:
    """What the UI needs to decide whether to offer the JCBrain tab.

    The key itself is never returned -- only whether one is present.
    """
    name = server_name()
    spec = server_spec(name)
    headers = spec.get("headers") if spec else None
    return {
        "configured": spec is not None,
        "server": name,
        "url": str(spec.get("url")) if spec else "",
        "has_auth": isinstance(headers, dict) and bool(headers),
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _parse_reply(body: str) -> dict[str, Any]:
    """Decode a JSON-RPC reply that arrived as JSON or as an SSE stream."""
    text = body.strip()
    if not text:
        raise BrainError(_("JCBrain returned an empty reply."))
    if text.startswith("{"):
        return json.loads(text)
    # SSE: take the last ``data:`` line carrying a JSON object.
    payload = None
    for line in text.splitlines():
        if line.startswith("data:"):
            candidate = line[5:].strip()
            if candidate.startswith("{"):
                payload = candidate
    if payload is None:
        raise BrainError(_("JCBrain returned an unexpected reply."))
    return json.loads(payload)


def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = TIMEOUT,
    opener=None,
) -> str:
    """Run one MCP tool and return its text content.

    ``opener`` exists for tests: a callable ``(request, timeout) -> response``
    with ``.read()``; defaults to :func:`urllib.request.urlopen`.
    """
    spec = server_spec()
    if spec is None:
        raise BrainError(
            _("JCBrain is not configured -- no HTTP MCP server named {name} in {path}.").format(
                name=server_name(), path=CLAUDE_CONFIG_PATH
            ),
            status=503,
        )
    headers = {
        "Content-Type": "application/json",
        # Streamable HTTP transports reject requests that do not accept
        # both; the function then picks one at its discretion.
        "Accept": "application/json, text/event-stream",
    }
    extra = spec.get("headers")
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode("utf-8")
    req = urllib.request.Request(str(spec["url"]), data=body, headers=headers, method="POST")
    open_fn = opener or (lambda r, t: urllib.request.urlopen(r, timeout=t))
    try:
        with open_fn(req, timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise BrainError(
            _("JCBrain answered HTTP {code}.").format(code=exc.code), status=502
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BrainError(
            _("JCBrain is unreachable: {error}").format(error=exc), status=503
        ) from exc

    try:
        reply = _parse_reply(raw)
    except json.JSONDecodeError as exc:
        raise BrainError(_("JCBrain returned an unexpected reply.")) from exc

    if reply.get("error"):
        err = reply["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise BrainError(str(message or _("JCBrain returned an error.")))
    result = reply.get("result") or {}
    chunks = [
        str(c.get("text", ""))
        for c in result.get("content") or []
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    text = "\n".join(chunks)
    if result.get("isError"):
        raise BrainError(text or _("JCBrain returned an error."))
    return text


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def search(query: str, **kw: Any) -> list[dict[str, Any]]:
    """Semantic search; ``[{id, title, url}, ...]`` best match first.

    The edge function's ``search`` tool fixes limit (10) and threshold
    (0.5) -- good enough for "find the note I mean" in a picker.
    """
    query = (query or "").strip()
    if not query:
        return []
    text = call_tool("search", {"query": query}, **kw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrainError(_("JCBrain returned an unexpected reply.")) from exc
    out: list[dict[str, Any]] = []
    for hit in data.get("results") or []:
        if not isinstance(hit, dict) or not is_valid_id(hit.get("id")):
            continue
        out.append(
            {
                "id": str(hit["id"]).lower(),
                "title": str(hit.get("title") or "").strip(),
                "url": str(hit.get("url") or ""),
                "path": thought_path(str(hit["id"])),
            }
        )
    return out


def fetch(thought: str, **kw: Any) -> dict[str, Any]:
    """One thought in full: ``{id, title, text, url, metadata, path}``."""
    thought = (thought or "").strip()
    if not is_valid_id(thought):
        raise BrainError(_("Not a JCBrain thought id: {value}").format(value=thought), 400)
    text = call_tool("fetch", {"id": thought}, **kw)
    if text.startswith("Fetch error") or text.startswith("Error"):
        # The tool reports a missing row as an error string rather than
        # via isError in every path; a row-not-found is the only plausible
        # cause for a well-formed id.
        raise BrainError(_("JCBrain has no thought with this id."), status=404)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrainError(_("JCBrain returned an unexpected reply.")) from exc
    if not isinstance(doc, dict) or not doc.get("id"):
        raise BrainError(_("JCBrain has no thought with this id."), status=404)
    return {
        "id": str(doc["id"]).lower(),
        "title": str(doc.get("title") or "").strip(),
        "text": str(doc.get("text") or ""),
        "url": str(doc.get("url") or ""),
        "metadata": doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
        "path": thought_path(str(doc["id"])),
    }


def briefing_text(path: str, **kw: Any) -> str:
    """The note body for an agent prompt, capped; empty string on failure.

    Used where the briefing is assembled server-side (compact seed). The
    ``/task`` loader does the same through ``GET /api/brain/thoughts/<id>``.
    """
    try:
        doc = fetch(thought_id(path), **kw)
    except BrainError:
        return ""
    text = doc["text"].strip()
    if len(text) > BRIEFING_MAX_CHARS:
        text = text[:BRIEFING_MAX_CHARS].rstrip() + "\n[... truncated]"
    return text
