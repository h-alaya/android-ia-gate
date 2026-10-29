"""Streamable HTTP transport: the MCP app behind bearer-token auth and DNS-rebinding protection (spec §6)."""

from __future__ import annotations

import hmac

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ia_gate.config import LOOPBACK_HOSTS, WILDCARD_HOSTS, GateConfig

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


class _AllowedHostMiddleware:
    """Pure ASGI middleware: reject a Host header outside the allowlist with an actionable French 421.

    Mirrors the SDK's DNS-rebinding Host check (same exact-match list), which otherwise answers with
    a bare English "Invalid Host header".
    """

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self._allowed = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        host = Headers(scope=scope).get("host", "") if scope["type"] in ("http", "websocket") else None
        if host is None or host in self._allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        name = _host_name(host)
        message = (
            f"En-tête Host « {host} » refusé par la protection DNS-rebinding de la gate. "
            f"Connectez-vous via une adresse autorisée ({', '.join(self._allowed)}) "
            f'ou ajoutez « {name} » à server.allowed_hosts (ex. allowed_hosts = ["{name}"]) '
            f"dans la configuration de la gate, puis redémarrez-la."
        )
        response = JSONResponse({"error": "invalid_host", "message": message}, status_code=421)
        await response(scope, receive, send)


def _host_name(host_header: str) -> str:
    """Host part of a Host header value, without port or IPv6 brackets."""
    if host_header.startswith("["):
        return host_header[1:].partition("]")[0]
    return host_header.rpartition(":")[0] if host_header.count(":") == 1 else host_header


def _host_header(host: str, port: int) -> str:
    # An IPv6 literal appears bracketed in the Host header: `[::1]:8765`.
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _allowed_host_headers(cfg: GateConfig) -> list[str]:
    # A wildcard bind is not a name clients can use: only `server.allowed_hosts` applies then.
    hosts = [] if cfg.host in WILDCARD_HOSTS else [cfg.host]
    if cfg.host in LOOPBACK_HOSTS:
        hosts += ["localhost", "127.0.0.1"]
    hosts += cfg.allowed_hosts
    return [_host_header(h, cfg.port) for h in dict.fromkeys(hosts)]


def build_http_app(server: MCPServer, cfg: GateConfig) -> Starlette:
    allowed = _allowed_host_headers(cfg)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=[],
    )
    app = server.streamable_http_app(host=cfg.host, transport_security=security)
    # Last added runs first: auth, then the Host check (so only authenticated clients see the hint).
    app.add_middleware(_AllowedHostMiddleware, allowed_hosts=allowed)
    app.add_middleware(BearerTokenMiddleware, token=cfg.token())
    return app
