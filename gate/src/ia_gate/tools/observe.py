"""Observation and UI tools: logcat, screenshot, UI hierarchy dump and input events (spec §5).

Every user-supplied value is validated before any adb call; values sent to `adb shell` are
quoted by `AdbClient`. Log output is bounded by `policy.max_log_bytes`, keeping the most
recent lines.
"""

from __future__ import annotations

import re

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ia_gate.adb import Target
from ia_gate.context import GateContext, audited, device_call
from ia_gate.identity import normalize_device_id
from ia_gate.tools.apps import check_package, pidof

MIN_LOG_LINES = 1
MAX_LOG_LINES = 10_000
MAX_TEXT_CHARS = 500
UI_DUMP_PATH = "/data/local/tmp/ia-gate-ui.xml"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_KEY_RE = re.compile(r"KEYCODE_[A-Z0-9_]+|\d{1,3}", re.ASCII)


def truncate_tail(text: str, max_bytes: int) -> str:
    """Keep the last `max_bytes` bytes (UTF-8) of `text`, starting on a line boundary.

    When something is dropped, the result is prefixed with a notice giving the number of
    dropped bytes. A tail without any line boundary (one huge line) is cut mid-line, but never
    inside a multi-byte character.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    start = len(raw) - max(max_bytes, 0)
    if raw[start - 1 : start] != b"\n":
        newline = raw.find(b"\n", start)
        if newline != -1 and newline + 1 < len(raw):
            start = newline + 1
        else:
            while start < len(raw) and raw[start] & 0xC0 == 0x80:  # UTF-8 continuation byte
                start += 1
    kept = raw[start:].decode("utf-8", errors="replace")
    return f"[… {start} octets tronqués …]\n{kept}"


def _clamp_lines(lines: int) -> int:
    return max(MIN_LOG_LINES, min(lines, MAX_LOG_LINES))


def _check_coordinate(name: str, value: int) -> int:
    if value < 0:
        raise ValueError(
            f"Coordonnée {name} invalide : {value}. Indiquez des pixels positifs ou nuls "
            "(la taille de l'écran est donnée par device_info, les positions par ui_dump)."
        )
    return value


def _check_text(text: str) -> str:
    if not text:
        raise ValueError("Texte vide : indiquez le texte à saisir (500 caractères au plus).")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"Texte trop long ({len(text)} caractères, {MAX_TEXT_CHARS} au plus) : "
            "découpez-le en plusieurs appels à ui_text."
        )
    if any(not char.isprintable() and char != " " for char in text):
        raise ValueError(
            "Le texte contient un caractère de contrôle (retour ligne, tabulation…) : envoyez-le "
            "séparément avec ui_key (ex. KEYCODE_ENTER, KEYCODE_TAB)."
        )
    return text


def _check_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError(
            f"Touche invalide : {key!r}. Indiquez un nom KEYCODE_… en majuscules "
            "(ex. KEYCODE_HOME, KEYCODE_BACK, KEYCODE_ENTER) ou un code numérique (ex. 3)."
        )
    return key


def register(server: MCPServer, ctx: GateContext) -> None:
    async def get_logs(device_id: str, package: str | None = None, lines: int = 500) -> str:
        """Return the last `lines` logcat lines of the device (1 to 10000, threadtime format), only
        those of the running app `package` when given. Long output keeps the most recent lines."""
        key = normalize_device_id(device_id)
        if package is not None:
            check_package(package)
        count = _clamp_lines(lines)

        async def op(target: Target) -> str:
            args = ["logcat", "-d", "-v", "threadtime", "-t", str(count)]
            if package is not None:
                pid = await pidof(ctx, target, package)
                if pid is None:
                    raise ToolError(
                        f"{package} ne tourne pas sur {key} : lancez-la (launch_app) puis réessayez, "
                        "ou appelez get_logs sans `package` pour voir tous les logs (ex. un crash)."
                    )
                args.append(f"--pid={pid}")
            output = (await ctx.adb.shell(target, args)).text
            return truncate_tail(output, ctx.config.max_log_bytes)

        return await device_call(ctx, key, op)

    async def clear_logs(device_id: str) -> str:
        """Clear the logcat buffer of the device (`logcat -c`), e.g. before reproducing a bug."""
        key = normalize_device_id(device_id)

        async def op(target: Target) -> str:
            await ctx.adb.shell(target, ["logcat", "-c"])
            return f"Logs effacés sur {key}."

        return await device_call(ctx, key, op)

    async def screenshot(device_id: str) -> Image:
        """Take a screenshot of the device screen (PNG image)."""
        key = normalize_device_id(device_id)

        async def op(target: Target) -> Image:
            data = await ctx.adb.exec_out(target, ["screencap", "-p"])
            if not data.startswith(PNG_MAGIC):
                preview = data[:200].decode("utf-8", errors="replace").strip() or "aucune sortie"
                raise ToolError(
                    f"Capture d'écran de {key} invalide (pas une image PNG) : {preview}. "
                    "Vérifiez que l'écran est allumé et déverrouillé, puis réessayez."
                )
            return Image(data=data, format="png")

        return await device_call(ctx, key, op)

    async def ui_dump(device_id: str) -> str:
        """Return the XML hierarchy of the current screen (`uiautomator dump`): texts, resource ids
        and `bounds` of every element, to find where to tap with ui_tap."""
        key = normalize_device_id(device_id)

        async def op(target: Target) -> str:
            output = (await ctx.adb.shell(target, ["uiautomator", "dump", UI_DUMP_PATH])).text
            if "ERROR" in output:
                raise ToolError(
                    f"uiautomator dump a échoué sur {key} : {output.strip()}. Attendez que l'écran "
                    "soit stable (fin des animations, écran allumé) puis réessayez."
                )
            return (await ctx.adb.shell(target, ["cat", UI_DUMP_PATH])).text

        return await device_call(ctx, key, op)

    async def ui_tap(device_id: str, x: int, y: int) -> str:
        """Tap the screen at pixel (x, y) (`input tap`). Element positions come from ui_dump `bounds`."""
        key = normalize_device_id(device_id)
        _check_coordinate("x", x)
        _check_coordinate("y", y)

        async def op(target: Target) -> str:
            await ctx.adb.shell(target, ["input", "tap", str(x), str(y)])
            return f"Tap en ({x}, {y}) sur {key}."

        return await device_call(ctx, key, op)

    async def ui_text(device_id: str, text: str) -> str:
        """Type `text` (500 characters at most, single line) into the focused field (`input text`).
        Tap the field first with ui_tap; send Enter or Tab with ui_key."""
        key = normalize_device_id(device_id)
        _check_text(text)
        # `input text` turns `%s` into a space and cannot take a literal space.
        encoded = text.replace(" ", "%s")

        async def op(target: Target) -> str:
            await ctx.adb.shell(target, ["input", "text", encoded])
            return f"Texte saisi sur {key} ({len(text)} caractères)."

        return await device_call(ctx, key, op)

    async def ui_key(device_id: str, key: str) -> str:
        """Send a key event (`input keyevent`): a KEYCODE_ name such as KEYCODE_HOME, KEYCODE_BACK,
        KEYCODE_ENTER, or a numeric key code (e.g. 3)."""
        device_key = normalize_device_id(device_id)
        _check_key(key)

        async def op(target: Target) -> str:
            await ctx.adb.shell(target, ["input", "keyevent", key])
            return f"Touche {key} envoyée à {device_key}."

        return await device_call(ctx, device_key, op)

    for fn in (get_logs, clear_logs, screenshot, ui_dump, ui_tap, ui_text, ui_key):
        server.add_tool(audited(ctx, fn))
