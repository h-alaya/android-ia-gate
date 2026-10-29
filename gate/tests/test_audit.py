import json
from datetime import UTC, datetime, timedelta

from ia_gate.audit import AuditLog


def test_audit_writes_jsonl(tmp_path):
    path = tmp_path / "nested" / "dir" / "audit.jsonl"
    log = AuditLog(path)
    log.record("device_info", {"device_id": "VAL-042"}, True, 12)
    log.record("install_apk", {"device_id": "VAL-042", "apk_path": "/x.apk"}, False, 7, error="Échec")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["tool"] == "device_info"
    assert first["args"] == {"device_id": "VAL-042"}
    assert first["ok"] is True
    assert first["duration_ms"] == 12
    assert first["error"] is None
    ts = datetime.fromisoformat(first["ts"])
    assert ts.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - ts) < timedelta(minutes=1)
    assert second["ok"] is False
    assert second["error"] == "Échec"
    assert "Échec" in lines[1]  # human-readable, not \u-escaped


def test_audit_serializes_non_json_args(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("x", {"path": tmp_path}, True, 0)
    assert json.loads(path.read_text(encoding="utf-8"))["args"] == {"path": str(tmp_path)}
