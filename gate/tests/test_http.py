"""HTTP transport: bearer token auth and DNS-rebinding protection around the MCP endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

# httpx2 is the HTTP client the MCP SDK ships with (same API as httpx).
import httpx2 as httpx
import pytest

from ia_gate.config import GateConfig, load_config
from ia_gate.http import BearerTokenMiddleware, build_http_app

TOKEN = "s3cret-token"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@asynccontextmanager
async def http_client(server, cfg: GateConfig, host: str = "127.0.0.1") -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client talking to the gate's ASGI app, run inside the app lifespan."""
    app = build_http_app(server, cfg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}:{cfg.port}") as c:
            yield c


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("IA_GATE_TOKEN", TOKEN)


async def test_http_rejects_missing_token(server, cfg, with_token):
    async with http_client(server, cfg) as c:
        response = await c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


async def test_http_rejects_wrong_token(server, cfg, with_token):
    async with http_client(server, cfg) as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer wrong-token"}
        )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


async def test_http_rejects_non_bearer_scheme(server, cfg, with_token):
    async with http_client(server, cfg) as c:
        response = await c.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": TOKEN})
    assert response.status_code == 401


async def test_http_rejects_non_ascii_token_without_crashing(server, cfg, with_token):
    async with http_client(server, cfg) as c:
        response = await c.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": "Bearer s3crét".encode("latin-1")},
        )
    assert response.status_code == 401


async def test_http_accepts_good_token(server, cfg, with_token):
    async with http_client(server, cfg) as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200, response.text
    assert '"serverInfo"' in response.text
    assert "ia-gate" in response.text


async def test_http_without_token_on_loopback_passes_through(server, cfg, monkeypatch):
    monkeypatch.delenv("IA_GATE_TOKEN", raising=False)
    async with http_client(server, cfg, host="localhost") as c:
        response = await c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 200, response.text


async def test_http_rejects_foreign_host_header(server, cfg, with_token):
    async with http_client(server, cfg, host="evil.example") as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 421


async def test_http_lan_host_is_allowed(server, cfg, with_token):
    lan = replace(cfg, host="192.168.1.10")
    async with http_client(server, lan, host="192.168.1.10") as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200, response.text


async def test_bearer_middleware_passes_lifespan_through():
    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = BearerTokenMiddleware(app, token=TOKEN)
    await middleware({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


async def test_http_wildcard_bind_rejects_lan_host_with_french_hint(server, cfg, with_token):
    wildcard = replace(cfg, host="0.0.0.0")
    async with http_client(server, wildcard, host="192.168.1.10") as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 421
    body = response.json()
    assert body["error"] == "invalid_host"
    assert "server.allowed_hosts" in body["message"]
    assert "192.168.1.10" in body["message"]


@pytest.mark.parametrize("client_host", ["192.168.1.10", "gatepc.local"])
async def test_http_allowed_hosts_accepts_ip_and_hostname(server, cfg, with_token, client_host):
    wildcard = replace(cfg, host="0.0.0.0", allowed_hosts=("192.168.1.10", "gatepc.local"))
    async with http_client(server, wildcard, host=client_host) as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200, response.text


async def test_http_host_check_runs_after_auth(server, cfg, with_token):
    # Unauthenticated clients must not learn the allowlist from the 421 hint.
    async with http_client(server, cfg, host="evil.example") as c:
        response = await c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401


@pytest.mark.parametrize("client_host", ["localhost", "127.0.0.1"])
async def test_http_wildcard_bind_accepts_local_clients(server, cfg, with_token, client_host):
    # 0.0.0.0 also listens on loopback: Claude Code on the gate PC keeps using localhost.
    wildcard = replace(cfg, host="0.0.0.0", allowed_hosts=("192.168.1.10",))
    async with http_client(server, wildcard, host=client_host) as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200, response.text


async def test_http_allowed_host_configured_in_upper_case(server, cfg, with_token, tmp_path):
    # Clients lowercase the host name they send; the configured spelling must not matter.
    (tmp_path / "c.toml").write_text('[server]\nhost = "0.0.0.0"\nallowed_hosts = ["GatePC.local"]\n')
    loaded = load_config(tmp_path / "c.toml")
    async with http_client(server, replace(cfg, host=loaded.host, allowed_hosts=loaded.allowed_hosts),
                           host="gatepc.local") as c:
        response = await c.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200, response.text
