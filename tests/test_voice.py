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


def test_validate_voice_model(tmp_path):
    assert validate_voice_model(" de ") == "de"
    assert validate_voice_model("en-us") == "en-us"
    assert validate_voice_model(str(tmp_path)) == str(tmp_path)
    with pytest.raises(ValueError):
        validate_voice_model("/no/such/dir")


def test_model_spec_follows_language(client):
    assert voice_routes.model_spec() == "en"
    client.put("/api/settings/language", json={"value": "de"})
    assert voice_routes.model_spec() == "de"
    client.put("/api/settings/voice_model", json={"value": "fr"})
    assert voice_routes.model_spec() == "fr"


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


def test_ws_streams_partial_and_final(client, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    client.put("/api/settings/voice_model", json={"value": "de"})
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
    assert calls["loaded"] == ["de"]


def test_ws_reports_missing_vosk(client, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    monkeypatch.setitem(sys.modules, "vosk", None)
    with client.websocket_connect("/api/voice/ws", headers=WS_HEADERS) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "ntasker[voice]" in msg["text"]


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


def test_ws_applies_punctuation(client, monkeypatch):
    client.put("/api/settings/plugins_enabled", json={"value": '["voice"]'})
    client.put("/api/settings/voice_model", json={"value": "de"})
    _fake_vosk(monkeypatch, {"partial": "hallo punkt", "final": "", "flush": "ende fragezeichen"})
    with client.websocket_connect("/api/voice/ws", headers=WS_HEADERS) as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_bytes(b"\x00" * 3200)
        assert ws.receive_json() == {"type": "partial", "text": "hallo."}
        ws.send_text("final")
        assert ws.receive_json() == {"type": "final", "text": "ende?"}
