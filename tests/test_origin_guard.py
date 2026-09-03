"""Tests for the CSRF / DNS-rebinding guard (:mod:`ntasker.middleware`).

ntasker has no auth layer and binds to loopback, so the browser's ambient
authority is all an attacker needs: any web page the user visits can POST to
127.0.0.1:8766, or -- worse -- open a WebSocket to the interactive Claude
session, because WebSocket upgrades are exempt from the same-origin policy.

These tests pin the guard's contract on both fronts.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ntasker.middleware import ALLOWED_HOSTS_ENV, OriginGuardMiddleware

LOCAL = "127.0.0.1:8766"
EVIL = "http://evil.example"


@pytest.fixture
def client() -> TestClient:
    """A minimal app carrying only the guard -- no DB, no ntasker routes."""
    app = FastAPI()

    @app.get("/read")
    def read() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/write")
    def write() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("shell")
        await websocket.close()

    app.add_middleware(OriginGuardMiddleware)
    return TestClient(app, base_url=f"http://{LOCAL}")


# --- the actual attacks --------------------------------------------------


def test_cross_origin_post_is_refused(client: TestClient) -> None:
    """The /shutdown-style attack: a hostile page POSTs to our loopback port."""
    resp = client.post("/write", headers={"Origin": EVIL})
    assert resp.status_code == 403
    assert "cross_origin" in resp.json()["detail"]


def test_cross_origin_websocket_is_refused(client: TestClient) -> None:
    """The worst case: a hostile page opening the interactive-shell socket.

    WebSocket upgrades ignore the same-origin policy, so without the guard
    ``evil.example`` gets a working connection.
    """
    # TestClient sends ``Host: testserver`` on upgrades regardless of
    # base_url, so pin it -- otherwise this passes via the Host check and
    # never exercises the Origin one.
    with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012
        with client.websocket_connect(
            "/ws", headers={"Host": LOCAL, "Origin": EVIL}
        ) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    assert exc.value.reason == "cross_origin"


def test_dns_rebinding_is_refused(client: TestClient) -> None:
    """A rebound name resolves to us and is *same-origin*, so Origin matches.

    Only the Host check catches this one -- which is why it exists.
    """
    resp = client.post(
        "/write",
        headers={"Host": "evil.example", "Origin": EVIL},
    )
    assert resp.status_code == 403
    assert "host_not_allowed" in resp.json()["detail"]


def test_rebinding_check_also_covers_reads(client: TestClient) -> None:
    """A rebound GET would exfiltrate every task; safe methods are guarded too."""
    resp = client.get("/read", headers={"Host": "evil.example"})
    assert resp.status_code == 403


def test_origin_null_is_refused(client: TestClient) -> None:
    """Sandboxed iframes and file:// pages send ``Origin: null``."""
    resp = client.post("/write", headers={"Origin": "null"})
    assert resp.status_code == 403


# --- traffic that must keep working --------------------------------------


def test_same_origin_post_passes(client: TestClient) -> None:
    """The app's own frontend (Alpine fetch) sends a matching Origin."""
    resp = client.post("/write", headers={"Origin": f"http://{LOCAL}"})
    assert resp.status_code == 200


def test_post_without_origin_passes(client: TestClient) -> None:
    """Non-browser clients carry no ambient authority.

    ``ntasker stop`` POSTs /shutdown via stdlib urllib, which sends no
    Origin header -- refusing it would break the CLI without buying safety.
    """
    resp = client.post("/write")
    assert resp.status_code == 200


def test_same_origin_websocket_passes(client: TestClient) -> None:
    with client.websocket_connect(
        "/ws", headers={"Host": LOCAL, "Origin": f"http://{LOCAL}"}
    ) as ws:
        assert ws.receive_text() == "shell"


def test_localhost_alias_passes(client: TestClient) -> None:
    """Reaching the UI via ``localhost`` instead of the IP literal is fine."""
    resp = client.post(
        "/write",
        headers={"Host": "localhost:8766", "Origin": "http://localhost:8766"},
    )
    assert resp.status_code == 200


def test_explicit_allowed_host_passes(client: TestClient) -> None:
    """``ntasker serve --host`` propagates its bind address into the guard."""
    os.environ[ALLOWED_HOSTS_ENV] = "192.168.1.5"
    try:
        resp = client.post(
            "/write",
            headers={"Host": "192.168.1.5:8766", "Origin": "http://192.168.1.5:8766"},
        )
        assert resp.status_code == 200
    finally:
        del os.environ[ALLOWED_HOSTS_ENV]


def test_mismatched_port_is_refused(client: TestClient) -> None:
    """Another local dev server is a different origin, loopback or not."""
    resp = client.post(
        "/write",
        headers={"Host": LOCAL, "Origin": "http://127.0.0.1:3000"},
    )
    assert resp.status_code == 403
