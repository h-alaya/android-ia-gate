"""Persistent device registry: gate ID -> fingerprint, stored as JSON (spec §4).

The file is re-read on every call so several processes can share it, and every write is
atomic (temporary file in the same directory + `os.replace`).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ia_gate.identity import Fingerprint, is_valid_serialno, normalize_device_id

REGISTRY_VERSION = 1


class RegistryError(Exception):
    """Unreadable registry or enrollment conflict."""


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    label: str
    fingerprint: Fingerprint
    enrolled_at: str
    last_adb_serial: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "fingerprint": self.fingerprint.to_dict(),
            "enrolled_at": self.enrolled_at,
            "last_adb_serial": self.last_adb_serial,
        }


def _android_ids_conflict(a: Fingerprint, b: Fingerprint) -> bool:
    return bool(a.android_id) and bool(b.android_id) and a.android_id != b.android_id


def _contradicts(a: Fingerprint, b: Fingerprint) -> bool:
    """True when a known identifier proves `a` and `b` are different devices.

    A serialno shared by several connected devices cannot *identify* one device, but two different
    real serials still prove two devices are distinct, so only placeholder serials are ignored here.
    """
    serials_differ = (
        is_valid_serialno(a.serialno) and is_valid_serialno(b.serialno) and a.serialno != b.serialno
    )
    return serials_differ or _android_ids_conflict(a, b)


class DeviceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- queries -------------------------------------------------------------------------------

    def all(self) -> list[DeviceRecord]:
        records = self._load()
        return [records[device_id] for device_id in sorted(records)]

    def get(self, device_id: str) -> DeviceRecord | None:
        try:
            key = normalize_device_id(device_id)
        except ValueError:
            return None
        return self._load().get(key)

    def match(
        self, fingerprint: Fingerprint, marker: str | None, ambiguous_serials: Collection[str]
    ) -> str | None:
        """Gate ID of a connected device, in spec §4 order; a criterion hitting ≥ 2 records is skipped.

        A known marker is ignored when its record contradicts the fingerprint (both serials valid
        and different, or both android_ids known and different): stale marker of a retired or
        reassigned device. A serial match is rejected when both android_ids are known and differ
        (batch sibling or factory-reset device); resolution then falls through to android_id.
        An android_id match is rejected when both serials are valid and differ (cloned ROM image
        sharing one android_id).
        """
        return self._match(self._load(), fingerprint, marker, ambiguous_serials)

    # -- mutations -----------------------------------------------------------------------------

    def enroll(self, device_id: str, fingerprint: Fingerprint, label: str, now: str) -> DeviceRecord:
        device_id = normalize_device_id(device_id)
        records = self._load()
        existing = records.get(device_id)
        matched = self._match(records, fingerprint, None, ())
        if matched is not None and matched != device_id:
            raise RegistryError(
                f"Cet appareil est déjà enrôlé sous l'ID {matched}. Utilisez {matched}, ou retirez "
                f"d'abord {matched} du registre ({self.path}) avant de le ré-enrôler sous {device_id}."
            )
        # A factory-reset device (same serial, new android_id) cannot be told apart from a batch
        # sibling sharing the serial: the ID stays taken until the user removes it explicitly.
        if existing is not None and matched != device_id and existing.fingerprint != fingerprint:
            raise RegistryError(
                f"L'ID {device_id} est déjà attribué à un autre appareil "
                f"({existing.fingerprint.manufacturer} {existing.fingerprint.model}, "
                f"serialno {existing.fingerprint.serialno or 'vide'}). Choisissez un autre ID ; "
                f"si c'est le même appareil réinitialisé, retirez d'abord {device_id} du registre "
                f"({self.path}) puis ré-enrôlez-le."
            )
        if existing is not None:
            record = replace(existing, label=label, fingerprint=fingerprint)
        else:
            record = DeviceRecord(device_id, label, fingerprint, now)
        records[device_id] = record
        self._save(records)
        return record

    def remember_serial(self, device_id: str, adb_serial: str) -> None:
        def update(records: dict[str, DeviceRecord], key: str) -> bool:
            if records[key].last_adb_serial == adb_serial:
                return False
            records[key] = replace(records[key], last_adb_serial=adb_serial)
            return True

        self._mutate(device_id, update)

    def remove(self, device_id: str) -> None:
        def delete(records: dict[str, DeviceRecord], key: str) -> bool:
            del records[key]
            return True

        self._mutate(device_id, delete)

    # -- internals -----------------------------------------------------------------------------

    def _mutate(self, device_id: str, change: Callable[[dict[str, DeviceRecord], str], bool]) -> None:
        key = normalize_device_id(device_id)
        records = self._load()
        if key not in records:
            known = ", ".join(sorted(records)) or "aucun"
            raise RegistryError(f"Équipement {key} non enrôlé. Équipements connus : {known}.")
        if change(records, key):
            self._save(records)

    @staticmethod
    def _match(
        records: dict[str, DeviceRecord],
        fingerprint: Fingerprint,
        marker: str | None,
        ambiguous_serials: Collection[str],
    ) -> str | None:
        if marker is not None:
            try:
                key = normalize_device_id(marker)
            except ValueError:
                key = None
            if key is not None and key in records and not _contradicts(records[key].fingerprint, fingerprint):
                return key

        def unique(predicate: Callable[[DeviceRecord], bool]) -> str | None:
            hits = [device_id for device_id, record in records.items() if predicate(record)]
            return hits[0] if len(hits) == 1 else None

        if is_valid_serialno(fingerprint.serialno, ambiguous_serials):
            found = unique(lambda r: r.fingerprint.serialno == fingerprint.serialno)
            # A batch can share one serialno: a known, different android_id rules the record out.
            if found is not None and not _android_ids_conflict(records[found].fingerprint, fingerprint):
                return found
        if fingerprint.android_id:
            found = unique(lambda r: r.fingerprint.android_id == fingerprint.android_id)
            # Cloned ROM images can share one android_id: two valid, different serials rule it out.
            if found is not None and not _contradicts(records[found].fingerprint, fingerprint):
                return found
        return None

    def _corrupted(self, reason: str) -> RegistryError:
        return RegistryError(
            f"Registre des équipements illisible ({self.path}) : {reason}. Corrigez le fichier ou "
            "restaurez une sauvegarde ; le supprimer oblige à ré-enrôler tous les équipements."
        )

    def _load(self) -> dict[str, DeviceRecord]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise self._corrupted(f"lecture impossible ({exc.strerror or exc})") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise self._corrupted(f"JSON invalide ligne {exc.lineno}") from exc
        if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
            raise self._corrupted(f'format inattendu (attendu un objet avec "version": {REGISTRY_VERSION})')
        devices = data.get("devices")
        if not isinstance(devices, dict):
            raise self._corrupted('champ "devices" manquant ou qui n\'est pas un objet')
        records: dict[str, DeviceRecord] = {}
        for raw_id, entry in devices.items():
            try:
                device_id = normalize_device_id(raw_id)
                if not isinstance(entry, dict):
                    raise ValueError("entrée qui n'est pas un objet")
                label = entry.get("label", "")
                enrolled_at = entry.get("enrolled_at")
                last = entry.get("last_adb_serial")
                if not isinstance(label, str) or not isinstance(enrolled_at, str):
                    raise ValueError("champ `label` ou `enrolled_at` manquant ou non textuel")
                if last is not None and not isinstance(last, str):
                    raise ValueError("champ `last_adb_serial` non textuel")
                fp_data = entry.get("fingerprint")
                if not isinstance(fp_data, dict):
                    raise ValueError("champ `fingerprint` manquant")
                fingerprint = Fingerprint.from_dict(fp_data)
            except ValueError as exc:
                raise self._corrupted(f"entrée {raw_id!r} invalide ({exc})") from exc
            if device_id in records:
                raise self._corrupted(f"ID {device_id} présent plusieurs fois")
            records[device_id] = DeviceRecord(device_id, label, fingerprint, enrolled_at, last)
        return records

    def _save(self, records: dict[str, DeviceRecord]) -> None:
        data = {
            "version": REGISTRY_VERSION,
            "devices": {device_id: records[device_id].to_dict() for device_id in sorted(records)},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
