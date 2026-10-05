"""Voice plugin HTTP + WebSocket routes.

Model management (JSON):

* ``GET /api/voice/models`` -- ``{engines, current, installed, catalog,
  catalog_error, job, dirs}``: which engine packages import (``vosk``,
  ``whisper``), the effective ``voice_model``, the installed models, the
  catalog (Whisper's fixed entries, then Vosk's -- whose part is empty +
  ``catalog_error`` while offline), the running/last download and the
  store directories.
* ``POST /api/voice/models/{name}`` -- start downloading a catalog entry
  in the background (409 while another download runs).
* ``GET /api/voice/models/job`` -- poll the download.
* ``DELETE /api/voice/models/{name}`` -- move an installed model to the
  trash; ``{was_current}`` says whether it was the ``voice_model``, which
  is then unset.
* ``POST /api/voice/models/{name}/restore`` -- undo the delete (404 once
  the trash is purged); body ``{"use": true}`` selects it again.

WebSocket ``/api/voice/ws``:

* client -> server: binary frames of 16 kHz mono int16 PCM; the text
  frame ``"final"`` asks for the result of whatever audio is still
  pending (sent before the client closes, so the last words are kept).
* server -> client: JSON ``{"type": "status", "text"}`` while the model
  loads, ``{"type": "partial", "text"}`` with a revisable hypothesis,
  ``{"type": "final", "text"}`` at each pause and on ``"final"``,
  ``{"type": "error", "text", "code"}`` followed by a close when no model
  is installed or selected (``no_model``), the model's engine package is
  missing (``no_engine``) or the model cannot be loaded
  (``load_failed``).

Both engines sit behind the same two calls (``accept`` a chunk, ``flush``
the rest). Vosk streams: it answers every chunk and spots pauses itself;
its lowercase words get spoken punctuation
(:mod:`~ntasker.plugins.voice.punct`). Whisper transcribes whole
utterances: the audio is buffered while the level says someone speaks, a
pause transcribes it as ``final``, and while speaking the buffer is
re-transcribed now and then for a ``partial`` -- the slower the model, the
rarer. Whisper punctuates by itself. Engine calls are blocking (C++,
model loading can take seconds), so they run in a worker thread. One
model is kept per process; changing ``voice_model`` swaps it on the next
connection.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

from ntasker.i18n import _
from ntasker.plugins.voice import models
from ntasker.plugins.voice.punct import language_of, punctuate
from ntasker.settings import delete_setting, get_setting, set_setting

SAMPLE_RATE = 16000
#: Engine -> the Python module it needs.
PACKAGES = {"vosk": "vosk", "whisper": "faster_whisper"}

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


def load_model(path: Path, engine: str) -> Any:
    """Return the ``engine`` model at ``path`` (cached; raises on failure)."""
    global _cached
    with _lock:
        if _cached is None or _cached[0] != str(path):
            _cached = None  # free the old model before loading the next one
            if engine == "whisper":
                from faster_whisper import WhisperModel  # noqa: PLC0415 -- optional extra

                model = WhisperModel(str(path), device="cpu", compute_type="int8")
            else:
                import vosk  # noqa: PLC0415 -- optional extra

                vosk.SetLogLevel(-1)
                model = vosk.Model(model_path=str(path))
            _cached = (str(path), model)
        return _cached[1]


def _evict(path: str) -> None:
    """Drop the cached model if it is the one at ``path``."""
    global _cached
    with _lock:
        if _cached and _cached[0] == path:
            _cached = None


class VoskRecognizer:
    """Streaming Vosk: a partial after every chunk, a final at each pause."""

    def __init__(self, model: Any, lang: str | None) -> None:
        import vosk  # noqa: PLC0415 -- optional extra

        self.rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        self.lang = lang

    def _text(self, raw: str, key: str) -> str:
        return punctuate(json.loads(raw).get(key, ""), self.lang)

    def accept(self, chunk: bytes) -> tuple[str, str] | None:
        if self.rec.AcceptWaveform(chunk):
            return "final", self._text(self.rec.Result(), "text")
        return "partial", self._text(self.rec.PartialResult(), "partial")

    def flush(self) -> str:
        return self._text(self.rec.FinalResult(), "text")


class WhisperRecognizer:
    """Utterance-wise Whisper on top of a simple level gate (see module doc)."""

    LEVEL = 300  # int16 RMS above which a chunk counts as speech
    LEAD_IN = 0.3  # seconds of silence kept before the first word
    PAUSE = 0.7  # seconds of silence that end an utterance
    MAX = 25.0  # seconds -- Whisper's window is 30
    PARTIAL_EVERY = 1.0  # seconds of new speech before the next partial

    def __init__(self, model: Any, lang: str | None) -> None:
        self.model = model
        self.lang = lang
        self._reset()

    def _reset(self) -> None:
        self.buf = bytearray()
        self.voiced = False
        self.silent = 0.0
        self.next_partial = self.PARTIAL_EVERY

    def _seconds(self) -> float:
        return len(self.buf) / 2 / SAMPLE_RATE

    def _transcribe(self, beam_size: int) -> str:
        import numpy as np  # noqa: PLC0415 -- comes with faster-whisper

        audio = np.frombuffer(bytes(self.buf), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(
            audio,
            language=self.lang,
            beam_size=beam_size,
            vad_filter=True,  # no hallucinated text for breath and noise
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def accept(self, chunk: bytes) -> tuple[str, str] | None:
        import numpy as np  # noqa: PLC0415 -- comes with faster-whisper

        self.buf += chunk
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        if samples.size and float(np.sqrt(np.mean(samples * samples))) >= self.LEVEL:
            self.voiced = True
            self.silent = 0.0
        else:
            self.silent += samples.size / SAMPLE_RATE
        if not self.voiced:
            del self.buf[: max(0, len(self.buf) - int(self.LEAD_IN * SAMPLE_RATE) * 2)]
            return None
        if self.silent >= self.PAUSE or self._seconds() >= self.MAX:
            return "final", self.flush()
        if self._seconds() >= self.next_partial:
            started = time.monotonic()
            text = self._transcribe(beam_size=1)
            took = time.monotonic() - started
            self.next_partial = self._seconds() + max(self.PARTIAL_EVERY, 2 * took)
            return "partial", text
        return None

    def flush(self) -> str:
        text = self._transcribe(beam_size=5) if self.voiced else ""
        self._reset()
        return text


@router.get("/api/voice/models")
def list_models() -> dict:
    catalog = models.whisper_catalog()
    catalog_error = None
    try:
        catalog += models.fetch_catalog()
    except Exception as exc:  # noqa: BLE001 -- offline: the card still shows the rest
        catalog_error = str(exc)
    return {
        "engines": {e: importlib.util.find_spec(p) is not None for e, p in PACKAGES.items()},
        "dirs": {e: str(models.models_dir(e)) for e in models.ENGINES},
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
        entry = models.find_entry(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail=_("Unknown model {name!r}.").format(name=name))
    try:
        return {"job": models.start_download(entry)}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/api/voice/models/{name}")
def delete_model(name: str) -> dict:
    path = models.resolve(name)
    try:
        models.delete(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=_("Unknown model {name!r}.").format(name=name)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if path:
        _evict(str(path))
    was_current = get_setting("voice_model") == name
    if was_current:
        delete_setting("voice_model")
    return {"was_current": was_current}


@router.post("/api/voice/models/{name}/restore")
def restore_model(name: str, use: bool = Body(False, embed=True)) -> dict:
    if not models.restore(name):
        raise HTTPException(status_code=404, detail=_("{name} can no longer be restored.").format(name=name))
    if use:
        set_setting("voice_model", name)
    return {"restored": name}


async def _fail(websocket: WebSocket, code: str, text: str) -> None:
    await websocket.send_json({"type": "error", "code": code, "text": text})
    await websocket.close()


@router.websocket("/api/voice/ws")
async def voice_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    spec = model_spec()
    path = models.resolve(spec) if spec else None
    if path is None:
        await _fail(websocket, "no_model", _("no speech model installed -- pick one in the settings"))
        return
    engine = models.engine_of(path)
    if importlib.util.find_spec(PACKAGES[engine]) is None:
        await _fail(
            websocket,
            "no_engine",
            _("the {package} package is missing -- run `ntasker enable voice`").format(package=PACKAGES[engine]),
        )
        return
    await websocket.send_json({"type": "status", "text": _("Loading the speech model...")})
    try:
        model = await asyncio.to_thread(load_model, path, engine)
    except Exception as exc:  # noqa: BLE001 -- surface any load failure to the user
        await _fail(websocket, "load_failed", str(exc))
        return
    lang = language_of(path.name) or ui_language()
    if engine == "whisper":
        rec: VoskRecognizer | WhisperRecognizer = WhisperRecognizer(model, lang if lang != "auto" else None)
    else:
        rec = VoskRecognizer(model, lang)

    await websocket.send_json({"type": "status", "text": ""})
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("text") == "final":
                await websocket.send_json({"type": "final", "text": await asyncio.to_thread(rec.flush)})
                continue
            chunk = msg.get("bytes")
            if not chunk:
                continue
            result = await asyncio.to_thread(rec.accept, chunk)
            if result:
                await websocket.send_json({"type": result[0], "text": result[1]})
    except WebSocketDisconnect:
        return
