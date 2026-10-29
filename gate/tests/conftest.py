"""Shared fixtures: a FakeRunner-backed gate context, MCP server and in-memory MCP client."""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator, Sequence

import pytest
from fakes import FakeRunner
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from ia_gate.adb import AdbClient, AdbResult
from ia_gate.audit import AuditLog
from ia_gate.config import GateConfig
from ia_gate.context import GateContext
from ia_gate.identity import MARKER_PATH, Fingerprint
from ia_gate.registry import DeviceRegistry
from ia_gate.resolver import IDENTITY_PROBE, DeviceResolver
from ia_gate.server import build_server

NOW = "2026-10-29T10:00:00+00:00"
CAT = ["shell", "cat", MARKER_PATH]
PROBE = ["shell", "sh", "-c", shlex.quote(IDENTITY_PROBE)]


class DeviceBench:
    """Simulated connected devices on top of a `FakeRunner`.

    `add()` registers the adb answers a device gives to the gate's identity reads; a marker
    written by `write_marker` becomes visible to later marker reads and identity probes.
    """

    def __init__(self, fake: FakeRunner) -> None:
        self.fake = fake
        self._lines: list[str] = []
        self._fingerprints: dict[str, Fingerprint] = {}

    async def run(self, args: Sequence[str], timeout: float) -> AdbResult:
        result = await self.fake(args, timeout)
        args = list(args)
        if args[3:6] == ["shell", "sh", "-c"] and args[6:] != PROBE[3:]:
            command = shlex.split(args[6])[0]
            if command.startswith("echo ") and command.endswith(f"> {MARKER_PATH}"):
                serial = args[2]
                marker = shlex.split(command)[1]
                self.fake.on(CAT, stdout=marker + "\n", serial=serial)
                self._probe(serial, self._fingerprints[serial], marker)
        return result

    def _probe(self, serial: str, fp: Fingerprint, marker: str | None) -> None:
        stdout = (
            f"serialno={fp.serialno}\nandroid_id={fp.android_id or 'null'}\n"
            f"boot_id=boot-{serial}\nmarker={marker or ''}\n"
        )
        self.fake.on(PROBE, stdout=stdout, serial=serial)

    def add(self, serial: str, fp: Fingerprint, marker: str | None = None, state: str = "device") -> None:
        """Connect a device reachable as `adb -s serial` in adb state `state`."""
        transport_id = len(self._lines) + 1
        self._lines.append(
            f"{serial}\t{state} usb:1-1 product:p model:{fp.model.replace(' ', '_')} "
            f"device:d transport_id:{transport_id}"
        )
        self.fake.on(["devices", "-l"], stdout="List of devices attached\n" + "\n".join(self._lines) + "\n")
        self._fingerprints[serial] = fp
        self.fake.on(["shell", "getprop", "ro.serialno"], stdout=fp.serialno + "\n", serial=serial)
        self.fake.on(
            ["shell", "settings", "get", "secure", "android_id"],
            stdout=(fp.android_id or "null") + "\n",
            serial=serial,
        )
        self.fake.on(
            ["shell", "getprop", "ro.product.manufacturer"], stdout=fp.manufacturer + "\n", serial=serial
        )
        self.fake.on(["shell", "getprop", "ro.product.model"], stdout=fp.model + "\n", serial=serial)
        self.fake.on(["shell", "sh", "-c"], serial=serial)
        if marker is None:
            self.fake.on(CAT, returncode=1, stderr="No such file or directory", serial=serial)
        else:
            self.fake.on(CAT, stdout=marker + "\n", serial=serial)
        self._probe(serial, fp, marker)


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def bench(fake: FakeRunner) -> DeviceBench:
    return DeviceBench(fake)


@pytest.fixture
def cfg(tmp_path) -> GateConfig:
    return GateConfig(
        registry_path=tmp_path / "state" / "devices.json",
        audit_log_path=tmp_path / "state" / "audit.jsonl",
        apk_dirs=(tmp_path / "apks",),
    )


@pytest.fixture
def ctx(cfg: GateConfig, bench: DeviceBench) -> GateContext:
    adb = AdbClient(bench.run, cfg.adb_path, cfg.adb_timeout_s)
    registry = DeviceRegistry(cfg.registry_path)
    return GateContext(
        config=cfg,
        adb=adb,
        registry=registry,
        resolver=DeviceResolver(adb, registry),
        audit=AuditLog(cfg.audit_log_path),
        clock=lambda: NOW,
    )


@pytest.fixture
def server(ctx: GateContext) -> MCPServer:
    return build_server(ctx)


@pytest.fixture
async def client(server: MCPServer) -> AsyncIterator[Client]:
    """An in-memory MCP client connected to `server`.

    pytest-asyncio runs fixture setup and teardown in different tasks, while `Client` holds an
    anyio cancel scope that must be exited in the task that entered it: one task owns it.
    """
    ready: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
    done = asyncio.Event()

    async def hold() -> None:
        async with Client(server) as c:
            ready.set_result(c)
            await done.wait()

    task = asyncio.create_task(hold())
    await asyncio.wait({ready, task}, return_when=asyncio.FIRST_COMPLETED)
    if not ready.done():
        await task  # connection failed: raise its error
    try:
        yield ready.result()
    finally:
        done.set()
        await task
