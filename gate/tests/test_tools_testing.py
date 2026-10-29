import asyncio
import json
import shlex
from pathlib import Path

import pytest
from conftest import NOW

from ia_gate.adb import AdbTimeout
from ia_gate.config import DEFAULT_SHELL_ALLOWLIST
from ia_gate.identity import Fingerprint
from ia_gate.tools.testing import (
    MAX_FAILURES,
    STACK_MAX_CHARS,
    InstrumentResult,
    check_shell_command,
    instrumentation_targets,
    parse_instrument_output,
)

DATA = Path(__file__).parent / "data"
FP_A = Fingerprint("R58M12AAAAA", "aaaa1111", "samsung", "SM-A505F")
TEST_PKG = "com.example.test"
RUNNER = "androidx.test.runner.AndroidJUnitRunner"

CRASH_OUTPUT = """\
INSTRUMENTATION_STATUS: class=com.example.LoginTest
INSTRUMENTATION_STATUS: current=1
INSTRUMENTATION_STATUS: id=AndroidJUnitRunner
INSTRUMENTATION_STATUS: numtests=3
INSTRUMENTATION_STATUS: stream=
com.example.LoginTest:
INSTRUMENTATION_STATUS: test=goodPassword
INSTRUMENTATION_STATUS_CODE: 1
INSTRUMENTATION_RESULT: shortMsg=Process crashed.
INSTRUMENTATION_CODE: 0
"""


def text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def data(result):
    structured = result.structured_content
    assert structured is not None, text(result)
    return structured["result"] if set(structured) == {"result"} else structured


@pytest.fixture
def device(ctx, bench):
    ctx.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.add("R58A", FP_A, marker="VAL-042")
    return "R58A"


def instrument_calls(fake):
    return [call for call in fake.calls if "instrument" in call]


# --- parse_instrument_output -------------------------------------------------------------------


def test_parse_instrument_ok():
    result = parse_instrument_output((DATA / "instrument_ok.txt").read_text())

    assert isinstance(result, InstrumentResult)
    assert result.passed == 3
    assert result.failed == 0
    assert result.failures == []
    assert result.completed is True
    assert result.raw_tail.endswith("INSTRUMENTATION_CODE: -1\n")


def test_parse_instrument_failure():
    result = parse_instrument_output((DATA / "instrument_fail.txt").read_text())

    assert result.passed == 2
    assert result.failed == 1
    assert result.completed is True
    assert len(result.failures) == 1
    assert result.failures[0]["test"] == "com.example.LoginTest#badPassword"
    stack = result.failures[0]["stack"]
    assert stack
    assert stack.startswith("java.lang.AssertionError: expected:<true> but was:<false>")
    assert "LoginTest.java:42" in stack
    assert "INSTRUMENTATION_" not in stack


def test_parse_instrument_error_code_counts_as_failure():
    output = (
        "INSTRUMENTATION_STATUS: class=com.example.A\n"
        "INSTRUMENTATION_STATUS: test=t\n"
        "INSTRUMENTATION_STATUS_CODE: 1\n"
        "INSTRUMENTATION_STATUS: class=com.example.A\n"
        "INSTRUMENTATION_STATUS: stack=java.lang.NullPointerException\n"
        "\tat com.example.A.t(A.java:3)\n"
        "INSTRUMENTATION_STATUS: test=t\n"
        "INSTRUMENTATION_STATUS_CODE: -1\n"
        "INSTRUMENTATION_CODE: -1\n"
    )

    result = parse_instrument_output(output)

    assert result.failed == 1
    assert result.failures == [
        {"test": "com.example.A#t", "stack": "java.lang.NullPointerException\n\tat com.example.A.t(A.java:3)"}
    ]


def test_parse_instrument_crash_not_completed():
    result = parse_instrument_output(CRASH_OUTPUT)

    assert result.completed is False
    assert result.passed == 0
    assert result.failed == 0
    assert "Process crashed." in result.raw_tail


def test_parse_instrument_raw_tail_is_last_4000_chars():
    output = (DATA / "instrument_fail.txt").read_text() * 10
    result = parse_instrument_output(output)

    assert len(output) > 4000
    assert result.raw_tail == output[-4000:]


def test_instrument_result_to_dict():
    result = parse_instrument_output((DATA / "instrument_fail.txt").read_text())

    as_dict = result.to_dict()

    assert as_dict["passed"] == 2
    assert as_dict["failed"] == 1
    assert as_dict["completed"] is True
    assert as_dict["failures"][0]["test"] == "com.example.LoginTest#badPassword"
    assert set(as_dict) == {"passed", "failed", "failures", "completed", "raw_tail"}


def failing_suite(count: int, frames: int = 150) -> str:
    """`count` failing tests, each with a long stack trace (e.g. a crash in `@Before`)."""
    blocks = []
    for i in range(count):
        stack = "java.lang.SecurityException: Permission denial\n" + "".join(
            f"\tat com.example.deep.Frame{j}.call(Frame{j}.java:{j})\n" for j in range(frames)
        )
        blocks.append(
            f"INSTRUMENTATION_STATUS: class=com.example.T{i}\n"
            f"INSTRUMENTATION_STATUS: test=t\n"
            "INSTRUMENTATION_STATUS_CODE: 1\n"
            f"INSTRUMENTATION_STATUS: class=com.example.T{i}\n"
            f"INSTRUMENTATION_STATUS: stack={stack}"
            f"INSTRUMENTATION_STATUS: test=t\n"
            "INSTRUMENTATION_STATUS_CODE: -2\n"
        )
    return "".join(blocks) + "INSTRUMENTATION_CODE: -1\n"


def test_parse_instrument_bounds_failures_and_stacks():
    result = parse_instrument_output(failing_suite(500))

    assert result.failed == 500
    assert len(result.failures) == MAX_FAILURES
    assert result.failures[0]["test"] == "com.example.T0#t"
    for failure in result.failures:
        assert failure["stack"].startswith("java.lang.SecurityException: Permission denial")
        assert len(failure["stack"]) <= STACK_MAX_CHARS + 100
        assert "caractères tronqués" in failure["stack"]
    assert result.completed is True


def test_parse_instrument_short_stack_kept_whole():
    result = parse_instrument_output(failing_suite(1, frames=3))

    assert "tronqués" not in result.failures[0]["stack"]
    assert result.failures[0]["stack"].endswith("Frame2.java:2)")


# --- run_instrumented_tests --------------------------------------------------------------------


async def test_run_tests_returns_structured_result(client, fake, device):
    fake.on(["shell", "am", "instrument"], stdout=(DATA / "instrument_fail.txt").read_text(), serial=device)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "val-042", "test_package": TEST_PKG}
    )

    assert not result.is_error, text(result)
    out = data(result)
    assert out["device_id"] == "VAL-042"
    assert out["passed"] == 2
    assert out["failed"] == 1
    assert out["completed"] is True
    assert instrument_calls(fake) == [
        ["adb", "-s", device, "shell", "am", "instrument", "-w", "-r", f"{TEST_PKG}/{RUNNER}"]
    ]


async def test_run_tests_passes_class_filter(client, fake, device):
    fake.on(["shell", "am", "instrument"], stdout=(DATA / "instrument_ok.txt").read_text(), serial=device)

    result = await client.call_tool(
        "run_instrumented_tests",
        {"device_id": "VAL-042", "test_package": TEST_PKG, "test_class": "com.example.LoginTest"},
    )

    assert not result.is_error, text(result)
    [call] = instrument_calls(fake)
    args = call[4:]
    index = args.index("-e")
    assert args[index : index + 3] == ["-e", "class", "com.example.LoginTest"]
    assert args[-1] == f"{TEST_PKG}/{RUNNER}"


async def test_run_tests_uses_timeout(client, fake, device):
    fake.on(["shell", "am", "instrument"], stdout=(DATA / "instrument_ok.txt").read_text(), serial=device)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG, "timeout_s": 60}
    )

    assert not result.is_error, text(result)
    index = fake.calls.index(instrument_calls(fake)[0])
    assert fake.timeouts[index] == 60


async def test_run_tests_timeout_is_actionable(client, bench, fake, device):
    real = bench.fake

    async def timing_out(args, timeout):
        if "instrument" in args:
            raise AdbTimeout("Commande adb expirée")
        return await real(args, timeout)

    bench.fake = timing_out

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG, "timeout_s": 30}
    )

    assert result.is_error
    assert "30 s" in text(result)
    assert "timeout_s" in text(result)
    assert "VAL-042" in text(result)


async def test_run_tests_many_long_failures_result_is_bounded(client, ctx, fake, device):
    raw = failing_suite(500)
    assert len(raw.encode()) > 10 * ctx.config.max_log_bytes
    fake.on(["shell", "am", "instrument"], stdout=raw, serial=device)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG}
    )

    assert not result.is_error, text(result)[:500]
    out = data(result)
    assert out["failed"] == 500
    assert out["completed"] is True
    assert len(out["failures"]) == MAX_FAILURES
    assert len(json.dumps(out).encode()) <= ctx.config.max_log_bytes
    assert len(text(result).encode()) <= ctx.config.max_log_bytes


PM_LIST_INSTRUMENTATION = """\
instrumentation:com.other.test/androidx.test.runner.AndroidJUnitRunner (target=com.other)
instrumentation:com.example.test/androidx.test.runner.AndroidJUnitRunner (target=com.example)
"""


def force_stops(fake, serial=None):
    return [
        call[-1]
        for call in fake.calls
        if call[3:6] == ["shell", "am", "force-stop"] and serial in (None, call[2])
    ]


def time_out_instrument(bench, order):
    real = bench.fake

    async def timing_out(args, timeout):
        order.append(list(args))
        if "instrument" in args:
            raise AdbTimeout("Commande adb expirée")
        return await real(args, timeout)

    bench.fake = timing_out


def test_instrumentation_targets_reads_pm_list():
    assert instrumentation_targets(PM_LIST_INSTRUMENTATION, TEST_PKG) == ["com.example"]
    assert instrumentation_targets(PM_LIST_INSTRUMENTATION, "com.absent.test") == []


async def test_run_tests_timeout_force_stops_instrumentation(client, bench, fake, device):
    fake.on(["shell", "pm", "list", "instrumentation"], stdout=PM_LIST_INSTRUMENTATION, serial=device)
    fake.on(["shell", "am", "force-stop"], serial=device)
    order: list[list[str]] = []
    time_out_instrument(bench, order)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG, "timeout_s": 30}
    )

    assert result.is_error
    assert "30 s" in text(result)
    assert "arrêtée par am force-stop" in text(result)
    assert sorted(force_stops(fake)) == ["com.example", TEST_PKG]
    instrument_index = next(i for i, call in enumerate(order) if "instrument" in call and "am" in call)
    stop_indexes = [i for i, call in enumerate(order) if "force-stop" in call]
    assert stop_indexes and min(stop_indexes) > instrument_index


async def test_run_tests_timeout_cleanup_failure_keeps_timeout_error(client, bench, fake, device):
    fake.on(["shell", "pm", "list", "instrumentation"], returncode=1, stderr="boom", serial=device)
    fake.on(["shell", "am", "force-stop"], returncode=1, stderr="boom", serial=device)
    time_out_instrument(bench, [])

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG, "timeout_s": 30}
    )

    assert result.is_error
    assert "expirés après 30 s" in text(result)
    assert "timeout_s" in text(result)
    assert force_stops(fake) == [TEST_PKG]
    # The run may still drive the device: never claim it was stopped.
    assert "arrêtée par am force-stop" not in text(result)
    assert "stop_app" in text(result)
    assert TEST_PKG in text(result)


WIFI = "192.168.1.50:5555"


def reconnect_during_run(bench, fake, new_serial):
    """While `am instrument` runs, the device leaves its USB serial (`new_serial` set: it is now
    reachable there; None: it is gone), then the run times out."""
    real = bench.fake

    async def timing_out(args, timeout):
        if "instrument" in args:
            if new_serial is not None:
                bench.fake = real  # `add` registers its answers on the FakeRunner
                bench.add(new_serial, FP_A, marker="VAL-042")
                bench.fake = timing_out
            fake.on(["devices", "-l"], stdout="List of devices attached\n" + (
                f"{new_serial}\tdevice product:p model:SM-A505F device:d transport_id:9\n"
                if new_serial is not None else ""
            ))
            raise AdbTimeout("Commande adb expirée")
        return await real(args, timeout)

    bench.fake = timing_out


async def test_run_tests_timeout_cleanup_resolves_device_again(client, bench, fake, device):
    for serial in (device, WIFI):
        fake.on(["shell", "pm", "list", "instrumentation"], stdout=PM_LIST_INSTRUMENTATION, serial=serial)
        fake.on(["shell", "am", "force-stop"], serial=serial)
    reconnect_during_run(bench, fake, WIFI)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG, "timeout_s": 30}
    )

    assert result.is_error
    assert force_stops(fake, device) == []
    assert sorted(force_stops(fake, WIFI)) == ["com.example", TEST_PKG]
    assert "arrêtée par am force-stop" in text(result)


async def test_run_tests_timeout_device_gone_skips_cleanup(client, bench, fake, device):
    fake.on(["shell", "pm", "list", "instrumentation"], stdout=PM_LIST_INSTRUMENTATION, serial=device)
    fake.on(["shell", "am", "force-stop"], serial=device)
    reconnect_during_run(bench, fake, None)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG, "timeout_s": 30}
    )

    assert result.is_error
    assert force_stops(fake) == []
    message = text(result)
    assert "expirés après 30 s" in message
    assert "arrêtée par am force-stop" not in message
    assert "stop_app" in message


async def test_run_tests_cancelled_force_stops_instrumentation(server, fake, device):
    fake.on(["shell", "am", "instrument"], stdout="", delay=60, serial=device)
    fake.on(["shell", "pm", "list", "instrumentation"], stdout=PM_LIST_INSTRUMENTATION, serial=device)
    fake.on(["shell", "am", "force-stop"], serial=device)

    task = asyncio.create_task(
        server.call_tool("run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG})
    )
    for _ in range(200):
        if instrument_calls(fake):
            break
        await asyncio.sleep(0.01)
    assert instrument_calls(fake)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(force_stops(fake)) == ["com.example", TEST_PKG]


async def test_run_tests_missing_instrumentation_is_actionable(client, fake, device):
    fake.on(
        ["shell", "am", "instrument"],
        returncode=1,
        stderr=(
            "java.lang.SecurityException: Unable to find instrumentation info for: "
            f"ComponentInfo{{{TEST_PKG}/{RUNNER}}}\n"
        ),
        serial=device,
    )

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG}
    )

    assert result.is_error
    assert "install_apk" in text(result)
    assert "pm list instrumentation" in text(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {"test_package": "com.x; reboot"},
        {"test_package": TEST_PKG, "runner": "androidx.Runner; reboot"},
        {"test_package": TEST_PKG, "test_class": "com.example.LoginTest && reboot"},
        {"test_package": TEST_PKG, "test_class": "-e debug true"},
        {"test_package": TEST_PKG, "timeout_s": 0},
    ],
)
async def test_run_tests_invalid_arguments_refused(client, fake, device, arguments):
    result = await client.call_tool("run_instrumented_tests", {"device_id": "VAL-042", **arguments})

    assert result.is_error
    assert fake.calls == []


async def test_run_tests_accepts_method_filter(client, fake, device):
    fake.on(["shell", "am", "instrument"], stdout=(DATA / "instrument_ok.txt").read_text(), serial=device)

    result = await client.call_tool(
        "run_instrumented_tests",
        {"device_id": "VAL-042", "test_package": TEST_PKG, "test_class": "com.example.LoginTest#badPassword"},
    )

    assert not result.is_error, text(result)
    [call] = instrument_calls(fake)
    # `#` starts a comment in the device shell: the argument must reach it quoted.
    assert shlex.quote("com.example.LoginTest#badPassword") in call
    assert shlex.split(" ".join(call[4:]))[-2] == "com.example.LoginTest#badPassword"


# --- shell --------------------------------------------------------------------------------------


def test_check_shell_command_returns_tokens():
    assert check_shell_command("pm list packages -3", DEFAULT_SHELL_ALLOWLIST) == [
        "pm",
        "list",
        "packages",
        "-3",
    ]
    assert check_shell_command("getprop 'ro.product.model'", DEFAULT_SHELL_ALLOWLIST) == [
        "getprop",
        "ro.product.model",
    ]


def test_check_shell_command_blank_allowlist_entry_allows_nothing():
    with pytest.raises(ValueError):
        check_shell_command("reboot", ["", "  ", "getprop"])


async def test_shell_allowlisted_ok(client, fake, device):
    fake.on(["shell", "pm", "list", "packages"], stdout="package:com.example.app\n", serial=device)

    result = await client.call_tool("shell", {"device_id": "VAL-042", "command": "pm list packages"})

    assert not result.is_error, text(result)
    assert "package:com.example.app" in text(result)
    assert ["adb", "-s", device, "shell", "pm", "list", "packages"] in fake.calls


async def test_shell_output_truncated(client, ctx, fake, device):
    lines = [f"line {i:06d} " + "z" * 80 for i in range(4000)]
    raw = "\n".join(lines) + "\n"
    assert len(raw.encode()) > ctx.config.max_log_bytes
    fake.on(["shell", "dumpsys"], stdout=raw, serial=device)

    result = await client.call_tool("shell", {"device_id": "VAL-042", "command": "dumpsys activity"})

    assert not result.is_error, text(result)
    out = data(result)
    assert out.startswith("[… ")
    assert "octets tronqués" in out.split("\n", 1)[0]
    assert out.endswith(lines[-1] + "\n")


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /sdcard",
        "getprop; reboot",
        "ls && reboot",
        "ls | sh",
        "echo $(id)",
        "ls\nreboot",
        "pmlist",
        "ls > /sdcard/x",
        "ls `id`",
        "ls\rreboot",
        "",
        "   ",
        "getprop 'unbalanced",
        # First word allowlisted, but not the whole entry: the allowlist matches on every word.
        "pm uninstall com.example.app",
        "settings put global adb_enabled 0",
        "wm density 50",
        "pm",
        "ip link set wlan0 down",
    ],
)
async def test_shell_rejected(client, fake, device, command):
    with pytest.raises(ValueError):
        check_shell_command(command, DEFAULT_SHELL_ALLOWLIST)

    result = await client.call_tool("shell", {"device_id": "VAL-042", "command": command})

    assert result.is_error
    assert fake.calls == []


async def test_shell_failure_output_is_bounded(client, ctx, fake, device):
    # `ls -R /sdcard` exits 1 on Android 11+ (Android/data is denied) but prints the whole listing.
    lines = [f"/sdcard/DCIM/file_{i:06d}.jpg " + "z" * 80 for i in range(4000)]
    raw = "\n".join(lines) + "\n"
    assert len(raw.encode()) > ctx.config.max_log_bytes
    fake.on(["shell", "ls"], stdout=raw, stderr="ls: /sdcard/Android/data: Permission denied\n",
            returncode=1, serial=device)

    result = await client.call_tool("shell", {"device_id": "VAL-042", "command": "ls -R /sdcard"})

    assert result.is_error
    message = text(result)
    assert len(message.encode()) <= ctx.config.max_log_bytes + 500
    assert "(code 1)" in message
    assert "octets tronqués" in message
    assert lines[-1] in message


async def test_run_tests_partial_output_on_adb_failure_is_parsed(client, ctx, fake, device):
    # USB unplugged mid-run: adb exits non-zero after a long partial status stream.
    block = (
        "INSTRUMENTATION_STATUS: class=com.example.LoginTest\n"
        "INSTRUMENTATION_STATUS: test=t{i}\n"
        "INSTRUMENTATION_STATUS_CODE: 1\n"
        "INSTRUMENTATION_STATUS: class=com.example.LoginTest\n"
        "INSTRUMENTATION_STATUS: test=t{i}\n"
        "INSTRUMENTATION_STATUS_CODE: 0\n"
    )
    raw = "".join(block.format(i=i) for i in range(3000))
    assert len(raw.encode()) > ctx.config.max_log_bytes
    fake.on(["shell", "am", "instrument"], stdout=raw, stderr="error: device offline\n",
            returncode=1, serial=device)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG}
    )

    assert not result.is_error, text(result)[:500]
    out = data(result)
    assert out["passed"] == 3000
    assert out["completed"] is False
    assert len(text(result).encode()) <= ctx.config.max_log_bytes


async def test_run_tests_adb_failure_without_status_is_bounded(client, ctx, fake, device):
    junk = "\n".join(f"noise {i:06d} " + "y" * 80 for i in range(4000)) + "\n"
    fake.on(["shell", "am", "instrument"], stdout=junk, returncode=1, serial=device)

    result = await client.call_tool(
        "run_instrumented_tests", {"device_id": "VAL-042", "test_package": TEST_PKG}
    )

    assert result.is_error
    message = text(result)
    assert len(message.encode()) <= ctx.config.max_log_bytes + 500
    assert "(code 1)" in message


async def test_shell_rejection_lists_allowed_commands(client, fake, device):
    result = await client.call_tool("shell", {"device_id": "VAL-042", "command": "rm -rf /sdcard"})

    assert result.is_error
    assert "pm list" in text(result)
    assert "shell_allowlist" in text(result)


async def test_shell_unknown_device_is_tool_error(client, device):
    result = await client.call_tool("shell", {"device_id": "NOPE", "command": "getprop"})

    assert result.is_error
    assert "non enrôlé" in text(result)
