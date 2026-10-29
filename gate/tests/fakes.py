"""Test doubles: `FakeRunner` replays adb outputs instead of running a real `adb`."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from ia_gate.adb import AdbResult, Target, TransportId

# A device-scoped call is keyed by its addressing flag and value: ("-s", SERIAL) or ("-t", N).
_TargetKey = tuple[str, str]


@dataclass
class _Response:
    prefix: list[str]
    target: _TargetKey | None
    result: AdbResult
    delay: float


def _to_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _key(target: Target) -> _TargetKey:
    if isinstance(target, TransportId):
        return ("-t", str(target.value))
    return ("-s", target)


def _split(args: Sequence[str]) -> tuple[_TargetKey | None, list[str]]:
    """Return (target key, args after `adb [-s SERIAL | -t N]`)."""
    rest = list(args[1:])
    if len(rest) >= 2 and rest[0] in ("-s", "-t"):
        return (rest[0], rest[1]), rest[2:]
    return None, rest


class FakeRunner:
    """A `Runner` that answers from registered responses and records every call.

    `on(prefix, ...)` matches on the args after `adb -s SERIAL` / `adb -t N` for
    device-scoped calls, else after `adb`. Shell arguments are matched in their quoted form.
    `serial=` restricts a response to one target (an adb serial or a `TransportId`). When
    several responses match, a target-specific one beats a generic one, then the longest
    prefix wins, then the most recently registered one (so a test can override a response).
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._responses: list[_Response] = []

    def on(
        self,
        prefix: list[str],
        stdout: str | bytes = "",
        returncode: int = 0,
        stderr: str = "",
        *,
        delay: float = 0.0,
        serial: Target | None = None,
    ) -> None:
        result = AdbResult(returncode, _to_bytes(stdout), _to_bytes(stderr))
        target = None if serial is None else _key(serial)
        self._responses.append(_Response(list(prefix), target, result, delay))

    def _find(self, args: Sequence[str]) -> _Response | None:
        target, scoped = _split(args)
        best: tuple[tuple[int, int, int], _Response] | None = None
        for index, response in enumerate(self._responses):
            if response.target is not None and response.target != target:
                continue
            if scoped[: len(response.prefix)] != response.prefix:
                continue
            rank = (response.target is not None, len(response.prefix), index)
            if best is None or rank > best[0]:
                best = (rank, response)
        return best[1] if best else None

    async def __call__(self, args: Sequence[str], timeout: float) -> AdbResult:
        args = list(args)
        self.calls.append(args)
        self.timeouts.append(timeout)
        response = self._find(args)
        if response is None:
            raise AssertionError(f"FakeRunner: unexpected adb call {args!r}")
        if response.delay:
            await asyncio.sleep(response.delay)
        return response.result
