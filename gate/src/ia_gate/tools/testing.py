"""Instrumented test runs and the restricted shell (spec §5, §6).

`run_instrumented_tests` runs `am instrument -w -r` and parses its raw status stream into a
structured result. `shell` only runs commands whose first words match an entry of
`policy.shell_allowlist`, with no shell metacharacter; each word is then quoted by `AdbClient`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ia_gate.adb import AdbError, AdbNotFound, AdbTimeout, Target
from ia_gate.context import GateContext, audited, device_call
from ia_gate.identity import normalize_device_id
from ia_gate.tools.apps import check_package
from ia_gate.tools.observe import truncate_tail

logger = logging.getLogger(__name__)

DEFAULT_RUNNER = "androidx.test.runner.AndroidJUnitRunner"
RAW_TAIL_CHARS = 4000
# Bounds of the returned failures: a whole suite failing for one reason (crash in `@Before`,
# missing permission) would otherwise return megabytes of stacks. `failed` stays exact.
MAX_FAILURES = 50
STACK_MAX_CHARS = 2000

# `INSTRUMENTATION_STATUS_CODE` values of a test status block (androidx.test / JUnit runner).
_CODE_OK = 0
_CODE_ERROR = -1
_CODE_FAILURE = -2
# `INSTRUMENTATION_CODE: -1` (Activity.RESULT_OK) closes a run that went to the end.
_RUN_COMPLETED = -1

_STATUS_PREFIX = "INSTRUMENTATION_STATUS: "
_STATUS_CODE_RE = re.compile(r"INSTRUMENTATION_STATUS_CODE: (-?\d+)")
_RUN_CODE_RE = re.compile(r"INSTRUMENTATION_CODE: (-?\d+)")
_MISSING_INSTRUMENTATION = "Unable to find instrumentation info"
# `pm list instrumentation` line: "instrumentation:TEST_PKG/RUNNER (target=TARGET_PKG)".
_INSTRUMENTATION_LINE_RE = re.compile(r"instrumentation:([^/\s]+)/\S+ \(target=([^)\s]+)\)")
# Upper bound of the best-effort cleanup run after a timed-out or cancelled instrumentation.
STOP_TIMEOUT_S = 30

_CLASS = r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*"
# A runner may be given relative to the test package (`.MyRunner`), as `am instrument` allows.
_RUNNER_RE = re.compile(rf"\.?{_CLASS}", re.ASCII)
# `com.example.LoginTest`, `com.example.LoginTest#badPassword`, or a comma-separated list of those.
_TEST_ENTRY = rf"{_CLASS}(?:#[A-Za-z_][A-Za-z0-9_$]*)?"
_TEST_CLASS_RE = re.compile(rf"{_TEST_ENTRY}(?:,{_TEST_ENTRY})*", re.ASCII)

# `AdbClient.shell` failure: "Commande `ARGS` échouée sur TARGET (code N) : OUTPUT". The commands
# sent by this module never contain parentheses (forbidden or rejected by the validators), so the
# first "(code N) : " ends the header.
_ADB_FAILURE_RE = re.compile(r"(.*?\(code -?\d+\)) : (.*)", re.DOTALL)

_SHELL_METACHARS = frozenset(";&|`$><()\n\r\x00")
_SHELL_METACHARS_TEXT = "; & | ` $ > < ( ) ni retour ligne"


@dataclass
class InstrumentResult:
    passed: int
    failed: int
    failures: list[dict]
    completed: bool
    raw_tail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bound_stack(stack: str) -> str:
    """The head of `stack` (exception and top frames), at most `STACK_MAX_CHARS` chars plus a notice."""
    if len(stack) <= STACK_MAX_CHARS:
        return stack
    return f"{stack[:STACK_MAX_CHARS]}\n[… {len(stack) - STACK_MAX_CHARS} caractères tronqués]"


def parse_instrument_output(text: str) -> InstrumentResult:
    """Parse the output of `am instrument -w -r`.

    Each test reports status blocks (`INSTRUMENTATION_STATUS: key=value` lines, values possibly
    spanning several lines) closed by `INSTRUMENTATION_STATUS_CODE`: `1` start, `0` ok, `-2`
    assertion failure, `-1` error. A run that reached its end prints `INSTRUMENTATION_CODE: -1`;
    without it the instrumentation crashed or was killed.

    `failed` counts every failure; `failures` keeps the first `MAX_FAILURES`, each stack cut to
    its first `STACK_MAX_CHARS` chars.
    """
    passed = 0
    failed = 0
    failures: list[dict] = []
    completed = False
    status: dict[str, str] = {}
    current_key: str | None = None  # status key whose value continues on the next lines

    for line in text.splitlines():
        if line.startswith(_STATUS_PREFIX):
            key, sep, value = line.removeprefix(_STATUS_PREFIX).partition("=")
            current_key = key if sep else None
            if sep:
                status[key] = value
            continue
        code_match = _STATUS_CODE_RE.fullmatch(line.strip())
        if code_match:
            code = int(code_match.group(1))
            if code == _CODE_OK:
                passed += 1
            elif code in (_CODE_FAILURE, _CODE_ERROR):
                failed += 1
                if len(failures) < MAX_FAILURES:
                    test = f"{status.get('class', '?')}#{status.get('test', '?')}"
                    stack = status.get("stack") or status.get("stream", "")
                    failures.append({"test": test, "stack": _bound_stack(stack.strip())})
            status = {}
            current_key = None
            continue
        run_match = _RUN_CODE_RE.fullmatch(line.strip())
        if run_match:
            completed = completed or int(run_match.group(1)) == _RUN_COMPLETED
            current_key = None
            continue
        if line.startswith("INSTRUMENTATION_"):
            current_key = None  # INSTRUMENTATION_RESULT / INSTRUMENTATION_FAILED: not a test status
            continue
        if current_key is not None:
            status[current_key] += "\n" + line

    return InstrumentResult(
        passed=passed,
        failed=failed,
        failures=failures,
        completed=completed,
        raw_tail=text[-RAW_TAIL_CHARS:],
    )


def check_shell_command(command: str, allowlist: Sequence[str]) -> list[str]:
    """Split `command` into words if it is a single allowlisted command; else `ValueError`.

    The command must not contain any shell metacharacter, and its first words must equal the
    words of one `allowlist` entry (e.g. `pm list` allows `pm list packages`, not `pmlist`).
    """
    allowed_text = ", ".join(entry for entry in allowlist if entry.split()) or "aucune"
    advice = (
        f"Commandes autorisées (policy.shell_allowlist) : {allowed_text}. Utilisez les tools dédiés "
        "(install_apk, get_logs, launch_app…) ou ajoutez la commande à policy.shell_allowlist dans "
        "~/.ia-gate/config.toml."
    )
    forbidden = sorted({char for char in command if char in _SHELL_METACHARS})
    if forbidden:
        raise ValueError(
            f"Commande shell refusée : {command!r} contient un métacaractère interdit "
            f"({', '.join(repr(char) for char in forbidden)}). Passez une seule commande simple, "
            f"sans {_SHELL_METACHARS_TEXT}. {advice}"
        )
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise ValueError(
            f"Commande shell refusée : {command!r} est mal formée ({exc}). Vérifiez les guillemets. {advice}"
        ) from exc
    if not tokens:
        raise ValueError(f"Commande shell vide : indiquez une commande. {advice}")
    for entry in allowlist:
        words = entry.split()
        if words and tokens[: len(words)] == words:
            return tokens
    raise ValueError(f"Commande shell refusée : {command!r} n'est pas autorisée. {advice}")


def split_adb_failure(exc: AdbError) -> tuple[str, str]:
    """Split an `AdbClient.shell` failure into its header (command, target, exit code) and the
    command output; a message of another shape is returned whole as the header."""
    message = str(exc)
    match = _ADB_FAILURE_RE.fullmatch(message)
    if match is None:
        return message, ""
    return match.group(1), match.group(2)


def bounded_adb_failure(exc: AdbError, max_bytes: int) -> ToolError:
    """A `ToolError` for a failed adb command, keeping its header and at most the last
    `max_bytes` bytes of its output (a failing command may still print megabytes)."""
    header, output = split_adb_failure(exc)
    if not output:
        return ToolError(truncate_tail(header, max_bytes))
    return ToolError(f"{header} : {truncate_tail(output, max_bytes)}")


def instrumentation_targets(text: str, test_package: str) -> list[str]:
    """The target packages declared by the instrumentations of `test_package`, read from the
    output of `pm list instrumentation`."""
    targets: list[str] = []
    for line in text.splitlines():
        match = _INSTRUMENTATION_LINE_RE.fullmatch(line.strip())
        if match and match.group(1) == test_package and match.group(2) not in targets:
            targets.append(match.group(2))
    return targets


async def stop_instrumentation(ctx: GateContext, device_id: str, test_package: str) -> bool:
    """Best effort: force-stop the package under test and `test_package` on `device_id`.

    Killing the host-side `am instrument -w` does not stop the instrumentation on the device: the
    tests would keep driving the UI after the device lock is released. The caller holds the device
    lock; the device is resolved again because the target of the run may be stale after a long
    run (another device now sharing its serial, a transport id reused after an adb restart). If
    the device no longer resolves, nothing is stopped.

    Returns True only if every force-stop succeeded. Never raises (except on cancellation of this
    cleanup itself), so the caller's original error is kept; shielded from the cancellation of the
    enclosing call so that it also runs when the MCP call is cancelled.
    """
    stopped = False
    with anyio.CancelScope(shield=True), anyio.move_on_after(STOP_TIMEOUT_S):
        try:
            target = await ctx.resolver.resolve(device_id)
        except Exception:
            logger.warning("%s introuvable : am force-stop %s non lancé", device_id, test_package,
                           exc_info=True)
            return False
        complete = True
        packages: list[str] = []
        try:
            listing = (await ctx.adb.shell(target, ["pm", "list", "instrumentation"])).text
            packages = instrumentation_targets(listing, test_package)
        except Exception:
            complete = False  # the package under test is unknown: it may keep running
            logger.warning("Lecture de pm list instrumentation impossible sur %s", target, exc_info=True)
        if test_package not in packages:
            packages.append(test_package)
        for package in packages:
            try:
                check_package(package)
                await ctx.adb.shell(target, ["am", "force-stop", package])
            except Exception:
                complete = False
                logger.warning("am force-stop %s impossible sur %s", package, target, exc_info=True)
        stopped = complete
    return stopped


def _still_running(device_id: str, test_package: str) -> str:
    return (
        f"L'arrêt des tests n'a pas pu être confirmé : ils tournent peut-être encore sur {device_id}. "
        f"Avant de le réutiliser, vérifiez-le avec list_devices puis lancez stop_app sur {test_package} "
        "et sur l'app testée (ou redémarrez l'appareil)."
    )


def _check_runner(runner: str) -> str:
    if not _RUNNER_RE.fullmatch(runner):
        raise ValueError(
            f"Runner invalide : {runner!r}. Indiquez un nom de classe (ex. {DEFAULT_RUNNER}), "
            "visible avec `shell` « pm list instrumentation »."
        )
    return runner


def _check_test_class(test_class: str) -> str:
    if not _TEST_CLASS_RE.fullmatch(test_class):
        raise ValueError(
            f"test_class invalide : {test_class!r}. Indiquez une classe complète "
            "(ex. com.example.LoginTest), une méthode (com.example.LoginTest#badPassword) "
            "ou une liste séparée par des virgules."
        )
    return test_class


def _check_timeout(timeout_s: int) -> int:
    if timeout_s <= 0:
        raise ValueError(f"timeout_s invalide : {timeout_s}. Indiquez une durée en secondes supérieure à 0.")
    return timeout_s


def register(server: MCPServer, ctx: GateContext) -> None:
    async def run_instrumented_tests(
        device_id: str,
        test_package: str,
        runner: str = DEFAULT_RUNNER,
        test_class: str | None = None,
        timeout_s: int = 900,
    ) -> dict[str, Any]:
        """Run the instrumented tests of the installed test APK `test_package` (`am instrument -w -r`)
        and return passed / failed counts, failures (test `Class#method` and stack trace; at most
        the first 50, stacks cut to their first 2000 chars, `failed` stays exact), whether
        the run completed (false: the app crashed or was killed) and the end of the raw output.
        `test_class` restricts the run to a class, a method (`Class#method`) or a comma-separated
        list. Install the app and its test APK first with install_apk. If adb dies mid-run, the
        partial counts come back with `stopped` (whether the tests were force-stopped on the device)
        and, when that is not confirmed, a `warning`."""
        key = normalize_device_id(device_id)
        check_package(test_package)
        _check_runner(runner)
        if test_class is not None:
            _check_test_class(test_class)
        _check_timeout(timeout_s)
        args = ["am", "instrument", "-w", "-r"]
        if test_class is not None:
            args += ["-e", "class", test_class]
        args.append(f"{test_package}/{runner}")
        missing = ToolError(
            f"Instrumentation {test_package}/{runner} introuvable sur {key} : installez l'APK de test "
            "(install_apk) et vérifiez test_package et runner avec `shell` « pm list instrumentation »."
        )

        async def op(target: Target) -> dict[str, Any]:
            try:
                output = (await ctx.adb.shell(target, args, timeout=timeout_s)).text
            except asyncio.CancelledError:
                await stop_instrumentation(ctx, key, test_package)
                raise
            except AdbTimeout as exc:
                if await stop_instrumentation(ctx, key, test_package):
                    raise ToolError(
                        f"Tests instrumentés expirés après {timeout_s} s sur {key} (exécution arrêtée par "
                        "am force-stop) : augmentez timeout_s ou limitez la campagne avec test_class."
                    ) from exc
                raise ToolError(
                    f"Tests instrumentés expirés après {timeout_s} s sur {key}, et leur arrêt n'a pas pu "
                    "être confirmé : ils tournent peut-être encore sur l'appareil. Avant de le réutiliser, "
                    f"vérifiez-le avec list_devices puis lancez stop_app sur {test_package} et sur l'app "
                    "testée (ou redémarrez l'appareil) ; augmentez ensuite timeout_s ou limitez la "
                    "campagne avec test_class."
                ) from exc
            except AdbNotFound:
                raise
            except AdbError as exc:
                if _MISSING_INSTRUMENTATION in str(exc):
                    raise missing from exc
                # adb died mid-run (USB unplugged, Wi-Fi dropped): the tests may still run on the
                # device and would keep driving its UI once the device lock is released.
                stopped = await stop_instrumentation(ctx, key, test_package)
                _, partial = split_adb_failure(exc)
                if _STATUS_PREFIX in partial:
                    # Report what ran; completed is False.
                    result = {"device_id": key, **parse_instrument_output(partial).to_dict()}
                    result["stopped"] = stopped
                    if not stopped:
                        result["warning"] = _still_running(key, test_package)
                    return result
                error = bounded_adb_failure(exc, ctx.config.max_log_bytes)
                if not stopped:
                    error = ToolError(f"{error} {_still_running(key, test_package)}")
                raise error from exc
            if _MISSING_INSTRUMENTATION in output:
                raise missing
            return {"device_id": key, **parse_instrument_output(output).to_dict()}

        return await device_call(ctx, key, op)

    async def shell(device_id: str, command: str) -> str:
        """Run a read-only diagnostic shell command on the device. Only commands starting with an
        entry of policy.shell_allowlist are accepted (by default: getprop, pm list, pm path,
        dumpsys, settings get, ls, ps, df, wm size, ip addr), without ; & | ` $ > < ( ) or newline.
        Long output keeps its end, also when the command fails."""
        key = normalize_device_id(device_id)
        tokens = check_shell_command(command, ctx.config.shell_allowlist)

        async def op(target: Target) -> str:
            try:
                output = (await ctx.adb.shell(target, tokens)).text
            except (AdbNotFound, AdbTimeout):
                raise
            except AdbError as exc:
                # A non-zero exit may still carry a huge output (`ls -R /sdcard` on Android 11+).
                raise bounded_adb_failure(exc, ctx.config.max_log_bytes) from exc
            return truncate_tail(output, ctx.config.max_log_bytes)

        return await device_call(ctx, key, op)

    for fn in (run_instrumented_tests, shell):
        server.add_tool(audited(ctx, fn))
