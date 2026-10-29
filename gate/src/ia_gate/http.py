"""Streamable HTTP transport: the MCP app behind bearer-token auth and DNS-rebinding protection (spec §6)."""

from __future__ import annotations

import hmac

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ia_gate.config import LOOPBACK_HOSTS, GateConfig

_UNAUTHORIZED_MESSAGE = (
    "Token manquant ou invalide. Envoyez l'en-tête `Authorization: Bearer <token>` avec la valeur "
    "de la variable d'environnement configurée sur la gate (IA_GATE_TOKEN par défaut)."
)


def _bearer_token(headers: Headers) -> bytes | None:
    """Raw token of an `Authorization: Bearer <token>` header, else None."""
    value = headers.get("authorization")
    if value is None:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    # Header values are latin-1 decoded by Starlette: re-encode to compare raw bytes.
    return token.strip().encode("latin-1")


class BearerTokenMiddleware:
    """Pure ASGI middleware: HTTP/WebSocket requests must carry `Authorization: Bearer <token>`.

    With `token=None` every request passes through; `validate_for_http` only allows that on a
    loopback bind.
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self._token = token.encode("utf-8") if token is not None else None

    def _authorized(self, scope: Scope) -> bool:
        assert self._token is not None
        provided = _bearer_token(Headers(scope=scope))
        return provided is not None and hmac.compare_digest(provided, self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._token is None or scope["type"] not in ("http", "websocket") or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"error": "unauthorized", "message": _UNAUTHORIZED_MESSAGE},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def _host_header(host: str, port: int) -> str:
    # An IPv6 literal appears bracketed in the Host header: `[::1]:8765`.
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def build_http_app(server: MCPServer, cfg: GateConfig) -> Starlette:
    hosts = [cfg.host]
    if cfg.host in LOOPBACK_HOSTS:
        hosts += [h for h in ("localhost", "127.0.0.1") if h not in hosts]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_host_header(h, cfg.port) for h in hosts],
        allowed_origins=[],
    )
    app = server.streamable_http_app(host=cfg.host, transport_security=security)
    app.add_middleware(BearerTokenMiddleware, token=cfg.token())
    return app
