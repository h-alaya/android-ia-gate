"""Device tools: list, enroll, describe and connect devices over Wi-Fi (spec §5)."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer

from ia_gate.adb import AdbError, AdbNotFound, AdbTimeout, Target
from ia_gate.context import GateContext, audited, device_call
from ia_gate.identity import normalize_device_id, read_fingerprint

_BATTERY_LEVEL_RE = re.compile(r"^\s*level:\s*(\d+)\s*$", re.MULTILINE)
_SCREEN_SIZE_RE = re.compile(r"^\s*(Physical|Override) size:\s*(\d+x\d+)\s*$", re.MULTILINE)
_HOST_RE = re.compile(r"^[A-Za-z0-9._:%\[\]-]+$")


def parse_battery_level(text: str) -> int | None:
    match = _BATTERY_LEVEL_RE.search(text)
    return int(match.group(1)) if match else None


def parse_screen_size(text: str) -> str | None:
    """`wm size` output: the override size when one is set, else the physical size."""
    sizes = dict(_SCREEN_SIZE_RE.findall(text))
    return sizes.get("Override") or sizes.get("Physical")


async def _optional_shell(ctx: GateContext, target: Target, args: list[str]) -> str:
    """Output of a command whose failure only leaves a field empty (a timeout still fails the call)."""
    try:
        return (await ctx.adb.shell(target, args)).text
    except (AdbTimeout, AdbNotFound):
        raise
    except AdbError:
        return ""


def register(server: MCPServer, ctx: GateContext) -> None:
    async def list_devices() -> list[dict[str, Any]]:
        """List enrolled devices with their state (online, offline, unauthorized, disconnected) and the
        connected devices that are not enrolled yet (device_id null; enroll them with enroll_device
        using their adb_serial)."""
        return [asdict(status) for status in await ctx.resolver.snapshot()]

    async def enroll_device(adb_serial: str, device_id: str, label: str = "") -> dict[str, Any]:
        """Enroll the connected device `adb_serial` (as shown by list_devices) under the gate ID
        `device_id` (e.g. VAL-042) with an optional human label; writes an ID marker on the device."""
        key = normalize_device_id(device_id)
        async with ctx.resolver.lock(key):
            record = await ctx.resolver.enroll(adb_serial, key, label, ctx.clock())
        return {"device_id": record.device_id, **record.to_dict()}

    async def device_info(device_id: str) -> dict[str, Any]:
        """Describe an enrolled device: fingerprint (serialno, android_id, manufacturer, model),
        Android version, battery level (%) and screen size."""
        key = normalize_device_id(device_id)

        async def op(target: Target) -> dict[str, Any]:
            fingerprint = await read_fingerprint(ctx.adb, target)
            android_version = await ctx.adb.getprop(target, "ro.build.version.release")
            battery = await _optional_shell(ctx, target, ["dumpsys", "battery"])
            screen = await _optional_shell(ctx, target, ["wm", "size"])
            record = ctx.registry.get(key)
            return {
                "device_id": key,
                "label": record.label if record else "",
                "adb_serial": str(target),
                "fingerprint": fingerprint.to_dict(),
                "android_version": android_version or None,
                "battery_level": parse_battery_level(battery),
                "screen_size": parse_screen_size(screen),
            }

        return await device_call(ctx, key, op)

    async def connect_wifi(host: str, port: int = 5555) -> str:
        """Connect to a device over Wi-Fi (`adb connect host:port`). The device must have been switched
        to TCP mode first (`adb tcpip 5555` over USB). Then use list_devices."""
        host = host.strip()
        # One colon is `ip:port` (an IPv6 address has several): the port has its own argument.
        if not host or not _HOST_RE.fullmatch(host) or host.count(":") == 1:
            raise ValueError(
                f"Adresse invalide : {host!r}. Indiquez l'IP ou le nom d'hôte de l'équipement "
                "(ex. 192.168.1.50), sans port ni espace."
            )
        if not 1 <= port <= 65535:
            raise ValueError(f"Port invalide : {port}. Indiquez un port entre 1 et 65535 (5555 par défaut).")
        output = await ctx.adb.connect(host.strip("[]"), port)
        return (
            f"{output}\nUtilisez list_devices pour voir l'appareil (et enroll_device s'il n'est pas enrôlé)."
        )

    for fn in (list_devices, enroll_device, device_info, connect_wifi):
        server.add_tool(audited(ctx, fn))
