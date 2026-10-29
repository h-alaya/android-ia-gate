"""App lifecycle tools: install, uninstall, launch and stop an app on a device (spec §5).

Every user-supplied value is validated before any adb call: the APK must resolve (real path,
symlinks followed) under one of `policy.apk_dirs` (spec §6), package and activity names must
look like Java identifiers.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ia_gate.adb import AdbError, AdbNotFound, AdbTimeout, InstallError, Target
from ia_gate.context import GateContext, audited, device_call
from ia_gate.identity import normalize_device_id

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
# `.Main`, `com.example.app.Main` or a nested class `com.example.app.Main$Inner`.
_ACTIVITY_RE = re.compile(r"^\.?[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")

# The launched process may appear a moment after `monkey` returns: pidof is retried.
PID_ATTEMPTS = 5
PID_RETRY_DELAY_S = 0.3

_INSTALL_ADVICE = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": (
        "L'APK n'est pas signé comme l'app déjà installée : désinstallez d'abord (uninstall_app), "
        "puis relancez install_apk."
    ),
    "INSTALL_FAILED_VERSION_DOWNGRADE": (
        "L'APK a une version inférieure à celle installée : désinstallez d'abord (uninstall_app) "
        "ou installez une version supérieure."
    ),
}


def check_apk_path(raw: str, apk_dirs: Sequence[Path]) -> Path:
    """Return the real path of the APK `raw` if it is an `.apk` regular file under one of `apk_dirs`.

    Symlinks and `..` are resolved first, so neither can lead outside the allowed directories.
    """
    allowed = [Path(directory).expanduser().resolve() for directory in apk_dirs]
    allowed_text = ", ".join(str(directory) for directory in allowed) or "aucun"
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"APK introuvable : {raw!r}. Copiez l'APK dans un dossier autorisé "
            f"({allowed_text}) et indiquez son chemin complet."
        ) from exc
    if not any(path.is_relative_to(directory) for directory in allowed):
        raise ValueError(
            f"APK refusé : {path} n'est pas dans un dossier autorisé. Dossiers autorisés "
            f"(policy.apk_dirs) : {allowed_text}. Copiez l'APK dans l'un d'eux."
        )
    if path.suffix != ".apk" or not path.is_file():
        raise ValueError(
            f"APK refusé : {path} n'est pas un fichier .apk. Indiquez un fichier .apk situé dans "
            f"un dossier autorisé ({allowed_text})."
        )
    return path


def check_package(package: str) -> str:
    if not PACKAGE_RE.fullmatch(package):
        raise ValueError(
            f"Nom de package invalide : {package!r}. Indiquez un nom Android complet "
            "(ex. com.example.app), visible avec `shell` « pm list packages »."
        )
    return package


def _check_activity(activity: str) -> str:
    if not _ACTIVITY_RE.fullmatch(activity):
        raise ValueError(
            f"Activité invalide : {activity!r}. Indiquez le nom de classe seul (ex. .MainActivity "
            "ou com.example.app.MainActivity), sans le package ni « / »."
        )
    return activity


def _install_failure(device_id: str, apk: Path, exc: InstallError) -> ToolError:
    message = f"{exc} — APK {apk.name} sur {device_id}."
    advice = _INSTALL_ADVICE.get(exc.code)
    if advice:
        message += f" {advice}"
    return ToolError(message)


async def pidof(ctx: GateContext, target: Target, package: str) -> int | None:
    """PID of the running process of `package`, or None when it is not running."""
    try:
        output = (await ctx.adb.shell(target, ["pidof", package])).text
    except (AdbTimeout, AdbNotFound):
        raise
    except AdbError:
        return None  # pidof exits with 1 when no process matches
    for token in output.split():
        if token.isdecimal():
            return int(token)
    return None


def register(server: MCPServer, ctx: GateContext) -> None:
    async def install_apk(device_id: str, apk_path: str, replace: bool = True) -> dict[str, Any]:
        """Install an APK stored on the gate PC (in one of the allowed directories, policy.apk_dirs) on
        the device. `replace` reinstalls over an existing version, keeping its data."""
        key = normalize_device_id(device_id)
        apk = check_apk_path(apk_path, ctx.config.apk_dirs)

        async def op(target: Target) -> dict[str, Any]:
            try:
                await ctx.adb.install(target, apk, replace)
            except InstallError as exc:
                raise _install_failure(key, apk, exc) from exc
            return {"device_id": key, "apk": str(apk), "installed": True}

        return await device_call(ctx, key, op)

    async def uninstall_app(device_id: str, package: str) -> dict[str, Any]:
        """Uninstall the app `package` (e.g. com.example.app) from the device, with its data."""
        key = normalize_device_id(device_id)
        check_package(package)

        async def op(target: Target) -> dict[str, Any]:
            await ctx.adb.uninstall(target, package)
            return {"device_id": key, "package": package, "uninstalled": True}

        return await device_call(ctx, key, op)

    async def launch_app(device_id: str, package: str, activity: str | None = None) -> dict[str, Any]:
        """Start the app `package` (its launcher activity, or `activity` such as .MainActivity) and
        return the PID of its process. No PID means the app crashed at startup: check get_logs."""
        key = normalize_device_id(device_id)
        check_package(package)
        if activity is not None:
            _check_activity(activity)

        async def op(target: Target) -> dict[str, Any]:
            if activity is None:
                args = ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
                no_launcher = ToolError(
                    f"Aucune activité de lancement pour {package} sur {key} : vérifiez que l'app "
                    "est installée (install_apk) ou précisez `activity`."
                )
                try:
                    output = (await ctx.adb.shell(target, args)).text
                except (AdbTimeout, AdbNotFound):
                    raise
                except AdbError as exc:
                    # Shell protocol v2 (Android 7+): monkey reports on stderr and exits with -4.
                    if "No activities found" in str(exc):
                        raise no_launcher from exc
                    raise
                # Pre-v2 shell protocol: stderr is merged into stdout and the exit code is 0.
                if "No activities found" in output:
                    raise no_launcher
            else:
                args = ["am", "start", "-W", "-n", f"{package}/{activity}"]
                output = (await ctx.adb.shell(target, args)).text
                errors = [line.strip() for line in output.splitlines() if line.startswith("Error")]
                if errors:
                    raise ToolError(
                        f"Lancement de {package}/{activity} impossible sur {key} : {' '.join(errors)} "
                        "Vérifiez le nom de l'activité (elle doit être exportée) et que l'app est installée."
                    )
            for attempt in range(PID_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(PID_RETRY_DELAY_S)
                pid = await pidof(ctx, target, package)
                if pid is not None:
                    return {"pid": pid}
            raise ToolError(
                f"{package} lancé sur {key} mais l'app ne tourne pas (crash au démarrage ?), "
                "consultez get_logs."
            )

        return await device_call(ctx, key, op)

    async def stop_app(device_id: str, package: str) -> dict[str, Any]:
        """Force-stop the app `package` on the device (`am force-stop`)."""
        key = normalize_device_id(device_id)
        check_package(package)

        async def op(target: Target) -> dict[str, Any]:
            await ctx.adb.shell(target, ["am", "force-stop", package])
            return {"device_id": key, "package": package, "stopped": True}

        return await device_call(ctx, key, op)

    for fn in (install_apk, uninstall_app, launch_app, stop_app):
        server.add_tool(audited(ctx, fn))
