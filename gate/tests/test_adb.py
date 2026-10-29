import asyncio
from pathlib import Path

import pytest
from fakes import FakeRunner

from ia_gate.adb import (
    AdbClient,
    AdbDevice,
    AdbError,
    AdbNotFound,
    AdbResult,
    AdbTimeout,
    InstallError,
    TransportId,
    _parse_devices,
    duplicate_serials,
    subprocess_runner,
)

DEVICES_OUTPUT = """List of devices attached
R58M12ABCDE            device usb:1-1 product:x model:SM_T505 device:gta4 transport_id:1
192.168.1.50:5555      device product:y model:VAL_200 device:v transport_id:2
emulator-5554          unauthorized transport_id:3

"""


@pytest.fixture
def fake():
    return FakeRunner()


@pytest.fixture
def adb(fake):
    return AdbClient(fake)


def test_adb_result_text_decodes_with_replacement():
    assert AdbResult(0, b"ok \xff\n", b"").text == "ok �\n"


async def test_devices_parses_usb_wifi_unauthorized(fake, adb):
    fake.on(["devices", "-l"], stdout=DEVICES_OUTPUT)
    assert await adb.devices() == [
        AdbDevice("R58M12ABCDE", "device", "SM T505", 1),
        AdbDevice("192.168.1.50:5555", "device", "VAL 200", 2),
        AdbDevice("emulator-5554", "unauthorized", None, 3),
    ]
    assert fake.calls == [["adb", "devices", "-l"]]


def test_parse_devices_keeps_distinct_transport_ids_for_shared_serial():
    devices = _parse_devices(
        "List of devices attached\n"
        "0123456789ABCDEF       device usb:1-1 product:t model:TAB_X device:t transport_id:5\n"
        "0123456789ABCDEF       device usb:1-2 product:t model:TAB_X device:t transport_id:6\n"
    )
    assert [d.serial for d in devices] == ["0123456789ABCDEF", "0123456789ABCDEF"]
    assert [d.transport_id for d in devices] == [5, 6]
    assert devices[0] != devices[1]


def test_parse_devices_without_transport_id():
    assert _parse_devices("List of devices attached\nABC device\nDEF device transport_id:x\n") == [
        AdbDevice("ABC", "device", None),
        AdbDevice("DEF", "device", None),
    ]


def test_duplicate_serials_and_target():
    a = AdbDevice("0123456789ABCDEF", "device", None, 5)
    b = AdbDevice("0123456789ABCDEF", "device", None, 6)
    c = AdbDevice("R58M12ABCDE", "device", None, 7)
    old = AdbDevice("0123456789ABCDEF", "device", None)
    duplicated = duplicate_serials([a, b, c])
    assert duplicated == {"0123456789ABCDEF"}
    assert a.target(duplicated) == TransportId(5)
    assert b.target(duplicated) == TransportId(6)
    assert c.target(duplicated) == "R58M12ABCDE"
    assert c.target() == "R58M12ABCDE"
    assert old.target(duplicated) == "0123456789ABCDEF"  # no transport id: adb will refuse
    assert str(TransportId(5)) == "transport_id:5"


async def test_commands_address_transport_id_with_dash_t(fake, adb):
    fake.on(["shell", "getprop"], stdout="v\n")
    fake.on(["exec-out", "screencap", "-p"], stdout=b"\x89PNG")
    fake.on(["install"], stdout="Success\n")
    fake.on(["uninstall", "com.example.app"], stdout="Success\n")
    target = TransportId(6)
    assert await adb.getprop(target, "ro.serialno") == "v"
    assert fake.calls[-1] == ["adb", "-t", "6", "shell", "getprop", "ro.serialno"]
    await adb.exec_out(target, ["screencap", "-p"])
    assert fake.calls[-1] == ["adb", "-t", "6", "exec-out", "screencap", "-p"]
    await adb.install(target, Path("/apks/app.apk"))
    assert fake.calls[-1] == ["adb", "-t", "6", "install", "-r", "/apks/app.apk"]
    await adb.uninstall(target, "com.example.app")
    assert fake.calls[-1] == ["adb", "-t", "6", "uninstall", "com.example.app"]


async def test_shell_error_names_transport_id(fake, adb):
    fake.on(["shell", "cat"], returncode=1, stderr="No such file")
    with pytest.raises(AdbError, match="transport_id:6"):
        await adb.shell(TransportId(6), ["cat", "/nope"])


async def test_devices_skips_daemon_messages(fake, adb):
    fake.on(
        ["devices", "-l"],
        stdout="* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
        "List of devices attached\nABC  offline transport_id:4\n",
    )
    assert await adb.devices() == [AdbDevice("ABC", "offline", None, 4)]


async def test_shell_quotes_arguments(fake, adb):
    fake.on(["shell", "input", "text"])
    await adb.shell("S", ["input", "text", "a b;rm"])
    assert fake.calls[-1][:3] == ["adb", "-s", "S"]
    assert fake.calls[-1][-4:] == ["shell", "input", "text", "'a b;rm'"]


async def test_shell_nonzero_raises_with_output(fake, adb):
    fake.on(["shell", "cat"], returncode=1, stderr="cat: /nope: No such file or directory")
    with pytest.raises(AdbError, match="No such file"):
        await adb.shell("S", ["cat", "/nope"])


async def test_exec_out_returns_bytes_and_quotes(fake, adb):
    fake.on(["exec-out", "screencap", "-p"], stdout=b"\x89PNG\r\n")
    assert await adb.exec_out("S", ["screencap", "-p"]) == b"\x89PNG\r\n"
    assert fake.calls[-1] == ["adb", "-s", "S", "exec-out", "screencap", "-p"]


async def test_getprop_strips(fake, adb):
    fake.on(["shell", "getprop", "ro.serialno"], stdout="R58M12ABCDE\r\n")
    assert await adb.getprop("S", "ro.serialno") == "R58M12ABCDE"


async def test_install_failure_code(fake, adb):
    fake.on(
        ["install"],
        stdout="Performing Streamed Install\n"
        "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Package com.x signatures do not match]\n",
        returncode=1,
    )
    with pytest.raises(InstallError) as exc_info:
        await adb.install("S", Path("/apks/app.apk"))
    assert exc_info.value.code == "INSTALL_FAILED_UPDATE_INCOMPATIBLE"
    assert isinstance(exc_info.value, AdbError)


async def test_install_replace_flag(fake, adb):
    fake.on(["install"], stdout="Performing Streamed Install\nSuccess\n")
    await adb.install("S", Path("/apks/app.apk"))
    assert fake.calls[-1] == ["adb", "-s", "S", "install", "-r", "/apks/app.apk"]
    await adb.install("S", Path("/apks/app.apk"), replace=False)
    assert fake.calls[-1] == ["adb", "-s", "S", "install", "/apks/app.apk"]


async def test_uninstall_ok_and_failure(fake, adb):
    fake.on(["uninstall", "com.example.app"], stdout="Success\n")
    await adb.uninstall("S", "com.example.app")
    assert fake.calls[-1] == ["adb", "-s", "S", "uninstall", "com.example.app"]
    fake.on(["uninstall", "com.missing"], stdout="Failure [DELETE_FAILED_INTERNAL_ERROR]\n", returncode=1)
    with pytest.raises(AdbError, match="DELETE_FAILED_INTERNAL_ERROR"):
        await adb.uninstall("S", "com.missing")


async def test_connect_ok_returns_message(fake, adb):
    fake.on(["connect", "192.168.1.50:5555"], stdout="connected to 192.168.1.50:5555\n")
    assert await adb.connect("192.168.1.50", 5555) == "connected to 192.168.1.50:5555"


async def test_connect_failure_raises(fake, adb):
    fake.on(
        ["connect"],
        stdout="failed to connect to '192.168.1.99:5555': Connection refused\n",
    )
    with pytest.raises(AdbError, match="192.168.1.99"):
        await adb.connect("192.168.1.99", 5555)


async def test_custom_adb_path_and_timeout(fake):
    adb = AdbClient(fake, adb_path="/opt/sdk/adb", timeout_s=7)
    fake.on(["shell", "true"])
    await adb.shell("S", ["true"])
    assert fake.calls[-1] == ["/opt/sdk/adb", "-s", "S", "shell", "true"]
    assert fake.timeouts[-1] == 7
    await adb.shell("S", ["true"], timeout=3)
    assert fake.timeouts[-1] == 3


async def test_fake_runner_unmatched_call_names_args(fake, adb):
    with pytest.raises(AssertionError, match="getprop"):
        await adb.getprop("S", "ro.product.model")


async def test_fake_runner_serial_specific_response_wins(fake, adb):
    fake.on(["shell", "getprop", "ro.serialno"], stdout="GENERIC")
    fake.on(["shell", "getprop", "ro.serialno"], stdout="FOR-A", serial="A")
    assert await adb.getprop("A", "ro.serialno") == "FOR-A"
    assert await adb.getprop("B", "ro.serialno") == "GENERIC"


async def test_fake_runner_transport_id_specific_response(fake, adb):
    fake.on(["shell", "getprop", "ro.serialno"], stdout="GENERIC")
    fake.on(["shell", "getprop", "ro.serialno"], stdout="FOR-T5", serial=TransportId(5))
    assert await adb.getprop(TransportId(5), "ro.serialno") == "FOR-T5"
    assert await adb.getprop(TransportId(6), "ro.serialno") == "GENERIC"
    assert await adb.getprop("transport_id:5", "ro.serialno") == "GENERIC"


async def test_subprocess_runner_ok():
    result = await subprocess_runner(["echo", "hello"], 5)
    assert result.returncode == 0 and result.text == "hello\n"


async def test_subprocess_runner_missing_binary():
    with pytest.raises(AdbNotFound, match="adb introuvable"):
        await subprocess_runner(["/nonexistent/adb"], 1)


async def test_subprocess_runner_timeout():
    with pytest.raises(AdbTimeout, match="expirée"):
        await subprocess_runner(["sleep", "5"], 0.1)


async def test_subprocess_runner_kills_process_on_cancel(monkeypatch):
    procs: list[asyncio.subprocess.Process] = []
    real_create = asyncio.create_subprocess_exec

    async def capturing_create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_create)
    task = asyncio.create_task(subprocess_runner(["sleep", "5"], 10))
    while not procs:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs[0].returncode is not None
