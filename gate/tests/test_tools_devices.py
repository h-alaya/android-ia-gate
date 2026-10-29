import asyncio
import json
import shlex

import pytest
from conftest import NOW
from mcp.server.mcpserver.exceptions import ToolError

from ia_gate.adb import AdbError
from ia_gate.context import audited, device_call
from ia_gate.identity import MARKER_PATH, Fingerprint

FP_A = Fingerprint("R58M12AAAAA", "aaaa1111", "samsung", "SM-A505F")
FP_B = Fingerprint("R58M12BBBBB", "bbbb2222", "Zebra", "TC52")

BATTERY = """Current Battery Service state:
  AC powered: false
  USB powered: true
  status: 2
  health: 2
  present: true
  level: 87
  scale: 100
  voltage: 4231
"""


def text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def data(result):
    structured = result.structured_content
    assert structured is not None, text(result)
    return structured["result"] if set(structured) == {"result"} else structured


def audit_entries(ctx):
    return [json.loads(line) for line in ctx.config.audit_log_path.read_text(encoding="utf-8").splitlines()]


def device_info_answers(fake, serial):
    fake.on(["shell", "getprop", "ro.build.version.release"], stdout="14\n", serial=serial)
    fake.on(["shell", "dumpsys", "battery"], stdout=BATTERY, serial=serial)
    fake.on(["shell", "wm", "size"], stdout="Physical size: 1080x2400\n", serial=serial)


async def test_list_tools_names(client):
    tools = (await client.list_tools()).tools
    assert sorted(tool.name for tool in tools) == [
        "clear_logs",
        "connect_wifi",
        "device_info",
        "enroll_device",
        "get_logs",
        "install_apk",
        "launch_app",
        "list_devices",
        "screenshot",
        "stop_app",
        "ui_dump",
        "ui_key",
        "ui_tap",
        "ui_text",
        "uninstall_app",
    ]
    assert all(tool.description for tool in tools)


async def test_server_instructions_explain_ids_and_enrollment(server):
    assert server.name == "ia-gate"
    assert "enroll_device" in server.instructions
    assert "device_id" in server.instructions


async def test_list_devices_via_client(client, ctx, bench):
    ctx.registry.enroll("VAL-001", FP_A, "Caisse 1", NOW)
    ctx.registry.enroll("VAL-002", Fingerprint("R58M99", "ffff", "samsung", "SM-X"), "", NOW)
    bench.add("R58A", FP_A, marker="VAL-001")
    bench.add("R58B", FP_B)

    result = await client.call_tool("list_devices", {})

    assert not result.is_error, text(result)
    assert data(result) == [
        {
            "device_id": "VAL-001",
            "adb_serial": "R58A",
            "state": "online",
            "label": "Caisse 1",
            "model": "SM-A505F",
        },
        {"device_id": "VAL-002", "adb_serial": None, "state": "disconnected", "label": "", "model": "SM-X"},
        {"device_id": None, "adb_serial": "R58B", "state": "online", "label": "", "model": "TC52"},
    ]


async def test_enroll_then_device_info(client, ctx, bench, fake):
    bench.add("R58A", FP_A)
    device_info_answers(fake, "R58A")

    enrolled = await client.call_tool(
        "enroll_device", {"adb_serial": "R58A", "device_id": "val-042", "label": "Valideur quai 3"}
    )

    assert not enrolled.is_error, text(enrolled)
    assert data(enrolled) == {
        "device_id": "VAL-042",
        "label": "Valideur quai 3",
        "fingerprint": FP_A.to_dict(),
        "enrolled_at": NOW,
        "last_adb_serial": "R58A",
    }
    assert ctx.registry.get("VAL-042").fingerprint == FP_A
    write = ["sh", "-c", shlex.quote(f"echo VAL-042 > {shlex.quote(MARKER_PATH)}")]
    assert ["adb", "-s", "R58A", "shell", *write] in fake.calls

    info = await client.call_tool("device_info", {"device_id": "val-042"})

    assert not info.is_error, text(info)
    assert data(info) == {
        "device_id": "VAL-042",
        "label": "Valideur quai 3",
        "adb_serial": "R58A",
        "fingerprint": FP_A.to_dict(),
        "android_version": "14",
        "battery_level": 87,
        "screen_size": "1080x2400",
    }


async def test_device_info_prefers_override_screen_size_and_tolerates_missing_battery(
    client, ctx, bench, fake
):
    ctx.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.add("R58A", FP_A, marker="VAL-042")
    fake.on(["shell", "getprop", "ro.build.version.release"], stdout="11\n", serial="R58A")
    fake.on(["shell", "dumpsys", "battery"], stdout="Can't find service: battery\n", serial="R58A")
    fake.on(
        ["shell", "wm", "size"],
        stdout="Physical size: 1080x2400\nOverride size: 720x1600\n",
        serial="R58A",
    )

    info = data(await client.call_tool("device_info", {"device_id": "VAL-042"}))

    assert info["battery_level"] is None
    assert info["screen_size"] == "720x1600"
    assert info["android_version"] == "11"


async def test_unknown_device_is_tool_error(client, bench):
    bench.add("R58A", FP_A)
    result = await client.call_tool("device_info", {"device_id": "NOPE"})
    assert result.is_error
    assert "non enrôlé" in text(result)


async def test_invalid_device_id_is_tool_error(client, bench):
    result = await client.call_tool("device_info", {"device_id": "bad id!"})
    assert result.is_error
    assert "ID d'équipement invalide" in text(result)


async def test_unauthorized_device_gives_rsa_message(client, ctx, bench):
    ctx.registry.enroll("VAL-042", FP_A, "", NOW)
    ctx.registry.remember_serial("VAL-042", "R58A")
    bench.add("R58A", FP_A, state="unauthorized")
    result = await client.call_tool("device_info", {"device_id": "VAL-042"})
    assert result.is_error
    assert "clé RSA" in text(result)


async def test_enroll_conflict_is_tool_error(client, ctx, bench):
    ctx.registry.enroll("VAL-001", FP_A, "", NOW)
    bench.add("R58A", FP_A)
    result = await client.call_tool("enroll_device", {"adb_serial": "R58A", "device_id": "VAL-002"})
    assert result.is_error
    assert "déjà enrôlé sous l'ID VAL-001" in text(result)


async def test_connect_wifi_calls_adb_connect(client, fake):
    fake.on(["connect", "192.168.1.50:5555"], stdout="connected to 192.168.1.50:5555\n")
    result = await client.call_tool("connect_wifi", {"host": "192.168.1.50"})
    assert not result.is_error, text(result)
    assert "connected to 192.168.1.50:5555" in text(result)
    assert ["adb", "connect", "192.168.1.50:5555"] in fake.calls


@pytest.mark.parametrize(
    ("host", "port"),
    [("192.168.1.50", 0), ("192.168.1.50", 70000), ("", 5555), ("bad host", 5555), ("10.0.0.2:5555", 5555)],
)
async def test_connect_wifi_rejects_bad_address_without_adb_call(client, fake, host, port):
    result = await client.call_tool("connect_wifi", {"host": host, "port": port})
    assert result.is_error
    assert fake.calls == []


async def test_tool_call_is_audited(client, ctx, bench):
    bench.add("R58A", FP_A)
    await client.call_tool("list_devices", {})
    await client.call_tool("device_info", {"device_id": "NOPE"})

    ok, failed = audit_entries(ctx)
    assert ok["tool"] == "list_devices"
    assert ok["args"] == {}
    assert ok["ok"] is True
    assert ok["error"] is None
    assert isinstance(ok["duration_ms"], int) and ok["duration_ms"] >= 0
    assert failed["tool"] == "device_info"
    assert failed["args"] == {"device_id": "NOPE"}
    assert failed["ok"] is False
    assert "non enrôlé" in failed["error"]


async def test_audited_converts_known_errors_and_keeps_signature(ctx):
    async def boom(device_id: str, count: int = 2) -> str:
        """Doc kept."""
        raise AdbError(f"panne {device_id} {count}")

    wrapped = audited(ctx, boom)
    assert wrapped.__name__ == "boom"
    assert wrapped.__doc__ == "Doc kept."
    with pytest.raises(ToolError, match="panne VAL-1 2"):
        await wrapped("VAL-1")
    [entry] = audit_entries(ctx)
    assert entry == {**entry, "tool": "boom", "args": {"device_id": "VAL-1", "count": 2}, "ok": False}


async def test_audited_records_unexpected_errors_and_reraises(ctx):
    async def crash() -> str:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await audited(ctx, crash)()
    [entry] = audit_entries(ctx)
    assert entry["ok"] is False
    assert "bug" in entry["error"]


async def test_device_call_serializes_same_device(ctx, bench):
    ctx.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.add("R58A", FP_A, marker="VAL-042")
    active = 0
    overlaps = 0
    targets = []

    async def op(target):
        nonlocal active, overlaps
        active += 1
        overlaps += active > 1
        await asyncio.sleep(0.01)
        targets.append(target)
        active -= 1
        return target

    results = await asyncio.gather(device_call(ctx, "VAL-042", op), device_call(ctx, "val-042", op))

    assert results == ["R58A", "R58A"]
    assert overlaps == 0
