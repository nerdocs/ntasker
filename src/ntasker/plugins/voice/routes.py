"""Voice plugin HTTP + WebSocket routes.

Model management (JSON):

* ``GET /api/voice/models`` -- ``{vosk, models_dir, current, installed,
  catalog, catalog_error, job}``: whether the ``vosk`` package imports,
  the store directory, the effective ``voice_model``, the installed
  models, Vosk's catalog (empty + ``catalog_error`` while offline) and
  the running/last download.
* ``POST /api/voice/models/{name}`` -- start downloading a catalog entry
  in the background (409 while another download runs).
* ``GET /api/voice/models/job`` -- poll the download.

WebSocket ``/api/voice/ws``:

* client -> server: binary frames of 16 kHz mono int16 PCM; the text
  frame ``"final"`` asks for the result of whatever audio is still
  pending (sent before the client closes, so the last words are kept).
* server -> client: JSON ``{"type": "status", "text"}`` while the model
  loads, ``{"type": "partial", "text"}`` after each chunk (a revisable
  hypothesis), ``{"type": "final", "text"}`` at each pause and on
  ``"final"``, ``{"type": "error", "text", "code"}`` followed by a close
  when Vosk is missing (``no_vosk``), no model is installed or selected
  (``no_model``) or the model cannot be loaded (``load_failed``). Spoken
  punctuation is already applied to both texts
  (:mod:`~ntasker.plugins.voice.punct`).

Vosk's calls are blocking C++ -- model loading can take seconds, so both
run in a worker thread. One model is kept per process; changing
``voice_model`` swaps it on the next connection.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ntasker.i18n import _
from ntasker.plugins.voice import models
from ntasker.plugins.voice.punct import language_of, punctuate
from ntasker.settings import get_setting

SAMPLE_RATE = 16000

router = APIRouter()

_lock = threading.Lock()
_cached: tuple[str, Any] | None = None


def ui_language() -> str | None:
    return get_setting("language", env_var="NTASKER_LANGUAGE")


def model_spec() -> str | None:
    """The ``voice_model`` setting, else an installed model -- preferring the
    UI language's -- else ``None``."""
    spec = get_setting("voice_model", env_var="NTASKER_VOICE_MODEL")
    if spec:
        return spec
    have = models.installed()
    if not have:
        return None
    lang = ui_language()
    for m in have:
        if language_of(m["name"]) == lang:
            return m["name"]
    return have[0]["name"]


def load_model(path: Path) -> Any:
    """Return the Vosk ``Model`` at ``path`` (cached; raises on failure)."""
    global _cached
    import vosk  # noqa: PLC0415 -- optional extra

    vosk.SetLogLevel(-1)
    with _lock:
        if _cached is None or _cached[0] != str(path):
            _cached = (str(path), vosk.Model(model_path=str(path)))
        return _cached[1]


@router.get("/api/voice/models")
def list_models() -> dict:
    catalog: list[dict] = []
    catalog_error = None
    try:
        catalog = models.fetch_catalog()
    except Exception as exc:  # noqa: BLE001 -- offline: the card still shows installed models
        catalog_error = str(exc)
    return {
        "vosk": importlib.util.find_spec("vosk") is not None,
        "models_dir": str(models.models_dir()),
        "current": model_spec(),
        "installed": models.installed(),
        "catalog": catalog,
        "catalog_error": catalog_error,
        "job": models.job_status(),
    }


@router.get("/api/voice/models/job")
def download_job() -> dict:
    return {"job": models.job_status()}


@router.post("/api/voice/models/{name}", status_code=202)
def download_model(name: str) -> dict:
    try:
        entry = next(m for m in models.fetch_catalog() if m["name"] == name)
    except StopIteration:
        raise HTTPException(status_code=404, detail=_("Unknown model {name!r}.").format(name=name)) from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        return {"job": models.start_download(entry)}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _fail(websocket: WebSocket, code: str, text: str) -> None:
    await websocket.send_json({"type": "error", "code": code, "text": text})
    await websocket.close()


@router.websocket("/api/voice/ws")
async def voice_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        import vosk  # noqa: PLC0415 -- optional extra
    except ImportError:
        await _fail(websocket, "no_vosk", _("the vosk package is missing -- run `ntasker enable voice`"))
        return
    spec = model_spec()
    path = models.resolve(spec) if spec else None
    if path is None:
        await _fail(websocket, "no_model", _("no speech model installed -- pick one in the settings"))
        return
    await websocket.send_json({"type": "status", "text": _("Loading the speech model...")})
    try:
        model = await asyncio.to_thread(load_model, path)
    except Exception as exc:  # noqa: BLE001 -- surface any load failure to the user
        await _fail(websocket, "load_failed", str(exc))
        return
    rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
    lang = language_of(path.name) or ui_language()

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
