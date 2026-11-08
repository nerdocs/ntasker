"""``ntasker worker``: run a team server's tasks on this machine.

The worker connects *out* to the server's ``/ws/worker`` (protocol: see
:mod:`ntasker.workers`) with the user's token and starts the agent sessions the
server's queue hands it -- with this machine's agent CLIs, subscription and
checkouts, through the same PTY code a local server uses
(:func:`ntasker.claude_runner.spawn_local`). Terminal output streams back; the
sessions' hooks and ``ntasker`` calls reach the server directly
(``NTASKER_URL`` / ``NTASKER_TOKEN`` in their environment).

Configuration lives in this machine's own settings: ``server_url``,
``server_token``, ``worker_name`` (default: the host name). A dropped link is
retried with backoff; running sessions are never stopped because of it, the
next ``hello`` hands them back to the server.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import shlex
import socket
import sys
import time

from ntasker import __version__, claude_runner
from ntasker.agents import get_spec, resolve_home
from ntasker.claude_runner import TermSession
from ntasker.rundiff import parse_baselines, run_diff
from ntasker.transcript import find_transcript

BACKOFF_MAX = 30.0

#: How often the transcript mirror is topped up, and how often the Diff view
#: is recomputed (git calls) while a run is alive.
SYNC_SECONDS = 2.0
DIFF_SECONDS = 10.0

#: Largest transcript chunk per message.
CHUNK = 512 * 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def ws_url(server_url: str) -> str:
    """``https://host`` -> ``wss://host/ws/worker`` (and http -> ws)."""
    base = server_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/ws/worker"


class Worker:
    """One worker process: its sessions outlive any single connection."""

    def __init__(self, server_url: str, token: str, name: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.name = name
        self._out: asyncio.Queue | None = None
        self._forwarders: dict[int, asyncio.Task] = {}
        # This machine's sessions by task id -- the server keeps its own
        # (relaying) registry, so the worker never shares the module one.
        self.sessions: dict[int, TermSession] = {}
        # Per run: what the server's mirror needs -- agent, session id,
        # baselines, transcript bytes sent, last diff sent (see _sync).
        self.runs: dict[int, dict] = {}

    # -- outgoing --------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        if self._out is not None:
            self._out.put_nowait(msg)

    def _forward(self, sess: TermSession) -> None:
        """Stream a session's output and exit to the server while connected."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        sess.subscribers.add(queue)

        async def run() -> None:
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "output":
                        self._send({"type": "output", "task_id": sess.task_id, "data": _b64(payload)})
                    else:
                        self._sync(sess.task_id, final=True)
                        self._send({"type": "exit", "task_id": sess.task_id, "code": payload})
                        return
            finally:
                sess.subscribers.discard(queue)
                if not sess.alive and self.sessions.get(sess.task_id) is sess:
                    del self.sessions[sess.task_id]

        self._forwarders[sess.task_id] = asyncio.create_task(run())

    def _hello(self) -> dict:
        return {
            "type": "hello",
            "name": self.name,
            "hostname": socket.gethostname(),
            "version": __version__,
            "sessions": [
                {"task_id": s.task_id, "alive": s.alive, "cwd": s.cwd, "buffer": _b64(s.buffer)}
                for s in self.sessions.values()
                if s.alive
            ],
        }

    # -- incoming --------------------------------------------------------------

    def _env(self) -> dict:
        return {"NTASKER_URL": self.server_url, "NTASKER_TOKEN": self.token}

    async def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "start":
            spec = msg.get("spec") or {}
            task_id = int(spec.get("task_id", 0))
            if spec.get("worktree_id"):
                self._send({"type": "refused", "task_id": task_id, "reason": "worktree"})
                return
            try:
                sess = claude_runner.spawn_local(spec, env=self._env(), register=False)
            except Exception as exc:  # noqa: BLE001 -- any spawn failure goes back as a refusal
                self._send({"type": "refused", "task_id": task_id, "reason": str(exc)})
                return
            self.sessions[task_id] = sess
            self.runs[task_id] = {
                "agent": spec.get("agent"),
                "session_id": spec.get("session_id") or spec.get("resume_id"),
                "baselines": sess.baselines or spec.get("baselines"),
                "offset": 0, "diff": None, "diff_at": 0.0,
            }
            self._forward(sess)
            self._send({"type": "started", "task_id": task_id, "cwd": sess.cwd, "baselines": sess.baselines})
            return
        sess = self.sessions.get(int(msg.get("task_id") or 0))
        if sess is None or not sess.alive:
            return
        if kind == "input":
            await claude_runner._write_pty(sess, _decode(msg.get("data")))
        elif kind == "resize":
            claude_runner._resize(sess, msg.get("rows", 24), msg.get("cols", 80))
        elif kind == "stop":
            claude_runner._stop(sess)
        elif kind == "upload":
            blob = _decode(msg.get("data"))
            if blob and len(blob) <= claude_runner.MAX_UPLOAD_BYTES:
                path = claude_runner._save_upload(str(msg.get("name", "")), blob)
                await claude_runner._write_pty(sess, f"\x1b[200~{shlex.quote(path)} \x1b[201~".encode())

    # -- mirror ----------------------------------------------------------------

    def _sync(self, task_id: int, final: bool = False) -> None:
        """Top up the server's transcript mirror and, now and then, the Diff view."""
        run = self.runs.get(task_id)
        if run is None or self._out is None:
            return
        with contextlib.suppress(Exception):  # a read hiccup waits for the next round
            agent = get_spec(run["agent"])
            path = find_transcript(resolve_home(agent), run["session_id"] or "") if agent.transcript else None
            if path is not None:
                size = path.stat().st_size
                if size < run["offset"]:
                    run["offset"] = 0  # rewritten -- send it again
                if size > run["offset"]:
                    with path.open("rb") as fh:
                        fh.seek(run["offset"])
                        while chunk := fh.read(CHUNK):
                            self._send({"type": "transcript", "task_id": task_id,
                                        "session_id": run["session_id"], "offset": run["offset"],
                                        "data": _b64(chunk)})
                            run["offset"] += len(chunk)
        now = time.monotonic()
        if run["baselines"] and (final or now - run["diff_at"] >= DIFF_SECONDS):
            run["diff_at"] = now
            with contextlib.suppress(Exception):
                diff = run_diff(parse_baselines(run["baselines"]))
                if diff != run["diff"]:
                    run["diff"] = diff
                    self._send({"type": "diff", "task_id": task_id, "diff": diff})
        if final:
            self.runs.pop(task_id, None)

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(SYNC_SECONDS)
            for task_id, sess in list(self.sessions.items()):
                if sess.alive:
                    self._sync(task_id)

    # -- connection ------------------------------------------------------------

    async def _session(self, ws) -> None:
        self._out = asyncio.Queue()
        # A fresh link: the server may have missed anything -- send in full.
        for run in self.runs.values():
            run["offset"], run["diff"], run["diff_at"] = 0, None, 0.0
        await ws.send(json.dumps(self._hello()))
        for sess in list(self.sessions.values()):
            if sess.alive:
                self._forward(sess)

        async def pump() -> None:
            while True:
                await ws.send(json.dumps(await self._out.get()))

        writer = asyncio.create_task(pump())
        syncer = asyncio.create_task(self._sync_loop())
        try:
            async for raw in ws:
                with contextlib.suppress(ValueError):
                    await self._handle(json.loads(raw))
        finally:
            writer.cancel()
            syncer.cancel()
            for task in self._forwarders.values():
                task.cancel()
            self._forwarders.clear()
            self._out = None

    async def run(self, once: bool = False) -> None:
        """Connect, serve, reconnect with backoff. ``once``: return after the first connection."""
        from websockets.asyncio.client import connect  # noqa: PLC0415 -- lazy: CLI stays light
        from websockets.exceptions import WebSocketException  # noqa: PLC0415

        backoff = 1.0
        url = ws_url(self.server_url)
        while True:
            try:
                async with connect(url, additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
                    print(f"nTasker worker {self.name}: connected to {self.server_url}", file=sys.stderr)
                    backoff = 1.0
                    await self._session(ws)
            except (OSError, WebSocketException) as exc:
                print(f"nTasker worker {self.name}: {exc}", file=sys.stderr)
            if once:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


def _decode(data) -> bytes:
    try:
        return base64.b64decode(str(data or ""), validate=True)
    except (ValueError, binascii.Error):
        return b""
