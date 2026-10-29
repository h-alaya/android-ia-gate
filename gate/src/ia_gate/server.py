"""MCP server assembly: one `MCPServer` with the tools of every `ia_gate.tools` module."""

from __future__ import annotations

import importlib
import pkgutil

from mcp.server.mcpserver import MCPServer

from ia_gate import __version__, tools
from ia_gate.context import GateContext

INSTRUCTIONS = """\
IA Gate drives Android devices (terminals, validators) connected over adb to the gate PC.

Devices are addressed by a stable gate ID such as VAL-042 (`device_id` argument, case-insensitive),
never by their adb serial: the ID survives reboots, IP changes and USB <-> Wi-Fi switches.

Workflow:
1. `list_devices` shows enrolled devices with their state (online, offline, unauthorized,
   disconnected) and connected devices that are not enrolled yet (device_id null, with their adb serial).
2. To enroll a new device, call `enroll_device(adb_serial, device_id, label)` with the adb serial
   shown by `list_devices` and a new ID (2-32 chars A-Z 0-9 _ -). It writes a marker on the device.
3. Every other tool takes the `device_id`. Calls on the same device run one after the other;
   different devices are handled in parallel.
4. A device on Wi-Fi is reached with `connect_wifi(host, port)` (after `adb tcpip 5555` over USB).

Error messages are in French and say what to do (accept the RSA key, plug the device in, ...):
relay them to the user when you cannot act on them yourself.
"""


def _register_tools(server: MCPServer, ctx: GateContext) -> None:
    for module_info in sorted(pkgutil.iter_modules(tools.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{tools.__name__}.{module_info.name}")
        module.register(server, ctx)


def build_server(ctx: GateContext) -> MCPServer:
    server = MCPServer(name="ia-gate", instructions=INSTRUCTIONS, version=__version__)
    _register_tools(server, ctx)
    return server
