import shlex
from pathlib import Path

import pytest
from conftest import NOW

from ia_gate.adb import AdbTimeout
from ia_gate.config import DEFAULT_SHELL_ALLOWLIST
from ia_gate.identity import Fingerprint
from ia_gate.tools.testing import InstrumentResult, check_shell_command, parse_instrument_output

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
