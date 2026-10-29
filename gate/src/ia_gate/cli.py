"""`ia-gate` command line: run the MCP server, list and enroll devices, generate a token."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from ia_gate.adb import AdbClient, AdbError, Runner, subprocess_runner
from ia_gate.audit import AuditLog
from ia_gate.config import ConfigError, GateConfig, load_config, validate_for_http
from ia_gate.context import GateContext
from ia_gate.http import build_http_app
from ia_gate.identity import normalize_device_id
from ia_gate.registry import DeviceRegistry, RegistryError
from ia_gate.resolver import DeviceResolver, DeviceUnavailable
from ia_gate.server import build_server

# Failures whose message tells the user what to do: printed as is, exit code 1.
_USER_ERRORS = (DeviceUnavailable, AdbError, RegistryError, ValueError)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_context(cfg: GateConfig, runner: Runner = subprocess_runner) -> GateContext:
    adb = AdbClient(runner, cfg.adb_path, cfg.adb_timeout_s)
    registry = DeviceRegistry(cfg.registry_path)
    return GateContext(
        config=cfg,
        adb=adb,
        registry=registry,
        resolver=DeviceResolver(adb, registry),
        audit=AuditLog(cfg.audit_log_path),
        clock=_now,
    )


def _context(cfg: GateConfig) -> GateContext:
    # Looked up at call time so tests can swap the module-level runner.
    return build_context(cfg, subprocess_runner)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ia-gate", description="IA Gate : serveur MCP pour piloter des équipements Android via adb."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    config = argparse.ArgumentParser(add_help=False)
    config.add_argument(
        "--config", type=Path, default=None, help="fichier de configuration (défaut : ~/.ia-gate/config.toml)"
    )

    serve = sub.add_parser("serve", parents=[config], help="lancer le serveur MCP")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")

    sub.add_parser("devices", parents=[config], help="lister les équipements enrôlés et connectés")

    enroll = sub.add_parser("enroll", parents=[config], help="enrôler un appareil connecté")
    enroll.add_argument("adb_serial", metavar="ADB_SERIAL", help="serial adb (voir `ia-gate devices`)")
    enroll.add_argument("device_id", metavar="DEVICE_ID", help="ID gate, ex. VAL-042")
    enroll.add_argument("--label", default="", help="libellé libre (ex. « Caisse 1 »)")

    sub.add_parser("token", help="générer un token aléatoire pour IA_GATE_TOKEN")
    return parser


def _serve(cfg: GateConfig, transport: str) -> int:
    if transport == "http":
        validate_for_http(cfg)
    server = build_server(_context(cfg))
    if transport == "http":
        uvicorn.run(build_http_app(server, cfg), host=cfg.host, port=cfg.port)
    else:
        server.run("stdio")
    return 0


def _devices(cfg: GateConfig) -> int:
    statuses = asyncio.run(_context(cfg).resolver.snapshot())
    rows = [("DEVICE_ID", "STATE", "ADB_SERIAL", "MODEL", "LABEL")]
    rows += [
        (s.device_id or "-", s.state, s.adb_serial or "-", s.model or "-", s.label or "") for s in statuses
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        print("  ".join([*cells, row[-1]]).rstrip())
    if not statuses:
        print("Aucun équipement enrôlé ni connecté. Branchez un appareil ou utilisez `adb connect`.")
    return 0


def _enroll(cfg: GateConfig, adb_serial: str, device_id: str, label: str) -> int:
    key = normalize_device_id(device_id)
    ctx = _context(cfg)
    record = asyncio.run(ctx.resolver.enroll(adb_serial, key, label, ctx.clock()))
    print(f"{record.device_id} enrôlé (serial adb {adb_serial}, modèle {record.fingerprint.model or '?'}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "token":
        print(secrets.token_urlsafe(32))
        return 0
    try:
        cfg = load_config(args.config)
        if args.command == "serve":
            return _serve(cfg, args.transport)
        if args.command == "devices":
            return _devices(cfg)
        return _enroll(cfg, args.adb_serial, args.device_id, args.label)
    except ConfigError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 2
    except _USER_ERRORS as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
