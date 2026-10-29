import json

import pytest

from ia_gate.identity import Fingerprint
from ia_gate.registry import DeviceRecord, DeviceRegistry, RegistryError

NOW = "2026-10-29T10:00:00+00:00"
BOGUS = "0123456789ABCDEF"

FP_A = Fingerprint(serialno="R58M12AAAAA", android_id="aaaa1111", manufacturer="samsung", model="SM-A")
FP_B = Fingerprint(serialno="R58M12BBBBB", android_id="bbbb2222", manufacturer="samsung", model="SM-B")


@pytest.fixture
def path(tmp_path):
    return tmp_path / "devices.json"


@pytest.fixture
def reg(path):
    return DeviceRegistry(path)


def test_enroll_and_get_case_insensitive(reg):
    record = reg.enroll("val-042", FP_A, "Bench 1", NOW)
    assert record == DeviceRecord("VAL-042", "Bench 1", FP_A, NOW, None)
    assert reg.get("val-042") == record
    assert reg.get("VAL-042") == record
    assert reg.get("VAL-999") is None
    assert reg.get("not a valid id") is None


def test_all_sorted_by_id(reg):
    reg.enroll("VAL-2", FP_B, "", NOW)
    reg.enroll("VAL-1", FP_A, "", NOW)
    assert [r.device_id for r in reg.all()] == ["VAL-1", "VAL-2"]


def test_empty_when_file_absent(reg):
    assert reg.all() == []
    assert reg.match(FP_A, None, ()) is None


def test_persisted_across_instances(path, reg):
    reg.enroll("VAL-042", FP_A, "Bench 1", NOW)
    reg.remember_serial("val-042", "192.168.1.20:5555")
    other = DeviceRegistry(path)
    record = other.get("VAL-042")
    assert record == DeviceRecord("VAL-042", "Bench 1", FP_A, NOW, "192.168.1.20:5555")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert list(data["devices"]) == ["VAL-042"]


def test_rereads_file_on_every_call(path, reg):
    other = DeviceRegistry(path)
    assert reg.get("VAL-042") is None
    other.enroll("VAL-042", FP_A, "", NOW)
    assert reg.get("VAL-042") is not None


def test_atomic_write_leaves_no_tmp_files(tmp_path, path, reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    reg.enroll("VAL-2", FP_B, "", NOW)
    reg.remember_serial("VAL-1", "R58M12AAAAA")
    reg.remove("VAL-2")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["devices.json"]


def test_creates_parent_directory(tmp_path):
    reg = DeviceRegistry(tmp_path / "sub" / "devices.json")
    reg.enroll("VAL-1", FP_A, "", NOW)
    assert (tmp_path / "sub" / "devices.json").exists()


def test_enroll_id_taken_by_other_device_raises(reg):
    reg.enroll("VAL-042", FP_A, "", NOW)
    with pytest.raises(RegistryError, match="VAL-042"):
        reg.enroll("val-042", FP_B, "", NOW)
    assert reg.get("VAL-042").fingerprint == FP_A


def test_enroll_same_device_twice_under_other_id_raises(reg):
    reg.enroll("VAL-042", FP_A, "", NOW)
    with pytest.raises(RegistryError, match="VAL-042"):
        reg.enroll("VAL-043", FP_A, "", NOW)
    assert reg.get("VAL-043") is None


def test_reenroll_same_device_same_id_updates_label(reg):
    reg.enroll("VAL-042", FP_A, "old", NOW)
    reg.remember_serial("VAL-042", "R58M12AAAAA")
    record = reg.enroll("VAL-042", FP_A, "new", "2026-10-30T10:00:00+00:00")
    assert record.label == "new"
    assert record.enrolled_at == NOW
    assert record.last_adb_serial == "R58M12AAAAA"
    assert reg.get("VAL-042") == record


def test_reenroll_bogus_device_same_id_allowed(reg):
    fp = Fingerprint(BOGUS, "", "acme", "X1")
    reg.enroll("VAL-1", fp, "a", NOW)
    assert reg.enroll("VAL-1", fp, "b", NOW).label == "b"


def test_enroll_invalid_id_raises_value_error(reg):
    with pytest.raises(ValueError):
        reg.enroll("-bad", FP_A, "", NOW)


def test_match_prefers_marker(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    reg.enroll("VAL-2", Fingerprint(BOGUS, "", "samsung", "SM-A"), "", NOW)
    # The serial says VAL-1 but the marker says VAL-2, whose record contradicts nothing in the
    # probe (bogus serial, unknown android_id): the marker wins.
    probe = Fingerprint(FP_A.serialno, "", "samsung", "SM-A")
    assert reg.match(probe, None, ()) == "VAL-1"
    assert reg.match(probe, "VAL-2", ()) == "VAL-2"
    assert reg.match(probe, "val-2", ()) == "VAL-2"


def test_match_marker_contradicted_by_fingerprint_is_ignored(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    reg.enroll("VAL-2", FP_B, "", NOW)
    # Marker VAL-2 on a device whose serial and android_id are VAL-1's: stale marker.
    assert reg.match(FP_A, "VAL-2", ()) == "VAL-1"
    # One contradicting identifier is enough: a different valid serial...
    assert reg.match(Fingerprint("R58M12CCCCC", "", "samsung", "SM-B"), "VAL-2", ()) is None
    # ...or a different known android_id (batch sibling carrying a copied marker).
    assert reg.match(Fingerprint(FP_B.serialno, "zzzz9999", "samsung", "SM-B"), "VAL-2", ()) is None
    assert reg.match(Fingerprint(BOGUS, "zzzz9999", "samsung", "SM-B"), "VAL-2", ()) is None


def test_match_stale_marker_after_remove_and_reenroll(reg):
    old = Fingerprint("BATCH777", "id-one", "acme", "X1")
    new = Fingerprint("NEW1", "id-new", "acme", "X1")
    reg.enroll("VAL-1", old, "", NOW)
    reg.remove("VAL-1")
    reg.enroll("VAL-1", new, "", NOW)
    # The retired device still carries marker VAL-1: it must not resolve to the new VAL-1.
    assert reg.match(old, "VAL-1", ()) is None
    assert reg.match(new, "VAL-1", ()) == "VAL-1"


def test_match_unknown_marker_falls_back_to_fingerprint(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    assert reg.match(FP_A, "VAL-999", ()) == "VAL-1"


def test_match_by_serial(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    reg.enroll("VAL-2", FP_B, "", NOW)
    assert reg.match(FP_B, None, ()) == "VAL-2"
    # android_id unreadable: nothing contradicts the serial, which decides alone.
    fp = Fingerprint(FP_B.serialno, "", "samsung", "SM-B")
    assert reg.match(fp, None, ()) == "VAL-2"


def test_match_serial_with_conflicting_android_id_does_not_match(reg):
    # Same valid serial but a different android_id: either a sibling of a batch sharing the
    # serial, or the same device after a factory reset. Never resolve it to the record.
    reg.enroll("VAL-1", FP_A, "", NOW)
    fp = Fingerprint(FP_A.serialno, "changed-after-reset", "samsung", "SM-A")
    assert reg.match(fp, None, ()) is None


def test_match_batch_sibling_alone_does_not_resolve(reg):
    reg.enroll("VAL-1", Fingerprint("BATCH777", "id-one", "acme", "X1"), "", NOW)
    assert reg.match(Fingerprint("BATCH777", "id-two", "acme", "X1"), None, ()) is None


def test_enroll_batch_sibling_under_new_id_succeeds(reg):
    fp_a = Fingerprint("BATCH777", "id-one", "acme", "X1")
    fp_b = Fingerprint("BATCH777", "id-two", "acme", "X1")
    reg.enroll("VAL-1", fp_a, "", NOW)
    record = reg.enroll("VAL-2", fp_b, "", NOW)
    assert record.fingerprint == fp_b
    assert reg.get("VAL-1").fingerprint == fp_a
    assert reg.match(fp_a, None, ()) == "VAL-1"
    assert reg.match(fp_b, None, ()) == "VAL-2"


def test_enroll_batch_sibling_cannot_take_existing_id(reg):
    fp_a = Fingerprint("BATCH777", "id-one", "acme", "X1")
    fp_b = Fingerprint("BATCH777", "id-two", "acme", "X1")
    reg.enroll("VAL-1", fp_a, "", NOW)
    with pytest.raises(RegistryError, match="VAL-1"):
        reg.enroll("VAL-1", fp_b, "", NOW)
    assert reg.get("VAL-1").fingerprint == fp_a
    assert reg.match(fp_a, None, ()) == "VAL-1"
    assert reg.match(fp_b, None, ()) is None


def test_reenroll_after_factory_reset_requires_remove(reg):
    # A factory-reset device (same serial, new android_id) cannot be told apart from a batch
    # sibling: its old ID stays taken until the user removes it explicitly.
    reg.enroll("VAL-1", FP_A, "old", NOW)
    reset = Fingerprint(FP_A.serialno, "new-after-reset", "samsung", "SM-A")
    assert reg.match(reset, None, ()) is None
    with pytest.raises(RegistryError) as exc_info:
        reg.enroll("val-1", reset, "new", NOW)
    message = str(exc_info.value)
    assert "réinitialisé" in message
    assert "retirez d'abord VAL-1" in message
    assert reg.get("VAL-1").fingerprint == FP_A
    reg.remove("VAL-1")
    record = reg.enroll("val-1", reset, "new", "2026-10-30T10:00:00+00:00")
    assert record.fingerprint == reset
    assert reg.match(reset, None, ()) == "VAL-1"


def test_enroll_conflicting_device_without_shared_serial_still_raises(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    other = Fingerprint("R58M12CCCCC", "cccc3333", "samsung", "SM-A")
    with pytest.raises(RegistryError, match="VAL-1"):
        reg.enroll("VAL-1", other, "", NOW)


def test_match_skips_bogus_serial_uses_android_id(reg):
    reg.enroll("VAL-1", Fingerprint(BOGUS, "id-one", "acme", "X1"), "", NOW)
    reg.enroll("VAL-2", Fingerprint(BOGUS, "id-two", "acme", "X1"), "", NOW)
    assert reg.match(Fingerprint(BOGUS, "id-two", "acme", "X1"), None, ()) == "VAL-2"
    assert reg.match(Fingerprint(BOGUS, "id-one", "acme", "X1"), None, ()) == "VAL-1"


def test_match_bogus_serial_alone_does_not_match(reg):
    reg.enroll("VAL-1", Fingerprint(BOGUS, "id-one", "acme", "X1"), "", NOW)
    assert reg.match(Fingerprint(BOGUS, "id-other", "acme", "X1"), None, ()) is None


def test_match_ambiguous_serial_uses_android_id(reg):
    # VAL-1's android_id is unknown, so nothing contradicts a serial match on SHARED123.
    reg.enroll("VAL-1", Fingerprint("SHARED123", "", "acme", "X1"), "", NOW)
    reg.enroll("VAL-2", Fingerprint("OTHER456", "id-two", "acme", "X1"), "", NOW)
    probe = Fingerprint("SHARED123", "id-two", "acme", "X1")
    # Serial not shared among connected devices: it decides.
    assert reg.match(probe, None, ()) == "VAL-1"
    # The serial is shared by two connected devices: it is not used, android_id decides.
    assert reg.match(probe, None, {"SHARED123"}) == "VAL-2"


def write_records(path, records):
    devices = {
        device_id: {
            "label": "",
            "fingerprint": fp.to_dict(),
            "enrolled_at": NOW,
            "last_adb_serial": None,
        }
        for device_id, fp in records.items()
    }
    path.write_text(json.dumps({"version": 1, "devices": devices}), encoding="utf-8")


def test_match_serial_on_two_records_is_skipped(path, reg):
    # A batch sharing one serialno (two siblings enrolled): the serial criterion hits two
    # records and is skipped, android_id decides.
    write_records(
        path,
        {
            "VAL-1": Fingerprint("BATCH777", "id-one", "acme", "X1"),
            "VAL-2": Fingerprint("BATCH777", "id-two", "acme", "X1"),
        },
    )
    assert reg.match(Fingerprint("BATCH777", "id-two", "acme", "X1"), None, ()) == "VAL-2"
    assert reg.match(Fingerprint("BATCH777", "id-new", "acme", "X1"), None, ()) is None


def test_match_android_id_on_two_records_is_skipped(path, reg):
    write_records(
        path,
        {
            "VAL-1": Fingerprint(BOGUS, "same", "acme", "X1"),
            "VAL-2": Fingerprint(BOGUS, "same", "acme", "X1"),
        },
    )
    assert reg.match(Fingerprint(BOGUS, "same", "acme", "X1"), None, ()) is None
    assert reg.match(Fingerprint(BOGUS, "same", "acme", "X1"), "VAL-2", ()) == "VAL-2"


def test_enroll_duplicate_android_id_raises(reg):
    reg.enroll("VAL-1", Fingerprint(BOGUS, "same", "acme", "X1"), "", NOW)
    with pytest.raises(RegistryError, match="VAL-1"):
        reg.enroll("VAL-2", Fingerprint(BOGUS, "same", "acme", "X1"), "", NOW)


def test_match_empty_android_id_never_matches(reg):
    reg.enroll("VAL-1", Fingerprint(BOGUS, "", "acme", "X1"), "", NOW)
    assert reg.match(Fingerprint(BOGUS, "", "acme", "X1"), None, ()) is None


def test_match_unknown_returns_none(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    assert reg.match(FP_B, None, ()) is None
    assert reg.match(FP_B, "NOPE-1", ()) is None


def test_remember_serial_and_remove(reg):
    reg.enroll("VAL-1", FP_A, "", NOW)
    reg.remember_serial("val-1", "10.0.0.5:5555")
    assert reg.get("VAL-1").last_adb_serial == "10.0.0.5:5555"
    reg.remove("val-1")
    assert reg.get("VAL-1") is None
    assert reg.all() == []


def test_remember_serial_unknown_id_raises(reg):
    with pytest.raises(RegistryError, match="VAL-9"):
        reg.remember_serial("VAL-9", "x")


def test_remove_unknown_id_raises(reg):
    with pytest.raises(RegistryError, match="VAL-9"):
        reg.remove("VAL-9")


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"version": 2, "devices": {}}',
        '{"version": 1, "devices": []}',
        '{"version": 1, "devices": {"VAL-1": {"label": "x"}}}',
    ],
)
def test_corrupted_file_raises_registry_error(path, reg, content):
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RegistryError) as exc_info:
        reg.all()
    assert str(path) in str(exc_info.value)
