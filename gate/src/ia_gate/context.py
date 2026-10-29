"""Shared state of the gate and the helpers every tool module uses.

`audited` wraps a tool function so each call is journaled and expected failures reach the
client as `ToolError` with their actionable French message. `device_call` resolves a gate
ID to its adb target and runs an operation under that device's lock (spec §5).
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from ia_gate.adb import AdbClient, AdbError, Target
from ia_gate.audit import AuditLog
from ia_gate.config import GateConfig
from ia_gate.registry import DeviceRegistry, RegistryError
from ia_gate.resolver import DeviceResolver, DeviceUnavailable

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

# Failures the user can act on: their message is shown as is.
_EXPECTED_ERRORS = (DeviceUnavailable, AdbError, RegistryError, ValueError)


@dataclass
class GateContext:
    config: GateConfig
    adb: AdbClient
    registry: DeviceRegistry
    resolver: DeviceResolver
    audit: AuditLog
    clock: Callable[[], str]


def _call_args(signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        return {**{str(i): value for i, value in enumerate(args)}, **kwargs}
    bound.apply_defaults()
    return dict(bound.arguments)


def audited(ctx: GateContext, fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Wrap the async tool `fn`: audit every call, turn expected errors into `ToolError`."""
    signature = inspect.signature(fn)
    tool = fn.__name__

    def record(call_args: dict[str, Any], started: float, error: str | None) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            ctx.audit.record(tool, call_args, error is None, duration_ms, error)
        except OSError:
            # The tool already ran: losing an audit line must not turn its result into a failure.
            logger.exception("Écriture du journal d'audit %s impossible", ctx.audit.path)

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        call_args = _call_args(signature, args, kwargs)
        started = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except ToolError as exc:
            record(call_args, started, str(exc))
            raise
        except _EXPECTED_ERRORS as exc:
            record(call_args, started, str(exc))
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            record(call_args, started, f"{type(exc).__name__}: {exc}")
            raise
        record(call_args, started, None)
        return result

    return wrapper


async def device_call(ctx: GateContext, device_id: str, op: Callable[[Target], Awaitable[T]]) -> T:
    """Run `op` on the device enrolled as `device_id`, serialized with its other operations.

    The device is resolved once the lock is held, so `op` gets the device's current adb target.
    """
    async with ctx.resolver.lock(device_id):
        target = await ctx.resolver.resolve(device_id)
        return await op(target)
