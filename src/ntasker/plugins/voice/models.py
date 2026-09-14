"""Vosk model store: catalog, installed models, background download.

Models live under ``platformdirs.user_data_dir("nTasker") / "vosk-models"``,
one unpacked directory per model, named as in Vosk's catalog
(``vosk-model-small-de-0.15``). The catalog is Vosk's own
``model-list.json``; obsolete entries are dropped. A download streams the
zip next to the store, verifies the catalog's MD5, unpacks, and removes
the archive -- in a daemon thread, so the UI polls :func:`job_status`.
One download runs at a time.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

import httpx
import platformdirs

from ntasker.paths import APP_NAME

CATALOG_URL = "https://alphacephei.com/vosk/models/model-list.json"
_CATALOG_TTL = 60 * 60
_CATALOG_FIELDS = ("name", "lang", "lang_text", "size", "size_text", "type", "url", "md5")

_catalog: list[dict] | None = None
_catalog_at = 0.0
_job: dict | None = None
_lock = threading.Lock()


def models_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME)) / "vosk-models"


def fetch_catalog() -> list[dict]:
    """Current (non-obsolete) catalog entries, cached for an hour. Raises offline."""
    global _catalog, _catalog_at
    with _lock:
        if _catalog is not None and time.time() - _catalog_at < _CATALOG_TTL:
            return list(_catalog)
    resp = httpx.get(CATALOG_URL, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    entries = [
        {k: m.get(k) for k in _CATALOG_FIELDS}
        for m in resp.json()
        if str(m.get("obsolete", "false")).lower() != "true"
    ]
    entries.sort(key=lambda m: (m["lang_text"] or "", m["size"] or 0))
    with _lock:
        _catalog, _catalog_at = entries, time.time()
    return list(entries)


def is_model_dir(path: Path) -> bool:
    """A Vosk model is an unpacked directory with an ``am/`` folder."""
    return (path / "am").is_dir()


def installed() -> list[dict]:
    """``[{name, path}]`` of the models in the store, sorted by name."""
    base = models_dir()
    if not base.is_dir():
        return []
    return [
        {"name": d.name, "path": str(d)}
        for d in sorted(base.iterdir())
        if d.is_dir() and is_model_dir(d)
    ]


def resolve(spec: str) -> Path | None:
    """Model directory for a ``voice_model`` value: a store name or a path."""
    if not spec:
        return None
    for candidate in (models_dir() / spec, Path(os.path.expanduser(spec))):
        if is_model_dir(candidate):
            return candidate
    return None


def job_status() -> dict | None:
    """The running or last download: ``{name, state, received, total, error}``."""
    with _lock:
        return dict(_job) if _job else None


def start_download(entry: dict) -> dict:
    """Start downloading a catalog ``entry`` in the background; raises if one runs."""
    global _job
    with _lock:
        if _job and _job["state"] in ("downloading", "unpacking"):
            raise RuntimeError("a download is already running")
        _job = {
            "name": entry["name"],
            "state": "downloading",
            "received": 0,
            "total": int(entry.get("size") or 0),
            "error": None,
        }
        job = dict(_job)
    threading.Thread(target=_run, args=(entry,), daemon=True).start()
    return job


def _set(**fields: object) -> None:
    with _lock:
        if _job:
            _job.update(fields)


def _run(entry: dict) -> None:
    name = entry["name"]
    base = models_dir()
    base.mkdir(parents=True, exist_ok=True)
    part = base / f"{name}.zip.part"
    target = base / name
    try:
        digest = hashlib.md5(usedforsecurity=False)
        received = 0
        with (
            httpx.stream("GET", entry["url"], timeout=30.0, follow_redirects=True) as resp,
            part.open("wb") as fh,
        ):
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or entry.get("size") or 0)
            _set(total=total)
            for chunk in resp.iter_bytes(1 << 16):
                fh.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                _set(received=received)
        if entry.get("md5") and digest.hexdigest() != entry["md5"]:
            raise RuntimeError("checksum mismatch")
        _set(state="unpacking")
        shutil.rmtree(target, ignore_errors=True)
        root = base.resolve()
        with zipfile.ZipFile(part) as archive:
            for info in archive.infolist():
                if not (root / info.filename).resolve().is_relative_to(root):
                    raise RuntimeError("archive contains an unsafe path")
            archive.extractall(base)
        if not is_model_dir(target):
            raise RuntimeError(f"archive did not contain a model directory {name!r}")
        _set(state="done")
    except Exception as exc:  # noqa: BLE001 -- any failure is reported to the UI
        shutil.rmtree(target, ignore_errors=True)
        _set(state="error", error=str(exc))
    finally:
        part.unlink(missing_ok=True)
