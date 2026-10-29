"""Test doubles: `FakeRunner` replays adb outputs instead of running a real `adb`."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from ia_gate.adb import AdbResult


@dataclass
class _Response:
    prefix: list[str]
    serial: str | None
    result: AdbResult
    delay: float


def _to_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _split(args: Sequence[str]) -> tuple[str | None, list[str]]:
    """Return (serial, args after `adb [-s SERIAL]`)."""
    rest = list(args[1:])
    if len(rest) >= 2 and rest[0] == "-s":
        return rest[1], rest[2:]
    return None, rest


class FakeRunner:
    """A `Runner` that answers from registered responses and records every call.

    `on(prefix, ...)` matches on the args after `adb -s SERIAL` for serial-scoped calls,
    else after `adb`. Shell arguments are matched in their quoted form. When several
    responses match, a serial-specific one (`serial=`) beats a generic one, then the longest
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
        serial: str | None = None,
    ) -> None:
        result = AdbResult(returncode, _to_bytes(stdout), _to_bytes(stderr))
        self._responses.append(_Response(list(prefix), serial, result, delay))

    def _find(self, args: Sequence[str]) -> _Response | None:
        serial, scoped = _split(args)
        best: tuple[tuple[int, int, int], _Response] | None = None
        for index, response in enumerate(self._responses):
            if response.serial is not None and response.serial != serial:
                continue
            if scoped[: len(response.prefix)] != response.prefix:
                continue
            rank = (response.serial is not None, len(response.prefix), index)
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
