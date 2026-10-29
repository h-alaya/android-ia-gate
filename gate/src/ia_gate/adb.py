"""Async adb client.

Every adb invocation goes through an injected `Runner`, so tests can replay recorded
adb output without a real device. Arguments sent to `adb shell` / `adb exec-out` are
`shlex.quote`d because the device-side shell re-parses them.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdbResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def err_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


Runner = Callable[[Sequence[str], float], Awaitable[AdbResult]]


class AdbError(Exception):
    """An adb command failed."""


class AdbTimeout(AdbError):
    """An adb command did not finish in time."""


class AdbNotFound(AdbError):
    """The adb binary could not be found."""


class InstallError(AdbError):
    """`adb install` reported an Android failure code (e.g. INSTALL_FAILED_...)."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        message = f"Échec de l'installation : {code}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


def _format_seconds(value: float) -> str:
    return f"{value:g}"


async def subprocess_runner(args: Sequence[str], timeout: float) -> AdbResult:
    """Run `args` as a subprocess; kill it and raise `AdbTimeout` after `timeout` seconds."""
    args = list(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AdbNotFound(
            f"Binaire adb introuvable (`adb.path` = {args[0]}). Installez Android platform-tools "
            "ou corrigez `adb.path` dans ~/.ia-gate/config.toml."
        ) from exc
    except OSError as exc:
        raise AdbError(f"Impossible de lancer {args[0]} : {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AdbTimeout(
            f"Commande adb expirée après {_format_seconds(timeout)} s : {shlex.join(args)}. "
            "Vérifiez que l'équipement répond (adb devices) ou augmentez `adb.timeout_s`."
        ) from exc
    return AdbResult(proc.returncode if proc.returncode is not None else -1, stdout, stderr)


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str | None


_INSTALL_FAILURE_RE = re.compile(r"Failure \[([A-Z0-9_]+)")
_CONNECT_FAILURE_WORDS = ("failed", "unable")


def _output(result: AdbResult) -> str:
    """stderr and stdout combined, stripped, for error messages."""
    return "\n".join(part for part in (result.err_text.strip(), result.text.strip()) if part)


def _parse_devices(text: str) -> list[AdbDevice]:
    devices: list[AdbDevice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        serial, rest = tokens[0], tokens[1:]
        if rest[0] == "no" and len(rest) > 1 and rest[1] == "permissions":
            state = "no permissions"
        else:
            state = rest[0]
        model = None
        for token in rest:
            if token.startswith("model:"):
                model = token.removeprefix("model:").replace("_", " ") or None
        devices.append(AdbDevice(serial, state, model))
    return devices


class AdbClient:
    def __init__(self, runner: Runner, adb_path: str = "adb", timeout_s: float = 120) -> None:
        self._runner = runner
        self.adb_path = adb_path
        self.timeout_s = timeout_s

    async def _run(self, args: Sequence[str], timeout: float | None = None) -> AdbResult:
        return await self._runner([self.adb_path, *args], self.timeout_s if timeout is None else timeout)

    async def devices(self) -> list[AdbDevice]:
        result = await self._run(["devices", "-l"])
        if result.returncode != 0:
            raise AdbError(f"`adb devices` a échoué (code {result.returncode}) : {_output(result)}")
        return _parse_devices(result.text)

    async def shell(self, serial: str, args: Sequence[str], timeout: float | None = None) -> AdbResult:
        quoted = [shlex.quote(arg) for arg in args]
        result = await self._run(["-s", serial, "shell", *quoted], timeout)
        if result.returncode != 0:
            raise AdbError(
                f"Commande `{' '.join(args)}` échouée sur {serial} (code {result.returncode}) : "
                f"{_output(result) or 'aucune sortie'}"
            )
        return result

    async def exec_out(self, serial: str, args: Sequence[str]) -> bytes:
        quoted = [shlex.quote(arg) for arg in args]
        result = await self._run(["-s", serial, "exec-out", *quoted])
        if result.returncode != 0:
            raise AdbError(
                f"Commande `{' '.join(args)}` échouée sur {serial} (code {result.returncode}) : "
                f"{result.err_text.strip() or 'aucune sortie'}"
            )
        return result.stdout

    async def getprop(self, serial: str, name: str) -> str:
        return (await self.shell(serial, ["getprop", name])).text.strip()

    async def install(self, serial: str, apk_path: Path, replace: bool = True) -> None:
        args = ["-s", serial, "install"]
        if replace:
            args.append("-r")
        args.append(str(apk_path))
        result = await self._run(args)
        output = _output(result)
        match = _INSTALL_FAILURE_RE.search(output)
        if match:
            failure_line = next(line for line in output.splitlines() if match.group(0) in line)
            raise InstallError(match.group(1), failure_line.strip())
        if result.returncode != 0:
            raise AdbError(
                f"Installation de {apk_path} échouée sur {serial} (code {result.returncode}) : "
                f"{output or 'aucune sortie'}"
            )

    async def uninstall(self, serial: str, package: str) -> None:
        result = await self._run(["-s", serial, "uninstall", package])
        output = _output(result)
        if result.returncode != 0 or "Failure" in output:
            raise AdbError(
                f"Désinstallation de {package} échouée sur {serial} : {output or 'aucune sortie'}. "
                "Vérifiez que le package est installé (pm list packages)."
            )

    async def connect(self, host: str, port: int) -> str:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        result = await self._run(["connect", target])
        output = _output(result)
        if result.returncode != 0 or any(word in output.lower() for word in _CONNECT_FAILURE_WORDS):
            raise AdbError(
                f"Connexion Wi-Fi à {target} impossible : {output or 'aucune sortie'}. "
                "Vérifiez l'IP, que l'équipement est sur le même réseau et que `adb tcpip "
                f"{port}` a été lancé en USB."
            )
        return result.text.strip() or output
