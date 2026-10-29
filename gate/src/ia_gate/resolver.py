"""Device resolver: maps gate IDs to connected devices, reports status, enrolls, locks (spec §4, §5, §8).

The adb serial is never an identifier: on every call the connected devices are listed, each
online one is identified from its fingerprint and marker through the registry, and only then
is the adb target of the requested ID returned. A device whose adb serial is shared by several
connected devices is addressed by its transport id (`adb -t N`).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from ia_gate.adb import AdbClient, AdbDevice, AdbError, AdbNotFound, Target, duplicate_serials
from ia_gate.identity import Fingerprint, normalize_device_id, read_fingerprint, read_marker, write_marker
from ia_gate.registry import DeviceRecord, DeviceRegistry, RegistryError

State = Literal["online", "offline", "unauthorized", "disconnected"]

_TRANSPORT_PREFIX = "transport_id:"

# Deadline for identifying one online device during a scan. A device adb still lists as
# `device` but that no longer answers (typically Wi-Fi) must not stall calls on the others.
IDENTIFY_TIMEOUT_S = 8.0

# One adb connection: the serial alone is not enough (shared serials), and a reconnect gets a
# new transport id, so a device swapped behind the same serial is never served a stale cache.
_ConnKey = tuple[str, int | None]


@dataclass(frozen=True)
class DeviceStatus:
    device_id: str | None
    adb_serial: str | None
    state: State
    label: str
    model: str | None


class DeviceUnavailable(Exception):
    """The requested device cannot be addressed; the message tells the user what to do."""


@dataclass(frozen=True)
class _Seen:
    """A device listed by `adb devices`, with what the gate could learn about it."""

    device: AdbDevice
    target: Target
    state: State
    adb_state: str
    fingerprint: Fingerprint | None
    device_id: str | None

    @property
    def address(self) -> str:
        return str(self.target)


def _state(adb_state: str) -> State:
    if adb_state == "device":
        return "online"
    if adb_state in ("unauthorized", "authorizing"):
        return "unauthorized"
    return "offline"


def _conn_key(device: AdbDevice) -> _ConnKey:
    return (device.serial, device.transport_id)


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _not_ready_message(name: str, state: State, adb_state: str) -> str:
    """Actionable message for a device that is connected but not usable (`name` = ID or description)."""
    if state == "unauthorized":
        return (
            f"Acceptez la clé RSA de débogage sur l'écran de {name} (cochez « Toujours autoriser »), "
            "puis réessayez."
        )
    detail = "" if adb_state == "offline" else f" (état adb : {adb_state})"
    return (
        f"{_capitalize(name)} est offline{detail} : débranchez/rebranchez-le ou lancez `adb reconnect`, "
        "puis réessayez."
    )


def _describe(seen: Sequence[_Seen]) -> str:
    return ", ".join(f"{s.address} ({s.adb_state})" for s in seen) or "aucun"


class DeviceResolver:
    def __init__(self, adb: AdbClient, registry: DeviceRegistry) -> None:
        self._adb = adb
        self._registry = registry
        self._fingerprints: dict[_ConnKey, Fingerprint] = {}
        self._markers: dict[_ConnKey, str | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- public API ----------------------------------------------------------------------------

    async def snapshot(self) -> list[DeviceStatus]:
        """Every enrolled device (disconnected if absent), then every connected unenrolled one."""
        seen, records = await self._scan()
        by_id: dict[str, list[_Seen]] = {}
        for entry in seen:
            if entry.device_id is not None:
                by_id.setdefault(entry.device_id, []).append(entry)
        statuses: list[DeviceStatus] = []
        for device_id in sorted(set(records) | set(by_id)):
            record = records.get(device_id)
            label = record.label if record else ""
            hits = by_id.get(device_id, [])
            if not hits and record is not None:
                model = record.fingerprint.model or None
                statuses.append(DeviceStatus(device_id, None, "disconnected", label, model))
            # Several hits would be a gate-level conflict: show them all, `resolve` refuses them.
            for entry in hits:
                statuses.append(
                    DeviceStatus(device_id, entry.address, entry.state, label, self._model(entry, record))
                )
        for entry in seen:
            if entry.device_id is None:
                statuses.append(DeviceStatus(None, entry.address, entry.state, "", self._model(entry, None)))
        return statuses

    async def resolve(self, device_id: str) -> Target:
        """adb target of the online device enrolled as `device_id`, else `DeviceUnavailable`."""
        key = normalize_device_id(device_id)
        seen, records = await self._scan()
        record = records.get(key)
        hits = [entry for entry in seen if entry.device_id == key]
        if record is None and not hits:
            known = ", ".join(sorted(records)) or "aucun"
            raise DeviceUnavailable(
                f"Équipement {key} non enrôlé. Équipements connus : {known}. Pour un nouvel appareil, "
                "repérez son serial adb avec list_devices puis enrôlez-le avec enroll_device."
            )
        if not hits:
            last = record.last_adb_serial if record else None
            message = (
                f"{key} n'est pas connecté (dernier serial adb connu : {last or 'aucun'}). "
                "Branchez-le en USB ou utilisez connect_wifi."
            )
            unknown = [entry for entry in seen if entry.device_id is None and entry.state != "online"]
            if unknown:
                message += (
                    f" Appareils détectés mais non identifiables : {_describe(unknown)} ; si l'un d'eux "
                    f"est {key}, acceptez la clé RSA sur son écran ou rebranchez-le."
                )
            raise DeviceUnavailable(message)
        if len(hits) > 1:
            raise DeviceUnavailable(
                f"Plusieurs appareils connectés correspondent à {key} : {_describe(hits)}. Impossible de "
                f"choisir sans risque d'erreur : débranchez ceux qui ne sont pas {key}, ou effacez leur "
                "marqueur et enrôlez-les sous un autre ID."
            )
        hit = hits[0]
        if hit.state != "online":
            raise DeviceUnavailable(_not_ready_message(key, hit.state, hit.adb_state))
        if record is not None:
            self._registry.remember_serial(key, hit.device.serial)
        return hit.target

    def lock(self, device_id: str) -> asyncio.Lock:
        """The lock serializing operations on one device (same lock for any spelling of the ID)."""
        key = normalize_device_id(device_id)
        return self._locks.setdefault(key, asyncio.Lock())

    async def enroll(self, adb_serial: str, device_id: str, label: str, now: str) -> DeviceRecord:
        """Enroll the online device `adb_serial` (or `transport_id:N`) as `device_id`; write its marker."""
        key = normalize_device_id(device_id)
        devices = await self._adb.devices()
        duplicated = duplicate_serials(devices)
        device = self._pick(devices, adb_serial.strip(), duplicated)
        state = _state(device.state)
        if state != "online":
            raise DeviceUnavailable(
                _not_ready_message(f"l'appareil {adb_serial.strip()}", state, device.state)
            )
        target = device.target(duplicated)
        fingerprint = await read_fingerprint(self._adb, target)
        marker = await read_marker(self._adb, target)
        if marker is not None and marker != key and self._registry.match(fingerprint, marker, ()) == marker:
            raise RegistryError(
                f"Cet appareil est déjà enrôlé sous l'ID {marker} (marqueur présent sur l'appareil). "
                f"Utilisez {marker}, ou retirez d'abord {marker} du registre ({self._registry.path}) "
                f"avant de le ré-enrôler sous {key}."
            )
        record = self._registry.enroll(key, fingerprint, label, now)
        await write_marker(self._adb, target, key)
        self._registry.remember_serial(key, device.serial)
        conn = _conn_key(device)
        self._fingerprints[conn] = fingerprint
        self._markers[conn] = key
        return self._registry.get(key) or record

    # -- internals -----------------------------------------------------------------------------

    @staticmethod
    def _model(entry: _Seen, record: DeviceRecord | None) -> str | None:
        if entry.fingerprint is not None and entry.fingerprint.model:
            return entry.fingerprint.model
        if record is not None and record.fingerprint.model:
            return record.fingerprint.model
        return entry.device.model

    @staticmethod
    def _pick(devices: Sequence[AdbDevice], adb_serial: str, duplicated: Collection[str]) -> AdbDevice:
        if adb_serial.startswith(_TRANSPORT_PREFIX):
            value = adb_serial.removeprefix(_TRANSPORT_PREFIX)
            matches = [d for d in devices if value.isdecimal() and d.transport_id == int(value)]
        else:
            matches = [d for d in devices if d.serial == adb_serial]
        if not matches:
            connected = ", ".join(str(d.target(duplicated)) for d in devices) or "aucun"
            raise DeviceUnavailable(
                f"Aucun appareil connecté avec le serial adb {adb_serial}. Appareils connectés : "
                f"{connected}. Utilisez le serial adb exact donné par list_devices."
            )
        if len(matches) > 1:
            choices = [str(d.target(duplicated)) for d in matches if d.transport_id is not None]
            if len(choices) == len(matches):
                hint = f"précisez lequel enrôler avec adb_serial = {' ou '.join(choices)} (voir list_devices)"
            else:
                hint = (
                    "branchez-les un par un, ou mettez à jour adb (platform-tools) "
                    "pour obtenir leur transport_id"
                )
            raise DeviceUnavailable(
                f"Plusieurs appareils connectés partagent le serial adb {adb_serial} : {hint}."
            )
        return matches[0]

    async def _identify(self, device: AdbDevice, target: Target) -> Fingerprint | None:
        """Fingerprint (cached per connection) and fresh marker of an online device; None if it fails.

        Bounded by `IDENTIFY_TIMEOUT_S`: a device that does not answer in time counts as offline
        (cancelling the read kills the pending adb subprocess).
        """
        try:
            return await asyncio.wait_for(self._read_identity(device, target), IDENTIFY_TIMEOUT_S)
        except AdbNotFound:
            raise
        except (AdbError, TimeoutError):
            return None

    async def _read_identity(self, device: AdbDevice, target: Target) -> Fingerprint:
        conn = _conn_key(device)
        fingerprint = self._fingerprints.get(conn)
        if fingerprint is None:
            fingerprint = await read_fingerprint(self._adb, target)
            self._fingerprints[conn] = fingerprint
        self._markers[conn] = await read_marker(self._adb, target)
        return fingerprint

    async def _scan(self) -> tuple[list[_Seen], dict[str, DeviceRecord]]:
        devices = await self._adb.devices()
        connected = {_conn_key(d) for d in devices}
        for cache in (self._fingerprints, self._markers):
            for conn in [c for c in cache if c not in connected]:
                del cache[conn]
        duplicated = duplicate_serials(devices)
        targets = [d.target(duplicated) for d in devices]
        online = [i for i, d in enumerate(devices) if _state(d.state) == "online"]
        identified = await asyncio.gather(*(self._identify(devices[i], targets[i]) for i in online))
        fingerprints: dict[int, Fingerprint | None] = dict(zip(online, identified, strict=True))
        # A ro.serialno shared by several online devices cannot identify any of them.
        counts = Counter(fp.serialno for fp in identified if fp is not None)
        ambiguous = frozenset(serialno for serialno, count in counts.items() if count > 1)
        records = {record.device_id: record for record in self._registry.all()}

        seen: list[_Seen] = []
        for index, device in enumerate(devices):
            state = _state(device.state)
            conn = _conn_key(device)
            if index in fingerprints:
                fingerprint = fingerprints[index]
                if fingerprint is None:
                    state = "offline"  # listed as online but does not answer shell commands
            else:
                fingerprint = self._fingerprints.get(conn)
            device_id = None
            if fingerprint is not None:
                device_id = self._registry.match(fingerprint, self._markers.get(conn), ambiguous)
            seen.append(_Seen(device, targets[index], state, device.state, fingerprint, device_id))

        # A device that is not online cannot be fingerprinted: fall back to the last adb serial
        # an enrolled device was reached through, when unambiguous.
        claimed = {entry.device_id for entry in seen if entry.device_id is not None}
        for index, entry in enumerate(seen):
            if entry.device_id is not None or entry.state == "online" or entry.device.serial in duplicated:
                continue
            if entry.fingerprint is not None:
                continue
            candidates = [
                device_id
                for device_id, record in records.items()
                if record.last_adb_serial == entry.device.serial and device_id not in claimed
            ]
            if len(candidates) == 1:
                seen[index] = _Seen(
                    entry.device, entry.target, entry.state, entry.adb_state, None, candidates[0]
                )
                claimed.add(candidates[0])
        return seen, records
