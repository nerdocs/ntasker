"""WebSocket endpoint streaming microphone audio through Vosk.

Protocol on ``/api/voice/ws``:

* client -> server: binary frames of 16 kHz mono int16 PCM; the text
  frame ``"final"`` asks for the result of whatever audio is still
  pending (sent before the client closes, so the last words are kept).
* server -> client: JSON ``{"type": "status", "text"}`` while the model
  loads, ``{"type": "partial", "text"}`` after each chunk (a revisable
  hypothesis), ``{"type": "final", "text"}`` at each pause and on
  ``"final"``, ``{"type": "error", "text"}`` followed by a close when
  Vosk is missing or the model cannot be loaded. Spoken punctuation is
  already applied to both texts (:mod:`~ntasker.plugins.voice.punct`).

Vosk's calls are blocking C++ -- model loading can take seconds (and
the first use of a language code downloads the model), so both run in a
worker thread. One model is kept per process; changing ``voice_model``
swaps it on the next connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ntasker.i18n import _
from ntasker.plugins.voice.punct import language_of, punctuate
from ntasker.settings import get_setting

SAMPLE_RATE = 16000

router = APIRouter()

_lock = threading.Lock()
_cached: tuple[str, Any] | None = None


def model_spec() -> str:
    """The ``voice_model`` setting, defaulting to the UI language's code."""
    spec = get_setting("voice_model", env_var="NTASKER_VOICE_MODEL")
    if spec:
        return spec
    lang = get_setting("language", env_var="NTASKER_LANGUAGE")
    return lang if lang in ("en", "de") else "en"


def load_model(spec: str) -> Any:
    """Return the Vosk ``Model`` for ``spec`` (cached; raises on failure)."""
    global _cached
    import vosk  # noqa: PLC0415 -- optional extra

    vosk.SetLogLevel(-1)
    with _lock:
        if _cached is None or _cached[0] != spec:
            model = vosk.Model(model_path=spec) if os.path.isdir(spec) else vosk.Model(lang=spec)
            _cached = (spec, model)
        return _cached[1]


@router.websocket("/api/voice/ws")
async def voice_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        import vosk  # noqa: PLC0415 -- optional extra
    except ImportError:
        await websocket.send_json(
            {"type": "error", "text": _("the vosk package is missing (pip install ntasker[voice])")}
        )
        await websocket.close()
        return
    await websocket.send_json({"type": "status", "text": _("Loading the speech model...")})
    spec = model_spec()
    try:
        model = await asyncio.to_thread(load_model, spec)
    except Exception as exc:  # noqa: BLE001 -- surface any load failure to the user
        await websocket.send_json({"type": "error", "text": str(exc)})
        await websocket.close()
        return
    rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
    lang = language_of(spec) or get_setting("language", env_var="NTASKER_LANGUAGE")

    async def send(kind: str, raw: str, key: str) -> None:
        text = punctuate(json.loads(raw).get(key, ""), lang)
        await websocket.send_json({"type": kind, "text": text})

    await websocket.send_json({"type": "status", "text": ""})
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("text") == "final":
                await send("final", rec.FinalResult(), "text")
                continue
            chunk = msg.get("bytes")
            if not chunk:
                continue
            if await asyncio.to_thread(rec.AcceptWaveform, chunk):
                await send("final", rec.Result(), "text")
            else:
                await send("partial", rec.PartialResult(), "partial")
    except WebSocketDisconnect:
        return
