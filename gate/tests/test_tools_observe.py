import base64
import shlex

import pytest
from conftest import NOW

from ia_gate.identity import Fingerprint
from ia_gate.tools.observe import truncate_tail

FP_A = Fingerprint("R58M12AAAAA", "aaaa1111", "samsung", "SM-A505F")
PKG = "com.example.app"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
UI_PATH = "/data/local/tmp/ia-gate-ui.xml"


def text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def data(result):
    structured = result.structured_content
    assert structured is not None, text(result)
    return structured["result"] if set(structured) == {"result"} else structured


def notice_of(result: str) -> str:
    return result.split("\n", 1)[0] + "\n"


@pytest.fixture
def device(ctx, bench):
    ctx.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.add("R58A", FP_A, marker="VAL-042")
    return "R58A"


def logcat_calls(fake):
    return [call for call in fake.calls if "logcat" in call]


def adb_tool_calls(fake, word):
    return [call for call in fake.calls if word in call]


# --- truncate_tail ---------------------------------------------------------------------------


def test_truncate_tail_keeps_recent_lines_with_notice():
    lines = [f"10-29 10:00:00.000  1234  1234 I Tag: line {i:06d} " + "x" * 40 for i in range(5000)]
    raw = "\n".join(lines) + "\n"
    assert len(raw.encode()) > 300_000

    result = truncate_tail(raw, 200_000)

    notice = notice_of(result)
    assert notice.startswith("[… ") and notice.endswith(" octets tronqués …]\n")
    assert result.startswith(notice)
    assert len(result.encode()) <= 200_000 + len(notice.encode())
    assert result.endswith(lines[-1] + "\n")
    body = result[len(notice) :]
    # Starts on a line boundary: the first kept line is a whole input line.
    assert body.split("\n", 1)[0] in lines
    dropped = len(raw.encode()) - len(body.encode())
    assert notice == f"[… {dropped} octets tronqués …]\n"


def test_truncate_tail_noop_when_small():
    raw = "a\nb\nc\n"
    assert truncate_tail(raw, 200_000) == raw
    assert truncate_tail(raw, len(raw.encode())) == raw


def test_truncate_tail_cut_on_line_start_keeps_that_line():
    raw = "aaaa\nbbbb\ncccc\n"
    result = truncate_tail(raw, 10)
    assert result == "[… 5 octets tronqués …]\nbbbb\ncccc\n"


def test_truncate_tail_multibyte_never_splits_characters():
    raw = "é" * 50 + "\n" + "à" * 50 + "\n"
    result = truncate_tail(raw, 101)
    assert result.endswith("à" * 50 + "\n")
    assert "�" not in result


def test_truncate_tail_single_huge_line_keeps_tail():
    raw = "x" * 1000
    result = truncate_tail(raw, 100)
    notice = notice_of(result)
    assert result == notice + "x" * 100
    assert notice == "[… 900 octets tronqués …]\n"


# --- get_logs / clear_logs ---------------------------------------------------------------------


async def test_get_logs_filters_by_pid(client, fake, device):
    fake.on(["shell", "pidof", PKG], stdout="4242\n", serial=device)
    fake.on(["shell", "logcat"], stdout="10-29 10:00:00.000  4242  4242 I App: hello\n", serial=device)

    result = await client.call_tool("get_logs", {"device_id": "val-042", "package": PKG})

    assert not result.is_error, text(result)
    assert "App: hello" in text(result)
    assert logcat_calls(fake) == [
        ["adb", "-s", device, "shell", "logcat", "-d", "-v", "threadtime", "-t", "500", "--pid=4242"]
    ]


async def test_get_logs_without_package_has_no_pid_filter(client, fake, device):
    fake.on(["shell", "logcat"], stdout="line\n", serial=device)

    result = await client.call_tool("get_logs", {"device_id": "VAL-042", "lines": 50})

    assert not result.is_error, text(result)
    assert logcat_calls(fake) == [
        ["adb", "-s", device, "shell", "logcat", "-d", "-v", "threadtime", "-t", "50"]
    ]
    assert not adb_tool_calls(fake, "pidof")


@pytest.mark.parametrize(("lines", "sent"), [(0, "1"), (-5, "1"), (10_000, "10000"), (99_999, "10000")])
async def test_get_logs_clamps_lines(client, fake, device, lines, sent):
    fake.on(["shell", "logcat"], stdout="line\n", serial=device)

    result = await client.call_tool("get_logs", {"device_id": "VAL-042", "lines": lines})

    assert not result.is_error, text(result)
    [call] = logcat_calls(fake)
    assert call[-2:] == ["-t", sent]


async def test_get_logs_package_not_running(client, fake, device):
    fake.on(["shell", "pidof", PKG], returncode=1, serial=device)

    result = await client.call_tool("get_logs", {"device_id": "VAL-042", "package": PKG})

    assert result.is_error
    assert PKG in text(result)
    assert "ne tourne pas" in text(result)
    assert "launch_app" in text(result)
    assert logcat_calls(fake) == []


async def test_get_logs_invalid_package_refused(client, fake, device):
    result = await client.call_tool("get_logs", {"device_id": "VAL-042", "package": "com.x; reboot"})

    assert result.is_error
    assert "Nom de package invalide" in text(result)
    assert fake.calls == []


async def test_get_logs_truncated_to_max_log_bytes(client, ctx, fake, device):
    lines = [f"10-29 10:00:00.000  1  1 I Tag: line {i:06d} " + "y" * 60 for i in range(4000)]
    raw = "\n".join(lines) + "\n"
    assert len(raw.encode()) > ctx.config.max_log_bytes
    fake.on(["shell", "logcat"], stdout=raw, serial=device)

    result = await client.call_tool("get_logs", {"device_id": "VAL-042", "lines": 10_000})

    assert not result.is_error, text(result)
    out = data(result)
    notice = notice_of(out)
    assert "octets tronqués" in notice
    assert len(out.encode()) <= ctx.config.max_log_bytes + len(notice.encode())
    assert out.endswith(lines[-1] + "\n")


async def test_clear_logs(client, fake, device):
    fake.on(["shell", "logcat", "-c"], serial=device)

    result = await client.call_tool("clear_logs", {"device_id": "VAL-042"})

    assert not result.is_error, text(result)
    assert "VAL-042" in text(result)
    assert ["adb", "-s", device, "shell", "logcat", "-c"] in fake.calls


# --- screenshot ---------------------------------------------------------------------------------


async def test_screenshot_returns_png_image(client, fake, device):
    fake.on(["exec-out", "screencap", "-p"], stdout=PNG, serial=device)

    result = await client.call_tool("screenshot", {"device_id": "VAL-042"})

    assert not result.is_error, text(result)
    images = [block for block in result.content if block.type == "image"]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert base64.b64decode(images[0].data) == PNG
    assert ["adb", "-s", device, "exec-out", "screencap", "-p"] in fake.calls


async def test_screenshot_rejects_non_png(client, fake, device):
    fake.on(["exec-out", "screencap", "-p"], stdout=b"screencap: permission denied\n", serial=device)

    result = await client.call_tool("screenshot", {"device_id": "VAL-042"})

    assert result.is_error
    assert "PNG" in text(result)
    assert "VAL-042" in text(result)


# --- ui_dump ------------------------------------------------------------------------------------


async def test_ui_dump_returns_xml(client, fake, device):
    xml = '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0"><node text="OK"/></hierarchy>'
    fake.on(["shell", "uiautomator", "dump"], stdout=f"UI hierchary dumped to: {UI_PATH}\n", serial=device)
    fake.on(["shell", "cat", UI_PATH], stdout=xml, serial=device)

    result = await client.call_tool("ui_dump", {"device_id": "VAL-042"})

    assert not result.is_error, text(result)
    assert data(result) == xml
    dump_index = fake.calls.index(["adb", "-s", device, "shell", "uiautomator", "dump", UI_PATH])
    cat_index = fake.calls.index(["adb", "-s", device, "shell", "cat", UI_PATH])
    assert dump_index < cat_index


async def test_ui_dump_error_is_reported(client, fake, device):
    fake.on(["shell", "uiautomator", "dump"], stdout="ERROR: could not get idle state.\n", serial=device)

    result = await client.call_tool("ui_dump", {"device_id": "VAL-042"})

    assert result.is_error
    assert "could not get idle state" in text(result)
    assert ["adb", "-s", device, "shell", "cat", UI_PATH] not in fake.calls


# --- ui_tap / ui_text / ui_key ----------------------------------------------------------------


async def test_ui_tap(client, fake, device):
    fake.on(["shell", "input", "tap"], serial=device)

    result = await client.call_tool("ui_tap", {"device_id": "VAL-042", "x": 100, "y": 250})

    assert not result.is_error, text(result)
    assert ["adb", "-s", device, "shell", "input", "tap", "100", "250"] in fake.calls


@pytest.mark.parametrize(("x", "y"), [(-1, 10), (10, -1)])
async def test_ui_tap_negative_refused(client, fake, device, x, y):
    result = await client.call_tool("ui_tap", {"device_id": "VAL-042", "x": x, "y": y})

    assert result.is_error
    assert fake.calls == []


async def test_ui_text_encodes_spaces_and_quotes(client, fake, device):
    fake.on(["shell", "input", "text"], serial=device)
    typed = 'hello world it\'s "me"'

    result = await client.call_tool("ui_text", {"device_id": "VAL-042", "text": typed})

    assert not result.is_error, text(result)
    [call] = adb_tool_calls(fake, "text")
    assert call[:6] == ["adb", "-s", device, "shell", "input", "text"]
    [sent] = call[6:]
    assert "hello%sworld" in sent
    # The device shell un-quotes the argument back to exactly the encoded text.
    assert shlex.split(sent) == ['hello%sworld%sit\'s%s"me"']


async def test_ui_text_too_long_refused(client, fake, device):
    result = await client.call_tool("ui_text", {"device_id": "VAL-042", "text": "a" * 501})

    assert result.is_error
    assert "500" in text(result)
    assert fake.calls == []


async def test_ui_text_max_length_accepted(client, fake, device):
    fake.on(["shell", "input", "text"], serial=device)

    result = await client.call_tool("ui_text", {"device_id": "VAL-042", "text": "a" * 500})

    assert not result.is_error, text(result)


@pytest.mark.parametrize("typed", ["", "line1\nline2", "tab\there"])
async def test_ui_text_empty_or_control_chars_refused(client, fake, device, typed):
    result = await client.call_tool("ui_text", {"device_id": "VAL-042", "text": typed})

    assert result.is_error
    assert fake.calls == []


@pytest.mark.parametrize("key", ["KEYCODE_HOME", "KEYCODE_DPAD_UP", "3", "187"])
async def test_ui_key_ok(client, fake, device, key):
    fake.on(["shell", "input", "keyevent"], serial=device)

    result = await client.call_tool("ui_key", {"device_id": "VAL-042", "key": key})

    assert not result.is_error, text(result)
    assert ["adb", "-s", device, "shell", "input", "keyevent", key] in fake.calls


@pytest.mark.parametrize(
    "key", ["3; reboot", "KEYCODE_HOME && reboot", "1234", "keycode_home", "3\n", "KEYCODE_", "٣"]
)
async def test_ui_key_rejects_injection(client, fake, device, key):
    result = await client.call_tool("ui_key", {"device_id": "VAL-042", "key": key})

    assert result.is_error
    assert "KEYCODE_" in text(result)
    assert fake.calls == []


async def test_unknown_device_is_tool_error(client, device):
    result = await client.call_tool("ui_tap", {"device_id": "NOPE", "x": 1, "y": 1})

    assert result.is_error
    assert "non enrôlé" in text(result)
