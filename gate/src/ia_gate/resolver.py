"""Device resolver: maps gate IDs to connected devices, reports status, enrolls, locks (spec §4, §5, §8).

The adb serial is never an identifier: on every call the connected devices are listed, each
online one is identified from its fingerprint and marker through the registry, and only then
is the adb target of the requested ID returned. A device whose adb serial is shared by several
connected devices is addressed by its transport id (`adb -t N`).
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from ia_gate.adb import AdbClient, AdbDevice, AdbError, AdbNotFound, Target, duplicate_serials
from ia_gate.identity import (
    MARKER_PATH,
    Fingerprint,
    normalize_device_id,
    read_fingerprint,
    read_marker,
    write_marker,
)
from ia_gate.registry import DeviceRecord, DeviceRegistry, RegistryError

State = Literal["online", "offline", "unauthorized", "disconnected"]

_TRANSPORT_PREFIX = "transport_id:"

# Deadline for identifying one online device during a scan. A device adb still lists as
# `device` but that no longer answers (typically Wi-Fi) must not stall calls on the others.
IDENTIFY_TIMEOUT_S = 8.0

# Read on every scan, in one adb call, from every online device: the fields that identify it.
# Each value is tagged so a missing or failing command cannot shift the others. A failing
# `settings` (early boot) fails the whole probe: an unread android_id must never pass for "none".
# boot_id is random per boot: equal values prove two adb transports reach the same device.
IDENTITY_PROBE = (
    "a=$(settings get secure android_id) || exit 1; "
    'echo "serialno=$(getprop ro.serialno)"; '
    'echo "android_id=$a"; '
    'echo "boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"; '
    f'echo "marker=$(cat {MARKER_PATH} 2>/dev/null)"'
)
_PROBE_TAGS = ("serialno", "android_id", "boot_id", "marker")

# adb serial of a network transport: `ip:port`, or an mDNS service name (`adb-…._adb-tls-connect._tcp`).
_NETWORK_SERIAL = re.compile(r"(:\d+|\._adb-tls-[a-z]+\._tcp\.?)$")

# One adb connection: the serial alone is not enough (shared serials). It does not prove the
# device behind it is unchanged (transport ids restart with the adb server, Wi-Fi addresses are
# reassigned), so cached data is always checked against a fresh identity read.
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
    marker: str | None = None  # read fresh during this scan, else None
    boot_id: str = ""  # read fresh during this scan, else ""

    @property
    def address(self) -> str:
        return str(self.target)

    @property
    def is_network(self) -> bool:
        return _NETWORK_SERIAL.search(self.device.serial) is not None


def _state(adb_state: str) -> State:
    if adb_state == "device":
        return "online"
    if adb_state in ("unauthorized", "authorizing"):
        return "unauthorized"
    return "offline"


def _conn_key(device: AdbDevice) -> _ConnKey:
    return (device.serial, device.transport_id)


def _parse_probe(text: str, target: Target) -> tuple[str, str, str, str | None]:
    """(serialno, android_id, boot_id, marker) from the output of `IDENTITY_PROBE`.

    `AdbError` if garbled or if android_id is empty: a device without one reports the literal
    `null`, so an empty value means the read failed (fail closed, the device counts as offline).
    """
    lines = text.splitlines()
    values: dict[str, str] = {}
    for index, line in enumerate(lines):
        tag, sep, value = line.partition("=")
        if not sep or tag in values or tag not in _PROBE_TAGS:
            continue
        if tag == "marker":
            # Last field: keep the whole file content, a multi-line marker is malformed.
            value = "\n".join([value, *lines[index + 1 :]])
        values[tag] = value.strip()
        if tag == "marker":
            break
    if len(values) != len(_PROBE_TAGS) or not values["android_id"]:
        output = text.strip() or "aucune sortie"
        raise AdbError(
            f"Réponse inattendue de {target} à la lecture de son identité : {output}. "
            "Vérifiez que `adb shell getprop ro.serialno` répond normalement, sinon rebranchez l'appareil."
        )
    android_id = "" if values["android_id"] == "null" else values["android_id"]
    try:
        marker: str | None = normalize_device_id(values["marker"])
    except ValueError:
        marker = None
    return values["serialno"], android_id, values["boot_id"], marker


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


def _same_transports(hits: Sequence[_Seen]) -> tuple[bool, bool]:
    """(may be one device on several transports, proven to be).

    Possible when every hit was identified fresh with the same serialno, android_id and marker
    and no two of them report different boot ids; proven when, in addition, the boot id and the
    android_id are known (a cloned image can carry the marker, never the boot id).
    """
    identities = {
        (h.fingerprint.serialno, h.fingerprint.android_id, h.marker) if h.fingerprint else None
        for h in hits
    }
    if len(identities) != 1 or None in identities or any(h.marker is None for h in hits):
        return False, False
    boot_ids = {h.boot_id for h in hits}
    if len(boot_ids - {""}) > 1:
        return False, False
    proven = "" not in boot_ids and all(h.fingerprint and h.fingerprint.android_id for h in hits)
    return True, proven


def _several_hits_message(key: str, hits: Sequence[_Seen], maybe_same: bool) -> str:
    message = f"Plusieurs appareils connectés correspondent à {key} : {_describe(hits)}. "
    if maybe_same:
        network = [h.address for h in hits if h.is_network]
        how = (
            "lancez " + " ou ".join(f"`adb disconnect {address}`" for address in network)
            if network
            else "débranchez l'une des connexions"
        )
        return message + (
            f"Il s'agit probablement de {key} connecté plusieurs fois (USB et Wi-Fi) : débranchez "
            f"le câble USB ou {how}, puis réessayez."
        )
    return message + (
        f"Impossible de choisir sans risque d'erreur : débranchez ceux qui ne sont pas {key}, ou "
        "effacez leur marqueur et enrôlez-les sous un autre ID."
    )


class DeviceResolver:
    def __init__(self, adb: AdbClient, registry: DeviceRegistry) -> None:
        self._adb = adb
        self._registry = registry
        self._fingerprints: dict[_ConnKey, Fingerprint] = {}
        self._markers: dict[_ConnKey, str | None] = {}
        self._boot_ids: dict[_ConnKey, str] = {}
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
            # Several hits (one device over USB and Wi-Fi, or a conflict `resolve` refuses): show all.
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
        online = [h for h in hits if h.state == "online"]
        if online:
            # A transport that is not online cannot receive commands (e.g. a dead Wi-Fi link adb still
            # lists, identified only from cache): it must not block the device's working transport.
            hits = online
        elif len(hits) > 1:
            # Nothing is reachable: report the state of the preferred (cable) transport.
            hits = sorted(hits, key=lambda h: (h.is_network, h.address))[:1]
        if len(hits) > 1:
            maybe_same, proven = _same_transports(hits)
            if not proven:
                raise DeviceUnavailable(_several_hits_message(key, hits, maybe_same))
            # One device reached over USB and Wi-Fi (connect_wifi flow): prefer the cable.
            hits = sorted(hits, key=lambda h: (h.is_network, h.address))
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
        """Fresh fingerprint (model cached per connection) and marker of an online device; None if it fails.

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
        """Identity read fresh on every scan; only manufacturer and model are cached.

        The connection key (adb serial, transport id) does not prove it is still the same
        device: transport ids restart at 1 with the adb server and a Wi-Fi `ip:port` can be
        handed to another device. So serialno, android_id and marker are always re-read, and the
        cached entry is dropped as soon as serialno or android_id differ from it.
        """
        conn = _conn_key(device)
        result = await self._adb.shell(target, ["sh", "-c", IDENTITY_PROBE])
        serialno, android_id, boot_id, marker = _parse_probe(result.text, target)
        self._markers[conn] = marker
        self._boot_ids[conn] = boot_id
        cached = self._fingerprints.get(conn)
        if cached is not None and (cached.serialno, cached.android_id) == (serialno, android_id):
            return cached
        self._fingerprints.pop(conn, None)
        manufacturer = await self._adb.getprop(target, "ro.product.manufacturer")
        model = await self._adb.getprop(target, "ro.product.model")
        fingerprint = Fingerprint(serialno, android_id, manufacturer, model)
        self._fingerprints[conn] = fingerprint
        return fingerprint

    async def _scan(self) -> tuple[list[_Seen], dict[str, DeviceRecord]]:
        devices = await self._adb.devices()
        connected = {_conn_key(d) for d in devices}
        for cache in (self._fingerprints, self._markers, self._boot_ids):
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
            marker, boot_id = None, ""
            if index in fingerprints:
                fingerprint = fingerprints[index]
                if fingerprint is None:
                    state = "offline"  # listed as online but does not answer shell commands
                else:
                    marker, boot_id = self._markers.get(conn), self._boot_ids.get(conn, "")
            else:
                fingerprint = self._fingerprints.get(conn)
            device_id = None
            if fingerprint is not None:
                device_id = self._registry.match(fingerprint, self._markers.get(conn), ambiguous)
            seen.append(
                _Seen(device, targets[index], state, device.state, fingerprint, device_id, marker, boot_id)
            )

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
