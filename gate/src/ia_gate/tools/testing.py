"""Instrumented test runs and the restricted shell (spec §5, §6).

`run_instrumented_tests` runs `am instrument -w -r` and parses its raw status stream into a
structured result. `shell` only runs commands whose first words match an entry of
`policy.shell_allowlist`, with no shell metacharacter; each word is then quoted by `AdbClient`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ia_gate.adb import AdbError, AdbNotFound, AdbTimeout, Target
from ia_gate.context import GateContext, audited, device_call
from ia_gate.identity import normalize_device_id
from ia_gate.tools.apps import check_package
from ia_gate.tools.observe import truncate_tail

DEFAULT_RUNNER = "androidx.test.runner.AndroidJUnitRunner"
RAW_TAIL_CHARS = 4000

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


def parse_instrument_output(text: str) -> InstrumentResult:
    """Parse the output of `am instrument -w -r`.

    Each test reports status blocks (`INSTRUMENTATION_STATUS: key=value` lines, values possibly
    spanning several lines) closed by `INSTRUMENTATION_STATUS_CODE`: `1` start, `0` ok, `-2`
    assertion failure, `-1` error. A run that reached its end prints `INSTRUMENTATION_CODE: -1`;
    without it the instrumentation crashed or was killed.
    """
    passed = 0
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
                test = f"{status.get('class', '?')}#{status.get('test', '?')}"
                stack = status.get("stack") or status.get("stream", "")
                failures.append({"test": test, "stack": stack.strip()})
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
        failed=len(failures),
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
        and return passed / failed counts, failures (test `Class#method` and stack trace), whether
        the run completed (false: the app crashed or was killed) and the end of the raw output.
        `test_class` restricts the run to a class, a method (`Class#method`) or a comma-separated
        list. Install the app and its test APK first with install_apk."""
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
            except AdbTimeout as exc:
                raise ToolError(
                    f"Tests instrumentés expirés après {timeout_s} s sur {key} : augmentez timeout_s "
                    "ou limitez la campagne avec test_class."
                ) from exc
            except AdbNotFound:
                raise
            except AdbError as exc:
                if _MISSING_INSTRUMENTATION in str(exc):
                    raise missing from exc
                _, partial = split_adb_failure(exc)
                if _STATUS_PREFIX in partial:
                    # adb died mid-run (e.g. USB unplugged): report what ran; completed is False.
                    return {"device_id": key, **parse_instrument_output(partial).to_dict()}
                raise bounded_adb_failure(exc, ctx.config.max_log_bytes) from exc
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
