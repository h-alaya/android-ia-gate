"""Device identity: fingerprint, serialno validity, gate ID format and on-device marker.

`ro.serialno` is unreliable on modified ROMs (empty, `unknown`, `0123456789ABCDEF`, or shared
by a whole batch), so a device is identified by a gate ID chosen at enrollment, stored both in
the registry (with a fingerprint) and in a marker file on the device (spec §4).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

from ia_gate.adb import AdbClient, AdbError, AdbNotFound, AdbTimeout, Target

MARKER_PATH = "/data/local/tmp/.ia-gate-id"
DEVICE_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")

# Placeholder serialnos reported by ROMs that do not set a real one (compared case-insensitively).
_BOGUS_SERIALNOS = frozenset({"", "unknown", "0123456789abcdef", "0123456789"})


def normalize_device_id(raw: str) -> str:
    """Strip and upper-case a gate ID; raise `ValueError` if it does not match `DEVICE_ID_RE`."""
    device_id = raw.strip().upper()
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError(
            f"ID d'équipement invalide : {raw!r}. Format attendu : 2 à 32 caractères parmi "
            "A-Z0-9, `_` et `-`, commençant par une lettre ou un chiffre (ex. VAL-042)."
        )
    return device_id


@dataclass(frozen=True)
class Fingerprint:
    serialno: str
    android_id: str
    manufacturer: str
    model: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Fingerprint:
        values: dict[str, str] = {}
        for field in fields(cls):
            value = d.get(field.name)
            if not isinstance(value, str):
                raise ValueError(f"Empreinte invalide : champ `{field.name}` manquant ou non textuel.")
            values[field.name] = value
        return cls(**values)


def is_valid_serialno(serialno: str, ambiguous: Collection[str] = ()) -> bool:
    """False for placeholder serialnos and for those shared by several connected devices."""
    value = serialno.strip()
    return value.lower() not in _BOGUS_SERIALNOS and value not in ambiguous


async def read_fingerprint(adb: AdbClient, serial: Target) -> Fingerprint:
    serialno = await adb.getprop(serial, "ro.serialno")
    android_id = (await adb.shell(serial, ["settings", "get", "secure", "android_id"])).text.strip()
    if android_id == "null":
        android_id = ""
    manufacturer = await adb.getprop(serial, "ro.product.manufacturer")
    model = await adb.getprop(serial, "ro.product.model")
    return Fingerprint(serialno, android_id, manufacturer, model)


async def read_marker(adb: AdbClient, serial: Target) -> str | None:
    """The gate ID stored on the device, or None when absent, unreadable or malformed.

    Timeouts and a missing adb binary still propagate: they say nothing about the marker.
    """
    try:
        result = await adb.shell(serial, ["cat", MARKER_PATH])
    except (AdbTimeout, AdbNotFound):
        raise
    except AdbError:
        return None
    try:
        return normalize_device_id(result.text)
    except ValueError:
        return None


async def write_marker(adb: AdbClient, serial: Target, device_id: str) -> None:
    """Write `device_id` to the marker file, then read it back to verify it."""
    device_id = normalize_device_id(device_id)
    command = f"echo {shlex.quote(device_id)} > {shlex.quote(MARKER_PATH)}"
    await adb.shell(serial, ["sh", "-c", command])
    found = await read_marker(adb, serial)
    if found != device_id:
        raise AdbError(
            f"Le marqueur {MARKER_PATH} n'a pas pu être vérifié sur {serial} "
            f"(attendu {device_id}, lu {found or 'rien'}). Vérifiez que /data/local/tmp est "
            "inscriptible par l'utilisateur shell (adb shell touch /data/local/tmp/test)."
        )
