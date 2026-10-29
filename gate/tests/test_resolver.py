import asyncio
import shlex
import time

import pytest
from fakes import FakeRunner

from ia_gate.adb import AdbClient, TransportId
from ia_gate.identity import MARKER_PATH, Fingerprint
from ia_gate.registry import DeviceRegistry, RegistryError
from ia_gate.resolver import IDENTITY_PROBE, DeviceResolver, DeviceStatus, DeviceUnavailable

NOW = "2026-10-29T10:00:00+00:00"
BOGUS = "0123456789ABCDEF"
CAT = ["shell", "cat", MARKER_PATH]
PROBE = ["shell", "sh", "-c", shlex.quote(IDENTITY_PROBE)]
PROPS = ("ro.serialno", "ro.product.manufacturer", "ro.product.model")

FP_A = Fingerprint("R58M12AAAAA", "aaaa1111", "samsung", "SM-A")
FP_B = Fingerprint("R58M12BBBBB", "bbbb2222", "samsung", "SM-B")
FP_C = Fingerprint("R58M12CCCCC", "cccc3333", "samsung", "SM-C")


def usb(serial, tid, model="SM_X"):
    return f"{serial}\tdevice usb:1-1 product:p model:{model} device:d transport_id:{tid}"


def state_line(serial, state, tid):
    return f"{serial}\t{state} usb:1-1 transport_id:{tid}"


def legacy(serial, model="SM_X"):
    """A device line from an old adb that does not report transport ids."""
    return f"{serial}\tdevice product:p model:{model} device:d"


class Bench:
    """A FakeRunner plus a simulated marker file per device (writes are visible to later reads)."""

    def __init__(self, tmp_path):
        self.fake = FakeRunner()
        self.adb = AdbClient(self._run)
        self.registry = DeviceRegistry(tmp_path / "devices.json")
        self.resolver = DeviceResolver(self.adb, self.registry)
        self._fingerprints = {}
        self._boot_ids = {}

    async def _run(self, args, timeout):
        result = await self.fake(args, timeout)
        if args[3:6] == ["shell", "sh", "-c"] and args[6] != PROBE[-1]:
            target = TransportId(int(args[2])) if args[1] == "-t" else args[2]
            marker = args[6].split()[1]
            self.fake.on(CAT, stdout=marker + "\n", serial=target)
            self._probe(target, self._fingerprints[target], marker, self._boot_ids[target])
        return result

    def devices(self, *lines):
        self.fake.on(
            ["devices", "-l"], stdout="List of devices attached\n" + "".join(f"{x}\n" for x in lines)
        )

    def _probe(self, target, fp, marker, boot_id):
        stdout = (
            f"serialno={fp.serialno}\nandroid_id={fp.android_id or 'null'}\n"
            f"boot_id={boot_id}\nmarker={marker or ''}\n"
        )
        self.fake.on(PROBE, stdout=stdout, serial=target)

    def device(self, target, fp, marker=None, boot_id=None):
        """Simulate a device; `boot_id` defaults to one unique to `target` (a distinct device)."""
        if boot_id is None:
            boot_id = f"boot-{target}"
        self._fingerprints[target] = fp
        self._boot_ids[target] = boot_id
        self.fake.on(["shell", "getprop", "ro.serialno"], stdout=fp.serialno + "\n", serial=target)
        self.fake.on(
            ["shell", "settings", "get", "secure", "android_id"],
            stdout=(fp.android_id or "null") + "\n",
            serial=target,
        )
        self.fake.on(["shell", "getprop", "ro.product.manufacturer"], stdout=fp.manufacturer, serial=target)
        self.fake.on(["shell", "getprop", "ro.product.model"], stdout=fp.model + "\r\n", serial=target)
        self.fake.on(["shell", "sh", "-c"], serial=target)
        if marker is None:
            self.fake.on(CAT, returncode=1, stderr="No such file or directory", serial=target)
        else:
            self.fake.on(CAT, stdout=marker + "\n", serial=target)
        self._probe(target, fp, marker, boot_id)

    def shell_calls(self):
        return [call for call in self.fake.calls if "shell" in call]


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path)


async def test_snapshot_lists_enrolled_and_unenrolled(bench):
    bench.registry.enroll("VAL-1", FP_A, "Bench 1", NOW)
    bench.registry.enroll("VAL-2", FP_B, "Bench 2", NOW)
    bench.devices(usb("R58A", 1), usb("R58C", 2))
    bench.device("R58A", FP_A, marker="VAL-1")
    bench.device("R58C", FP_C)
    assert await bench.resolver.snapshot() == [
        DeviceStatus("VAL-1", "R58A", "online", "Bench 1", "SM-A"),
        DeviceStatus("VAL-2", None, "disconnected", "Bench 2", "SM-B"),
        DeviceStatus(None, "R58C", "online", "", "SM-C"),
    ]


async def test_snapshot_reports_unauthorized_unenrolled_device(bench):
    bench.devices(state_line("R58Z", "unauthorized", 4))
    assert await bench.resolver.snapshot() == [DeviceStatus(None, "R58Z", "unauthorized", "", None)]
    assert bench.shell_calls() == []


def probe_calls(bench):
    return [c for c in bench.fake.calls if c[3:] == PROBE or c[5:] == PROBE]


async def test_model_cached_until_connection_leaves_identity_reread_each_scan(bench):
    bench.devices(usb("R58A", 1))
    bench.device("R58A", FP_A)
    await bench.resolver.snapshot()
    await bench.resolver.snapshot()
    model_reads = [c for c in bench.fake.calls if c[-1] == "ro.product.model"]
    assert len(model_reads) == 1
    # serialno, android_id and marker are re-read on every scan, in a single adb call: the
    # connection key (adb serial, transport id) does not prove it is still the same device.
    assert len(probe_calls(bench)) == 2
    assert [c for c in bench.fake.calls if c[-1] == "ro.serialno"] == []

    bench.devices()
    await bench.resolver.snapshot()
    bench.devices(usb("R58A", 1))
    await bench.resolver.snapshot()
    assert len([c for c in bench.fake.calls if c[-1] == "ro.product.model"]) == 2


async def test_cached_model_dropped_when_identity_changes_behind_same_key(bench):
    bench.devices(usb("10.0.0.5:5555", 1))
    bench.device("10.0.0.5:5555", FP_A)
    assert await bench.resolver.snapshot() == [DeviceStatus(None, "10.0.0.5:5555", "online", "", "SM-A")]
    # The DHCP lease goes to another device, and adb restarted: same serial, same transport id.
    bench.device("10.0.0.5:5555", FP_B)
    assert await bench.resolver.snapshot() == [DeviceStatus(None, "10.0.0.5:5555", "online", "", "SM-B")]


async def test_swapped_transport_ids_after_adb_restart_are_not_confused(bench):
    # Same bogus adb serial and ro.serialno; after an adb server restart the transport ids are
    # handed out in the other order while no scan ran in between.
    fp1 = Fingerprint(BOGUS, "aaaa1111", "acme", "VAL")
    fp2 = Fingerprint(BOGUS, "bbbb2222", "acme", "VAL")
    bench.registry.enroll("VAL-1", fp1, "", NOW)
    bench.registry.enroll("VAL-2", fp2, "", NOW)
    bench.devices(usb(BOGUS, 1), usb(BOGUS, 2))
    bench.device(TransportId(1), fp1, marker="VAL-1")
    bench.device(TransportId(2), fp2, marker="VAL-2")
    assert await bench.resolver.resolve("VAL-1") == TransportId(1)
    assert await bench.resolver.resolve("VAL-2") == TransportId(2)

    bench.device(TransportId(1), fp2, marker="VAL-2")
    bench.device(TransportId(2), fp1, marker="VAL-1")
    assert await bench.resolver.resolve("VAL-1") == TransportId(2)
    assert await bench.resolver.resolve("VAL-2") == TransportId(1)


async def test_swapped_devices_without_marker_are_not_confused(bench):
    fp1 = Fingerprint(BOGUS, "aaaa1111", "acme", "VAL")
    fp2 = Fingerprint(BOGUS, "bbbb2222", "acme", "VAL")
    bench.registry.enroll("VAL-1", fp1, "", NOW)
    bench.registry.enroll("VAL-2", fp2, "", NOW)
    bench.devices(usb(BOGUS, 1), usb(BOGUS, 2))
    bench.device(TransportId(1), fp1)
    bench.device(TransportId(2), fp2)
    assert await bench.resolver.resolve("VAL-1") == TransportId(1)

    bench.device(TransportId(1), fp2)
    bench.device(TransportId(2), fp1)
    assert await bench.resolver.resolve("VAL-1") == TransportId(2)


async def test_wifi_address_reused_by_other_device_with_old_adb(bench):
    # Old adb (no transport id): the connection key is (ip:port, None) for both devices.
    bench.registry.enroll("VAL-1", FP_A, "", NOW)
    bench.registry.enroll("VAL-2", FP_B, "", NOW)
    bench.devices(legacy("10.0.0.5:5555"))
    bench.device("10.0.0.5:5555", FP_A, marker="VAL-1")
    assert await bench.resolver.resolve("VAL-1") == "10.0.0.5:5555"

    bench.device("10.0.0.5:5555", FP_B, marker="VAL-2")
    with pytest.raises(DeviceUnavailable, match="VAL-1 n'est pas connecté"):
        await bench.resolver.resolve("VAL-1")
    assert await bench.resolver.resolve("VAL-2") == "10.0.0.5:5555"


async def test_probe_parses_missing_marker_and_null_android_id(bench):
    fp = Fingerprint("R58Q", "", "acme", "VAL")
    bench.registry.enroll("VAL-9", fp, "", NOW)
    bench.devices(usb("R58Q", 1))
    bench.device("R58Q", fp)
    assert await bench.resolver.resolve("VAL-9") == "R58Q"
    assert (await bench.resolver.snapshot())[0].model == "VAL"


async def test_unparseable_probe_output_counts_as_offline(bench):
    bench.devices(usb("R58A", 1))
    bench.device("R58A", FP_A)
    bench.fake.on(PROBE, stdout="sh: settings: not found\n", serial="R58A")
    assert await bench.resolver.snapshot() == [DeviceStatus(None, "R58A", "offline", "", "SM X")]


async def test_probe_failing_settings_command_exits_non_zero():
    # `settings` failing (early boot: "cmd: Can't find service: settings") must fail the probe.
    assert "|| exit 1" in IDENTITY_PROBE


async def test_empty_android_id_in_probe_fails_closed(bench):
    # VAL-1 is not connected. A batch sibling sharing its valid ro.serialno is, but its
    # `settings get secure android_id` failed: an empty value must not read as "no android_id",
    # otherwise the serialno alone would hand VAL-1's ID to the sibling.
    bench.registry.enroll("VAL-1", FP_A, "", NOW)
    sibling = Fingerprint(FP_A.serialno, "5151aaaa", "samsung", "SM-A")
    bench.devices(usb("R58SIB", 1))
    bench.device("R58SIB", sibling)
    bench.fake.on(
        PROBE, stdout=f"serialno={FP_A.serialno}\nandroid_id=\nboot_id=b1\nmarker=\n", serial="R58SIB"
    )
    with pytest.raises(DeviceUnavailable, match="VAL-1 n'est pas connecté"):
        await bench.resolver.resolve("VAL-1")
    snapshot = await bench.resolver.snapshot()
    assert DeviceStatus(None, "R58SIB", "offline", "", "SM X") in snapshot
    assert all(status.device_id != "VAL-1" or status.adb_serial is None for status in snapshot)


async def test_same_device_over_usb_and_wifi_resolves_to_usb(bench):
    # Normal connect_wifi flow: `adb tcpip` needs the cable, so the device is listed twice.
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.devices(usb("R58M12AAAAA", 1), usb("192.168.1.50:5555", 2))
    bench.device("R58M12AAAAA", FP_A, marker="VAL-042", boot_id="b-042")
    bench.device("192.168.1.50:5555", FP_A, marker="VAL-042", boot_id="b-042")
    assert await bench.resolver.resolve("VAL-042") == "R58M12AAAAA"
    assert bench.registry.get("VAL-042").last_adb_serial == "R58M12AAAAA"

    bench.devices(usb("192.168.1.50:5555", 2), usb("R58M12AAAAA", 1))
    assert await bench.resolver.resolve("VAL-042") == "R58M12AAAAA"

    # Cable unplugged: only the Wi-Fi transport is left.
    bench.devices(usb("192.168.1.50:5555", 2))
    assert await bench.resolver.resolve("VAL-042") == "192.168.1.50:5555"


async def test_same_identity_unproven_suggests_disconnecting_a_transport(bench):
    # Same serialno, android_id and marker but boot_id unreadable: maybe one device twice,
    # maybe a clone. Refuse, and do not only advise wiping the marker.
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.devices(usb("R58M12AAAAA", 1), usb("192.168.1.50:5555", 2))
    bench.device("R58M12AAAAA", FP_A, marker="VAL-042", boot_id="")
    bench.device("192.168.1.50:5555", FP_A, marker="VAL-042", boot_id="")
    with pytest.raises(DeviceUnavailable, match="Plusieurs appareils") as exc:
        await bench.resolver.resolve("VAL-042")
    message = str(exc.value)
    assert "USB et Wi-Fi" in message
    assert "adb disconnect 192.168.1.50:5555" in message


async def test_clones_with_same_identity_but_different_boot_ids_are_refused(bench):
    # Cloned image (marker included): same serialno, android_id and marker, but two boots.
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.devices(usb("10.0.0.1:5555", 1), usb("10.0.0.2:5555", 2))
    bench.device("10.0.0.1:5555", FP_A, marker="VAL-042")
    bench.device("10.0.0.2:5555", FP_A, marker="VAL-042")
    with pytest.raises(DeviceUnavailable, match="Plusieurs appareils") as exc:
        await bench.resolver.resolve("VAL-042")
    assert "USB et Wi-Fi" not in str(exc.value)
    assert "effacez leur marqueur" in str(exc.value)


async def test_resolve_follows_device_from_usb_to_wifi(bench):
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.devices(usb("R58M12AAAAA", 1))
    bench.device("R58M12AAAAA", FP_A, marker="VAL-042")
    assert await bench.resolver.resolve("val-042") == "R58M12AAAAA"
    assert bench.registry.get("VAL-042").last_adb_serial == "R58M12AAAAA"

    bench.devices(usb("192.168.1.50:5555", 7))
    bench.device("192.168.1.50:5555", FP_A, marker="VAL-042")
    assert await bench.resolver.resolve("VAL-042") == "192.168.1.50:5555"
    assert bench.registry.get("VAL-042").last_adb_serial == "192.168.1.50:5555"


async def test_resolve_unknown_id_lists_known_ids(bench):
    bench.registry.enroll("VAL-1", FP_A, "", NOW)
    bench.registry.enroll("VAL-2", FP_B, "", NOW)
    bench.devices()
    with pytest.raises(DeviceUnavailable, match="VAL-099 non enrôlé") as exc:
        await bench.resolver.resolve("val-099")
    assert "VAL-1, VAL-2" in str(exc.value)
    assert "enroll_device" in str(exc.value)


async def test_resolve_disconnected_message(bench):
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.registry.remember_serial("VAL-042", "192.168.1.50:5555")
    bench.devices()
    with pytest.raises(DeviceUnavailable) as exc:
        await bench.resolver.resolve("VAL-042")
    message = str(exc.value)
    assert "VAL-042 n'est pas connecté" in message
    assert "192.168.1.50:5555" in message
    assert "connect_wifi" in message


async def test_resolve_unauthorized_message_mentions_rsa(bench):
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.registry.remember_serial("VAL-042", "R58M12AAAAA")
    bench.devices(state_line("R58M12AAAAA", "unauthorized", 3))
    with pytest.raises(DeviceUnavailable) as exc:
        await bench.resolver.resolve("VAL-042")
    assert "clé RSA" in str(exc.value)
    assert "VAL-042" in str(exc.value)
    assert bench.shell_calls() == []
    assert await bench.resolver.snapshot() == [
        DeviceStatus("VAL-042", "R58M12AAAAA", "unauthorized", "", "SM-A")
    ]


async def test_resolve_offline_message(bench):
    bench.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.registry.remember_serial("VAL-042", "192.168.1.50:5555")
    bench.devices(state_line("192.168.1.50:5555", "offline", 3))
    with pytest.raises(DeviceUnavailable) as exc:
        await bench.resolver.resolve("VAL-042")
    assert "VAL-042 est offline" in str(exc.value)
    assert "adb reconnect" in str(exc.value)


async def test_offline_device_identified_from_cached_fingerprint(bench):
    # Bogus serial, no android_id: only the marker identifies it; it was seen online before.
    fp = Fingerprint(BOGUS, "", "acme", "VAL")
    bench.registry.enroll("VAL-7", fp, "", NOW)
    bench.devices(usb("192.168.1.9:5555", 5))
    bench.device("192.168.1.9:5555", fp, marker="VAL-7")
    assert await bench.resolver.resolve("VAL-7") == "192.168.1.9:5555"
    bench.registry.remember_serial("VAL-7", "other")  # last_adb_serial no longer helps
    bench.devices(state_line("192.168.1.9:5555", "offline", 5))
    with pytest.raises(DeviceUnavailable, match="VAL-7 est offline"):
        await bench.resolver.resolve("VAL-7")


async def test_two_devices_same_bogus_serial_resolve_distinctly(bench):
    fp1 = Fingerprint(BOGUS, "aaaa1111", "acme", "VAL")
    fp2 = Fingerprint(BOGUS, "bbbb2222", "acme", "VAL")
    bench.registry.enroll("VAL-1", fp1, "", NOW)
    bench.registry.enroll("VAL-2", fp2, "", NOW)
    bench.devices(usb(BOGUS, 3), usb(BOGUS, 4))
    bench.device(TransportId(3), fp1)
    bench.device(TransportId(4), fp2)
    assert await bench.resolver.resolve("VAL-1") == TransportId(3)
    assert await bench.resolver.resolve("val-2") == TransportId(4)
    assert all(call[1] == "-t" for call in bench.shell_calls())
    assert bench.registry.get("VAL-1").last_adb_serial == BOGUS
    assert await bench.resolver.snapshot() == [
        DeviceStatus("VAL-1", "transport_id:3", "online", "", "VAL"),
        DeviceStatus("VAL-2", "transport_id:4", "online", "", "VAL"),
    ]


async def test_serialno_shared_by_online_devices_is_ambiguous(bench):
    # Y shares X's real serialno and has no android_id: the serial alone must not resolve it.
    fp_x = Fingerprint("R58SHARED", "aaaa1111", "acme", "VAL")
    fp_y = Fingerprint("R58SHARED", "", "acme", "VAL")
    bench.registry.enroll("VAL-1", fp_x, "", NOW)
    bench.devices(usb("10.0.0.1:5555", 1), usb("10.0.0.2:5555", 2))
    bench.device("10.0.0.1:5555", fp_x)
    bench.device("10.0.0.2:5555", fp_y)
    assert await bench.resolver.resolve("VAL-1") == "10.0.0.1:5555"
    snapshot = await bench.resolver.snapshot()
    assert DeviceStatus(None, "10.0.0.2:5555", "online", "", "VAL") in snapshot


async def test_resolve_refuses_two_devices_matching_same_id(bench):
    fp = Fingerprint(BOGUS, "", "acme", "VAL")
    bench.registry.enroll("VAL-1", fp, "", NOW)
    bench.devices(usb("10.0.0.1:5555", 1), usb("10.0.0.2:5555", 2))
    bench.device("10.0.0.1:5555", fp, marker="VAL-1")
    bench.device("10.0.0.2:5555", fp, marker="VAL-1")
    with pytest.raises(DeviceUnavailable, match="Plusieurs appareils") as exc:
        await bench.resolver.resolve("VAL-1")
    assert "10.0.0.1:5555" in str(exc.value) and "10.0.0.2:5555" in str(exc.value)


async def test_unresponsive_device_does_not_break_resolution(bench):
    bench.registry.enroll("VAL-1", FP_A, "", NOW)
    bench.devices(usb("R58A", 1), usb("R58BAD", 2))
    bench.device("R58A", FP_A, marker="VAL-1")
    bench.fake.on(["shell"], returncode=1, stderr="error: closed", serial="R58BAD")
    assert await bench.resolver.resolve("VAL-1") == "R58A"
    snapshot = await bench.resolver.snapshot()
    assert DeviceStatus(None, "R58BAD", "offline", "", "SM X") in snapshot


async def test_enroll_writes_marker_and_registry(bench):
    bench.devices(usb("R58C", 2))
    bench.device("R58C", FP_C)
    record = await bench.resolver.enroll("R58C", "val-043", "Bench 3", NOW)
    assert record.device_id == "VAL-043"
    assert record.label == "Bench 3"
    assert record.fingerprint == FP_C
    assert bench.registry.get("VAL-043").fingerprint == FP_C
    assert ["adb", "-s", "R58C", "shell", "sh", "-c", f"'echo VAL-043 > {MARKER_PATH}'"] in bench.fake.calls
    assert await bench.resolver.resolve("VAL-043") == "R58C"
    assert await bench.resolver.snapshot() == [DeviceStatus("VAL-043", "R58C", "online", "Bench 3", "SM-C")]


async def test_enroll_by_transport_id_when_serial_is_shared(bench):
    fp1 = Fingerprint(BOGUS, "aaaa1111", "acme", "VAL")
    fp2 = Fingerprint(BOGUS, "bbbb2222", "acme", "VAL")
    bench.devices(usb(BOGUS, 3), usb(BOGUS, 4))
    bench.device(TransportId(3), fp1)
    bench.device(TransportId(4), fp2)
    snapshot = await bench.resolver.snapshot()
    assert [s.adb_serial for s in snapshot] == ["transport_id:3", "transport_id:4"]

    with pytest.raises(DeviceUnavailable, match="transport_id:3"):
        await bench.resolver.enroll(BOGUS, "VAL-2", "", NOW)
    assert bench.registry.all() == []

    record = await bench.resolver.enroll("transport_id:4", "VAL-2", "", NOW)
    assert record.fingerprint == fp2
    assert ["adb", "-t", "4", "shell", "sh", "-c", f"'echo VAL-2 > {MARKER_PATH}'"] in bench.fake.calls
    assert await bench.resolver.resolve("VAL-2") == TransportId(4)


async def test_enroll_rejects_offline_device(bench):
    bench.devices(state_line("R58C", "offline", 2))
    with pytest.raises(DeviceUnavailable, match="offline"):
        await bench.resolver.enroll("R58C", "VAL-043", "", NOW)
    assert bench.registry.all() == []
    assert bench.shell_calls() == []


async def test_enroll_rejects_unauthorized_device(bench):
    bench.devices(state_line("R58C", "unauthorized", 2))
    with pytest.raises(DeviceUnavailable, match="clé RSA"):
        await bench.resolver.enroll("R58C", "VAL-043", "", NOW)
    assert bench.registry.all() == []


async def test_enroll_unknown_adb_serial(bench):
    bench.devices(usb("R58C", 2))
    with pytest.raises(DeviceUnavailable, match="R58X") as exc:
        await bench.resolver.enroll("R58X", "VAL-043", "", NOW)
    assert "R58C" in str(exc.value)
    assert bench.shell_calls() == []


async def test_enroll_refuses_device_known_by_marker_under_other_id(bench):
    fp = Fingerprint(BOGUS, "", "acme", "VAL")
    bench.registry.enroll("VAL-1", fp, "", NOW)
    bench.devices(usb("10.0.0.1:5555", 1))
    bench.device("10.0.0.1:5555", fp, marker="VAL-1")
    with pytest.raises(RegistryError, match="VAL-1"):
        await bench.resolver.enroll("10.0.0.1:5555", "VAL-2", "", NOW)
    assert bench.registry.get("VAL-2") is None


async def test_enroll_invalid_id_raises_value_error(bench):
    with pytest.raises(ValueError):
        await bench.resolver.enroll("R58C", "-bad", "", NOW)
    assert bench.fake.calls == []


async def test_lock_serializes_same_device(bench):
    resolver = bench.resolver
    assert resolver.lock("val-1") is resolver.lock("VAL-1")

    async def hold(device_id):
        async with resolver.lock(device_id):
            await asyncio.sleep(0.05)

    start = time.monotonic()
    await asyncio.gather(hold("VAL-1"), hold("val-1"))
    assert time.monotonic() - start >= 0.1

    start = time.monotonic()
    await asyncio.gather(hold("VAL-1"), hold("VAL-2"))
    assert time.monotonic() - start < 0.1


async def test_hanging_device_does_not_delay_other_devices(bench, monkeypatch):
    # A Wi-Fi device still listed as `device` but no longer answering: its shell calls hang.
    monkeypatch.setattr("ia_gate.resolver.IDENTIFY_TIMEOUT_S", 0.2)
    bench.registry.enroll("VAL-1", FP_A, "", NOW)
    bench.devices(usb("R58A", 1), usb("192.168.1.60:5555", 2))
    bench.device("R58A", FP_A, marker="VAL-1")
    bench.fake.on(["shell"], delay=30, serial="192.168.1.60:5555")
    start = time.monotonic()
    assert await bench.resolver.resolve("VAL-1") == "R58A"
    snapshot = await bench.resolver.snapshot()
    assert time.monotonic() - start < 2
    assert DeviceStatus(None, "192.168.1.60:5555", "offline", "", "SM X") in snapshot
