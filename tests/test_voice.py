"""Tests for the opt-in voice plugin and the ``plugins_enabled`` switch."""

from __future__ import annotations

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from ntasker import plugins
from ntasker.app import app
from ntasker.cli import main
from ntasker.db import init_db, set_db_path
from ntasker.plugins.voice import models as voice_models
from ntasker.plugins.voice import routes as voice_routes
from ntasker.plugins.voice import validate_voice_model

BASE = "http://127.0.0.1:8766"
WS_HEADERS = {"Host": "127.0.0.1:8766", "Origin": BASE}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    monkeypatch.delenv(plugins.ENV_ENABLED, raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    monkeypatch.setattr(voice_routes, "_cached", None)
    return TestClient(app, base_url=BASE)


def test_voice_is_off_by_default(client):
    listed = {p["name"]: p for p in client.get("/api/plugins").json()}
    assert listed["voice"]["enabled"] is False
    assert listed["voice"]["default_on"] is False
    assert listed["workspace"]["enabled"] is True
    assert "voice" not in plugins.enabled_names()
    assert "voice-tray" not in client.get("/").text


def test_enable_via_setting_and_env(client, monkeypatch):
    r = client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    assert r.status_code == 200, r.text
    assert plugins.is_enabled("voice")
    assert "voice-tray" in client.get("/").text
    monkeypatch.setenv(plugins.ENV_ENABLED, "")
    assert not plugins.is_enabled("voice")
    assert client.put("/api/settings/plugins_enabled", json={"value": '["nope"]'}).status_code == 400


def test_cli_enable_disable(client, tmp_path, capsys):
    db = str(tmp_path / "t.db")
    assert main(["--db", db, "enable", "voice"]) == 0
    assert json.loads(client.get("/api/settings/plugins_enabled").json()["value"]) == ["voice"]
    assert main(["--db", db, "disable", "voice"]) == 0
    assert json.loads(client.get("/api/settings/plugins_enabled").json()["value"]) == []
    # default-on plugins go through plugins_disabled
    assert main(["--db", db, "disable", "pi"]) == 0
    assert json.loads(client.get("/api/settings/plugins_disabled").json()["value"]) == ["pi"]
    assert main(["--db", db, "enable", "pi"]) == 0
    assert json.loads(client.get("/api/settings/plugins_disabled").json()["value"]) == []
    assert main(["--db", db, "enable", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty model store in tmp; ``store.add(name)`` fakes an installed model."""
    base = tmp_path / "vosk-models"
    monkeypatch.setattr(voice_models, "models_dir", lambda: base)
    monkeypatch.setattr(voice_models, "_job", None)
    monkeypatch.setattr(voice_models, "_catalog", None)

    class Store:
        dir = base

        @staticmethod
        def add(name):
            (base / name / "am").mkdir(parents=True)
            return base / name

    return Store


def test_validate_voice_model(store, tmp_path):
    store.add("vosk-model-small-de-0.15")
    assert validate_voice_model(" vosk-model-small-de-0.15 ") == "vosk-model-small-de-0.15"
    custom = store.add("elsewhere")
    assert validate_voice_model(str(custom)) == str(custom)
    with pytest.raises(ValueError):
        validate_voice_model("de")
    with pytest.raises(ValueError):
        validate_voice_model(str(tmp_path))  # a directory, but not a model


def test_model_spec_prefers_ui_language(client, store):
    assert voice_routes.model_spec() is None
    store.add("vosk-model-small-en-us-0.15")
    store.add("vosk-model-small-de-0.15")
    assert voice_routes.model_spec() == "vosk-model-small-de-0.15"
    client.put("/api/settings/language", json={"value": "en"})
    assert voice_routes.model_spec() == "vosk-model-small-en-us-0.15"
    client.put("/api/settings/voice_model", json={"value": "vosk-model-small-de-0.15"})
    assert voice_routes.model_spec() == "vosk-model-small-de-0.15"


def test_ws_refused_while_disabled(client):
    # starlette raises on the 404 / close while the plugin is off
    with pytest.raises(Exception), client.websocket_connect("/api/voice/ws", headers=WS_HEADERS):  # noqa: B017
        pass


def _fake_vosk(monkeypatch, results):
    """Install a stand-in ``vosk`` module: every third chunk yields a final result."""
    calls = {"n": 0, "loaded": []}

    class Model:
        def __init__(self, lang=None, model_path=None):
            calls["loaded"].append(lang or model_path)

    class KaldiRecognizer:
        def __init__(self, model, rate):
            pass

        def AcceptWaveform(self, chunk):  # noqa: N802 -- vosk API
            calls["n"] += 1
            return calls["n"] % 3 == 0

        def Result(self):  # noqa: N802
            return json.dumps({"text": results["final"]})

        def PartialResult(self):  # noqa: N802
            return json.dumps({"partial": results["partial"]})

        def FinalResult(self):  # noqa: N802
            return json.dumps({"text": results["flush"]})

    mod = types.ModuleType("vosk")
    mod.Model = Model
    mod.KaldiRecognizer = KaldiRecognizer
    mod.SetLogLevel = lambda level: None
    monkeypatch.setitem(sys.modules, "vosk", mod)
    return calls


def test_ws_streams_partial_and_final(client, store, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    store.add("vosk-model-small-de-0.15")
    calls = _fake_vosk(monkeypatch, {"partial": "hallo we", "final": "hallo welt", "flush": "ende"})
    with client.websocket_connect("/api/voice/ws", headers=WS_HEADERS) as ws:
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json() == {"type": "status", "text": ""}
        ws.send_bytes(b"\x00" * 3200)
        assert ws.receive_json() == {"type": "partial", "text": "hallo we"}
        ws.send_bytes(b"\x00" * 3200)
        ws.receive_json()
        ws.send_bytes(b"\x00" * 3200)
        assert ws.receive_json() == {"type": "final", "text": "hallo welt"}
        ws.send_text("final")
        assert ws.receive_json() == {"type": "final", "text": "ende"}
    assert calls["loaded"] == [str(store.dir / "vosk-model-small-de-0.15")]


def test_ws_reports_missing_vosk(client, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    monkeypatch.setitem(sys.modules, "vosk", None)
    with client.websocket_connect("/api/voice/ws", headers=WS_HEADERS) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and msg["code"] == "no_vosk"


def test_ws_reports_missing_model(client, store, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    _fake_vosk(monkeypatch, {"partial": "", "final": "", "flush": ""})
    with client.websocket_connect("/api/voice/ws", headers=WS_HEADERS) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and msg["code"] == "no_model"


def test_language_of_model_spec():
    from ntasker.plugins.voice.punct import language_of

    assert language_of("de") == "de"
    assert language_of("en-us") == "en"
    assert language_of("/models/vosk-model-de-0.21") == "de"
    assert language_of("/home/de/vosk-model-small-en-us-0.15/") == "en"
    assert language_of("fr") is None


def test_punctuate_de_and_en():
    from ntasker.plugins.voice.punct import punctuate

    assert punctuate("hallo welt punkt wie geht es fragezeichen gut komma danke", "de") == (
        "hallo welt. Wie geht es? Gut, danke"
    )
    assert punctuate("erstens doppelpunkt neue zeile zweitens strichpunkt absatz drittens", "de") == (
        "erstens:\nZweitens;\n\nDrittens"
    )
    assert punctuate("hello period new paragraph what question mark", "en") == "hello.\n\nWhat?"
    assert punctuate("full stop exclamation point", "en") == ".!"
    assert punctuate("punkt", "en") == "punkt"
    assert punctuate("punkt", None) == "punkt"


def test_ws_applies_punctuation(client, store, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    store.add("vosk-model-de-0.21")
    _fake_vosk(monkeypatch, {"partial": "hallo punkt", "final": "", "flush": "ende fragezeichen"})
    with client.websocket_connect("/api/voice/ws", headers=WS_HEADERS) as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_bytes(b"\x00" * 3200)
        assert ws.receive_json() == {"type": "partial", "text": "hallo."}
        ws.send_text("final")
        assert ws.receive_json() == {"type": "final", "text": "ende?"}


CATALOG = [
    {"name": "vosk-model-small-de-0.15", "lang": "de", "lang_text": "German", "size": 10,
     "size_text": "10B", "type": "small", "url": "https://example.test/de.zip",
     "md5": None, "obsolete": "false"},
    {"name": "vosk-model-de-0.5", "lang": "de", "lang_text": "German", "size": 5,
     "size_text": "5B", "type": "big", "url": "https://example.test/old.zip",
     "md5": None, "obsolete": "true"},
]


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _fake_httpx(monkeypatch, payload: bytes, md5: str | None = None):
    """Stand-in for httpx.get (catalog) and httpx.stream (download)."""
    import contextlib
    import hashlib
    import types

    catalog = [dict(m) for m in CATALOG]
    if md5 is not None:
        catalog[0]["md5"] = md5
    elif md5 is None:
        catalog[0]["md5"] = hashlib.md5(payload, usedforsecurity=False).hexdigest()

    class Resp:
        headers = {"content-length": str(len(payload))}  # noqa: RUF012

        def raise_for_status(self):
            pass

        def json(self):
            return catalog

        def iter_bytes(self, n):
            for i in range(0, len(payload), 4):
                yield payload[i : i + 4]

    @contextlib.contextmanager
    def stream(method, url, **kw):
        yield Resp()

    monkeypatch.setattr(voice_models, "httpx", types.SimpleNamespace(get=lambda *a, **k: Resp(), stream=stream))
    monkeypatch.setattr(voice_models, "_catalog", None)


def _wait_job():
    import time

    for _ in range(200):
        job = voice_models.job_status()
        if job and job["state"] in ("done", "error"):
            return job
        time.sleep(0.01)
    raise AssertionError("download did not finish")


def test_models_endpoint_and_download(client, store, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    payload = _zip_bytes({"vosk-model-small-de-0.15/am/final.mdl": b"x", "vosk-model-small-de-0.15/conf/a": b"y"})
    _fake_httpx(monkeypatch, payload)
    info = client.get("/api/voice/models").json()
    assert info["installed"] == [] and info["current"] is None and info["job"] is None
    assert [m["name"] for m in info["catalog"]] == ["vosk-model-small-de-0.15"]  # obsolete dropped
    assert client.post("/api/voice/models/nope").status_code == 404
    r = client.post("/api/voice/models/vosk-model-small-de-0.15")
    assert r.status_code == 202, r.text
    assert _wait_job()["state"] == "done"
    assert not list(store.dir.glob("*.part"))
    info = client.get("/api/voice/models").json()
    assert [m["name"] for m in info["installed"]] == ["vosk-model-small-de-0.15"]
    assert info["current"] == "vosk-model-small-de-0.15"
    # the card selects a model by name
    assert client.put("/api/settings/voice_model", json={"value": "vosk-model-small-de-0.15"}).status_code == 200


def test_download_rejects_bad_checksum_and_unsafe_zip(client, store, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    payload = _zip_bytes({"vosk-model-small-de-0.15/am/final.mdl": b"x"})
    _fake_httpx(monkeypatch, payload, md5="0" * 32)
    client.post("/api/voice/models/vosk-model-small-de-0.15")
    job = _wait_job()
    assert job["state"] == "error" and "checksum" in job["error"]
    assert voice_models.installed() == []

    payload = _zip_bytes({"../escape": b"x"})
    _fake_httpx(monkeypatch, payload)
    client.post("/api/voice/models/vosk-model-small-de-0.15")
    job = _wait_job()
    assert job["state"] == "error" and "unsafe" in job["error"]
    assert not (store.dir.parent / "escape").exists()


def test_cli_enable_installs_missing_extra(client, tmp_path, monkeypatch, capsys):
    import subprocess

    ran = []
    monkeypatch.setattr(plugins, "missing_requirements", lambda extra: ["vosk>=0.3.45"])
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (ran.append(cmd), types.SimpleNamespace(returncode=0))[1])
    assert main(["--db", str(tmp_path / "t.db"), "enable", "voice"]) == 0
    assert ran and ran[0][-1] == "vosk>=0.3.45"
    assert plugins.is_enabled("voice")
    # a failing installer leaves the plugin off
    client.put("/api/settings/plugins_enabled", json={"value": "[]"})
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1))
    assert main(["--db", str(tmp_path / "t.db"), "enable", "voice"]) == 1
    assert not plugins.is_enabled("voice")


def test_install_command_is_durable_in_uv_tool_homes(monkeypatch):
    from ntasker import service

    monkeypatch.setattr(service.sys, "executable", "/home/u/.local/share/uv/tools/ntasker/bin/python")
    assert service.resolve_install_command("voice", ["vosk>=0.3.45"]) == ["uv", "tool", "install", "ntasker[voice]"]
    assert service.install_needs_restart()
    monkeypatch.setattr(service.sys, "executable", "/opt/venv/bin/python")
    assert service.resolve_install_command("voice", ["vosk>=0.3.45"])[-1] == "vosk>=0.3.45"
    assert not service.install_needs_restart()


def test_api_installs_missing_extra_in_background(client, monkeypatch):
    import subprocess
    import time

    ran = []
    monkeypatch.setattr(plugins, "missing_requirements", lambda extra: ["vosk>=0.3.45"])
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (ran.append(cmd), types.SimpleNamespace(returncode=0, stdout="ok", stderr=""))[1],
    )
    assert client.post("/api/plugins/claude/install").status_code == 400   # no extra
    assert client.post("/api/plugins/nope/install").status_code == 404
    r = client.post("/api/plugins/voice/install")
    assert r.status_code == 202, r.text
    for _ in range(50):
        job = client.get("/api/plugins/install").json()["job"]
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job["state"] == "done" and job["plugin"] == "voice" and ran[0][-1] == "vosk>=0.3.45"
    assert [p for p in client.get("/api/plugins").json() if p["name"] == "voice"][0]["missing"] == ["vosk>=0.3.45"]

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    client.post("/api/plugins/voice/install")
    for _ in range(50):
        job = client.get("/api/plugins/install").json()["job"]
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job["state"] == "failed" and job["output"] == "boom"
