import pytest
from fakes import FakeRunner

from ia_gate.adb import AdbClient, AdbError, AdbTimeout, TransportId
from ia_gate.identity import (
    DEVICE_ID_RE,
    MARKER_PATH,
    Fingerprint,
    is_valid_serialno,
    normalize_device_id,
    read_fingerprint,
    read_marker,
    write_marker,
)

WRITE_ARGS = ["shell", "sh", "-c", f"'echo VAL-042 > {MARKER_PATH}'"]
CAT_ARGS = ["shell", "cat", MARKER_PATH]


@pytest.fixture
def fake():
    return FakeRunner()


@pytest.fixture
def adb(fake):
    return AdbClient(fake)


def test_constants():
    assert MARKER_PATH == "/data/local/tmp/.ia-gate-id"
    assert DEVICE_ID_RE.pattern == r"^[A-Z0-9][A-Z0-9_-]{1,31}$"


@pytest.mark.parametrize(
    ("serialno", "ambiguous", "expected"),
    [
        ("", (), False),
        ("unknown", (), False),
        ("0123456789ABCDEF", (), False),
        ("0123456789", (), False),
        ("R58M12ABCDE", (), True),
        ("R58M12ABCDE", {"R58M12ABCDE"}, False),
    ],
)
def test_invalid_serialnos(serialno, ambiguous, expected):
    assert is_valid_serialno(serialno, ambiguous) is expected


def test_invalid_serialnos_variants():
    assert is_valid_serialno("  ") is False
    assert is_valid_serialno("UNKNOWN") is False
    assert is_valid_serialno("0123456789abcdef") is False
    assert is_valid_serialno("R58M12ABCDE", {"OTHER"}) is True


def test_normalize_device_id():
    assert normalize_device_id(" val-042 ") == "VAL-042"
    assert normalize_device_id("tab_1") == "TAB_1"
    for bad in ("-bad", "a", "x" * 33, "VAL 042", "", "VAL/042"):
        with pytest.raises(ValueError, match="A-Z0-9"):
            normalize_device_id(bad)


def test_normalize_device_id_accepts_max_length():
    assert normalize_device_id("x" * 32) == "X" * 32


def test_fingerprint_round_trip():
    fp = Fingerprint("R58M12ABCDE", "a1b2c3", "samsung", "SM-T505")
    assert fp.to_dict() == {
        "serialno": "R58M12ABCDE",
        "android_id": "a1b2c3",
        "manufacturer": "samsung",
        "model": "SM-T505",
    }
    assert Fingerprint.from_dict(fp.to_dict()) == fp


def test_fingerprint_from_dict_rejects_bad_data():
    with pytest.raises(ValueError):
        Fingerprint.from_dict({"serialno": "X"})
    with pytest.raises(ValueError):
        Fingerprint.from_dict({"serialno": 1, "android_id": "", "manufacturer": "", "model": ""})


def _answer_props(fake, serialno="R58M12ABCDE\n", android_id="a1b2c3d4e5f60718\n", serial=None):
    fake.on(["shell", "getprop", "ro.serialno"], stdout=serialno, serial=serial)
    fake.on(["shell", "settings", "get", "secure", "android_id"], stdout=android_id, serial=serial)
    fake.on(["shell", "getprop", "ro.product.manufacturer"], stdout="samsung\n", serial=serial)
    fake.on(["shell", "getprop", "ro.product.model"], stdout="SM-T505\r\n", serial=serial)


async def test_read_fingerprint(fake, adb):
    _answer_props(fake)
    assert await read_fingerprint(adb, "S") == Fingerprint(
        "R58M12ABCDE", "a1b2c3d4e5f60718", "samsung", "SM-T505"
    )
    assert all(call[:3] == ["adb", "-s", "S"] for call in fake.calls)


async def test_read_fingerprint_null_android_id(fake, adb):
    _answer_props(fake, serialno="0123456789ABCDEF\n", android_id="null\n")
    fp = await read_fingerprint(adb, "S")
    assert fp.android_id == ""
    assert fp.serialno == "0123456789ABCDEF"


async def test_read_fingerprint_by_transport_id(fake, adb):
    _answer_props(fake, serial=TransportId(6))
    fp = await read_fingerprint(adb, TransportId(6))
    assert fp.serialno == "R58M12ABCDE"
    assert all(call[:3] == ["adb", "-t", "6"] for call in fake.calls)


async def test_read_marker_absent_returns_none(fake, adb):
    fake.on(CAT_ARGS, returncode=1, stderr=f"cat: {MARKER_PATH}: No such file or directory")
    assert await read_marker(adb, "S") is None


async def test_read_marker_garbage_returns_none(fake, adb):
    fake.on(CAT_ARGS, stdout="hello world\n")
    assert await read_marker(adb, "S") is None


async def test_read_marker_returns_normalized_id(fake, adb):
    fake.on(CAT_ARGS, stdout="val-042\r\n")
    assert await read_marker(adb, "S") == "VAL-042"
    assert fake.calls[-1] == ["adb", "-s", "S", "shell", "cat", MARKER_PATH]


async def test_read_marker_timeout_propagates(adb):
    async def slow_runner(args, timeout):
        raise AdbTimeout("Commande adb expirée après 1 s")

    with pytest.raises(AdbTimeout):
        await read_marker(AdbClient(slow_runner), "S")


async def test_write_marker_verifies(fake, adb):
    fake.on(WRITE_ARGS)
    fake.on(CAT_ARGS, stdout="VAL-042\n")
    await write_marker(adb, "S", "val-042")
    assert fake.calls == [
        ["adb", "-s", "S", *WRITE_ARGS],
        ["adb", "-s", "S", *CAT_ARGS],
    ]


async def test_write_marker_mismatch_raises(fake, adb):
    fake.on(WRITE_ARGS)
    fake.on(CAT_ARGS, stdout="VAL-001\n")
    with pytest.raises(AdbError, match="VAL-042"):
        await write_marker(adb, "S", "VAL-042")


async def test_write_marker_unreadable_raises(fake, adb):
    fake.on(WRITE_ARGS)
    fake.on(CAT_ARGS, returncode=1, stderr="Permission denied")
    with pytest.raises(AdbError, match=MARKER_PATH):
        await write_marker(adb, "S", "VAL-042")


async def test_write_marker_rejects_invalid_id_before_adb(fake, adb):
    with pytest.raises(ValueError):
        await write_marker(adb, "S", "bad id; rm -rf /")
    assert fake.calls == []
