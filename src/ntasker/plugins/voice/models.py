"""Speech model store: catalog, installed models, background download, deletion.

Two engines, one store directory each under
``platformdirs.user_data_dir("nTasker")``:

* ``vosk-models/`` -- Vosk models, named as in Vosk's catalog
  (``vosk-model-small-de-0.15``). The catalog is Vosk's own
  ``model-list.json``; obsolete entries are dropped. A download streams the
  zip next to the store, verifies the catalog's MD5, unpacks, and removes
  the archive.
* ``whisper-models/`` -- faster-whisper (CTranslate2) conversions of
  OpenAI's Whisper, named ``whisper-<size>``. The catalog is the fixed
  :data:`WHISPER_CATALOG`; a download fetches the model files from
  Hugging Face into ``<name>.part/``, verifies each LFS file's SHA-256
  and renames the directory into place.

Downloads run in a daemon thread, so the UI polls :func:`job_status`; one
runs at a time. :func:`delete` moves a model into the store's ``.trash/``
so :func:`restore` can undo it; the trash is purged :data:`TRASH_TTL`
seconds later (and on the next delete or download).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

import httpx
import platformdirs

from ntasker.i18n import _
from ntasker.paths import APP_NAME

ENGINES = ("vosk", "whisper")

CATALOG_URL = "https://alphacephei.com/vosk/models/model-list.json"
_CATALOG_TTL = 60 * 60
_CATALOG_FIELDS = ("name", "lang", "lang_text", "size", "size_text", "type", "url", "md5")

HF_URL = "https://huggingface.co"
#: Files faster-whisper needs from a model repo (as ``faster_whisper.download_model``).
WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*")
#: ``(name, Hugging Face repo, approx. size in bytes, type)``. Whisper is
#: multilingual, so the entries carry no language.
_WHISPER = (
    ("whisper-tiny", "Systran/faster-whisper-tiny", 78_000_000, "small"),
    ("whisper-base", "Systran/faster-whisper-base", 148_000_000, "small"),
    ("whisper-small", "Systran/faster-whisper-small", 486_000_000, "small"),
    ("whisper-medium", "Systran/faster-whisper-medium", 1_530_000_000, "big"),
    ("whisper-large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1_620_000_000, "big"),
    ("whisper-large-v3", "Systran/faster-whisper-large-v3", 3_090_000_000, "big"),
)

TRASH = ".trash"
TRASH_TTL = 60.0

_catalog: list[dict] | None = None
_catalog_at = 0.0
_job: dict | None = None
_lock = threading.Lock()


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


def models_dir(engine: str) -> Path:
    """Store directory of ``engine`` (``vosk-models`` / ``whisper-models``)."""
    return data_dir() / f"{engine}-models"


def _size_text(size: int) -> str:
    """``1.5GiB`` / ``74.6MiB`` -- the way Vosk's catalog writes sizes."""
    return f"{size / 2**30:.1f}GiB" if size >= 2**30 else f"{size / 2**20:.1f}MiB"


def whisper_catalog() -> list[dict]:
    """The fixed faster-whisper catalog, in the shape of Vosk's entries."""
    return [
        {
            "name": name,
            "engine": "whisper",
            "lang": None,
            "lang_text": _("Multilingual"),
            "size": size,
            "size_text": _size_text(size),
            "type": kind,
            "repo": repo,
        }
        for name, repo, size, kind in _WHISPER
    ]


def fetch_catalog() -> list[dict]:
    """Current (non-obsolete) Vosk catalog entries, cached for an hour. Raises offline."""
    global _catalog, _catalog_at
    with _lock:
        if _catalog is not None and time.time() - _catalog_at < _CATALOG_TTL:
            return list(_catalog)
    resp = httpx.get(CATALOG_URL, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    entries = [
        {**{k: m.get(k) for k in _CATALOG_FIELDS}, "engine": "vosk"}
        for m in resp.json()
        if str(m.get("obsolete", "false")).lower() != "true"
    ]
    entries.sort(key=lambda m: (m["lang_text"] or "", m["size"] or 0))
    with _lock:
        _catalog, _catalog_at = entries, time.time()
    return list(entries)


def find_entry(name: str) -> dict | None:
    """Catalog entry ``name`` -- Whisper's offline, else Vosk's (raises offline)."""
    for entry in whisper_catalog():
        if entry["name"] == name:
            return entry
    return next((m for m in fetch_catalog() if m["name"] == name), None)


def engine_of(path: Path) -> str | None:
    """``vosk`` for a directory with an ``am/`` folder, ``whisper`` for one with
    ``model.bin``, else ``None`` (not a model)."""
    if (path / "am").is_dir():
        return "vosk"
    if (path / "model.bin").is_file():
        return "whisper"
    return None


def installed() -> list[dict]:
    """``[{name, path, engine}]`` of the models in the stores, sorted by name."""
    found = []
    for engine in ENGINES:
        base = models_dir(engine)
        if not base.is_dir():
            continue
        found += [
            {"name": d.name, "path": str(d), "engine": engine}
            for d in base.iterdir()
            if d.is_dir() and engine_of(d) == engine
        ]
    return sorted(found, key=lambda m: m["name"])


def resolve(spec: str) -> Path | None:
    """Model directory for a ``voice_model`` value: a store name or a path."""
    if not spec:
        return None
    candidates = [models_dir(e) / spec for e in ENGINES] + [Path(os.path.expanduser(spec))]
    return next((c for c in candidates if engine_of(c)), None)


def _store_path(name: str) -> Path | None:
    """The store directory of installed model ``name`` (names only, no paths)."""
    return next((Path(m["path"]) for m in installed() if m["name"] == name), None)


def purge_trash() -> None:
    """Delete everything in the stores' trash for good."""
    for engine in ENGINES:
        shutil.rmtree(models_dir(engine) / TRASH, ignore_errors=True)


def delete(name: str) -> None:
    """Move installed model ``name`` into its store's trash (undo with
    :func:`restore`). Raises ``KeyError`` for an unknown name,
    ``RuntimeError`` while that model downloads."""
    with _lock:
        if _job and _job["name"] == name and _job["state"] in ("downloading", "unpacking"):
            raise RuntimeError(_("{name} is still downloading.").format(name=name))
    path = _store_path(name)
    if path is None:
        raise KeyError(name)
    purge_trash()
    trash = path.parent / TRASH
    trash.mkdir(exist_ok=True)
    path.rename(trash / name)
    timer = threading.Timer(TRASH_TTL, shutil.rmtree, (trash / name,), {"ignore_errors": True})
    timer.daemon = True
    timer.start()


def restore(name: str) -> bool:
    """Move model ``name`` back from the trash; ``False`` once it is purged."""
    for engine in ENGINES:
        src = models_dir(engine) / TRASH / name
        dst = models_dir(engine) / name
        if src.is_dir() and not dst.exists():
            src.rename(dst)
            return True
    return False


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
    purge_trash()
    run = _run_whisper if entry["engine"] == "whisper" else _run_vosk
    threading.Thread(target=run, args=(entry,), daemon=True).start()
    return job


def _set(**fields: object) -> None:
    with _lock:
        if _job:
            _job.update(fields)


def _run_vosk(entry: dict) -> None:
    name = entry["name"]
    base = models_dir("vosk")
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
        if engine_of(target) != "vosk":
            raise RuntimeError(f"archive did not contain a model directory {name!r}")
        _set(state="done")
    except Exception as exc:  # noqa: BLE001 -- any failure is reported to the UI
        shutil.rmtree(target, ignore_errors=True)
        _set(state="error", error=str(exc))
    finally:
        part.unlink(missing_ok=True)


def _run_whisper(entry: dict) -> None:
    name, repo = entry["name"], entry["repo"]
    base = models_dir("whisper")
    base.mkdir(parents=True, exist_ok=True)
    part = base / f"{name}.part"
    target = base / name
    try:
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir()
        resp = httpx.get(f"{HF_URL}/api/models/{repo}/tree/main", timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        files = [
            f
            for f in resp.json()
            if f.get("type") == "file" and any(fnmatch.fnmatch(f["path"], p) for p in WHISPER_FILES)
        ]
        _set(total=sum(int(f.get("size") or 0) for f in files))
        received = 0
        for f in files:
            if "/" in f["path"] or f["path"].startswith("."):
                raise RuntimeError("model repository contains an unsafe path")
            digest = hashlib.sha256()
            url = f"{HF_URL}/{repo}/resolve/main/{f['path']}"
            with (
                httpx.stream("GET", url, timeout=30.0, follow_redirects=True) as r,
                (part / f["path"]).open("wb") as fh,
            ):
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 16):
                    fh.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    _set(received=received)
            sha = (f.get("lfs") or {}).get("oid")
            if sha and digest.hexdigest() != sha:
                raise RuntimeError("checksum mismatch")
        if engine_of(part) != "whisper":
            raise RuntimeError(f"{repo} did not contain a model")
        shutil.rmtree(target, ignore_errors=True)
        part.rename(target)
        _set(state="done")
    except Exception as exc:  # noqa: BLE001 -- any failure is reported to the UI
        _set(state="error", error=str(exc))
    finally:
        shutil.rmtree(part, ignore_errors=True)
