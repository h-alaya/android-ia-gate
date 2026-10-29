"""Gate configuration loaded from `~/.ia-gate/config.toml` (spec §7)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.ia-gate/config.toml")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

DEFAULT_SHELL_ALLOWLIST = (
    "getprop",
    "pm list",
    "pm path",
    "dumpsys",
    "settings get",
    "ls",
    "ps",
    "df",
    "wm size",
    "ip addr",
)


class ConfigError(Exception):
    """Invalid or unsafe gate configuration."""


@dataclass(frozen=True)
class GateConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "IA_GATE_TOKEN"
    adb_path: str = "adb"
    adb_timeout_s: float = 120
    registry_path: Path = Path("~/.ia-gate/devices.json").expanduser()
    audit_log_path: Path = Path("~/.ia-gate/audit.jsonl").expanduser()
    apk_dirs: tuple[Path, ...] = (Path("~/ia-gate-apks").expanduser(),)
    shell_allowlist: tuple[str, ...] = DEFAULT_SHELL_ALLOWLIST
    max_log_bytes: int = 200_000

    def token(self) -> str | None:
        """Return the bearer token from the environment, or None if unset/empty."""
        value = os.environ.get(self.token_env, "")
        return value or None


def _expand(raw: str) -> Path:
    return Path(raw).expanduser()


def _get(data: dict[str, Any], section: str, key: str, types: type | tuple[type, ...], default: Any) -> Any:
    table = data.get(section, {})
    if not isinstance(table, dict):
        raise ConfigError(
            f"Configuration invalide : [{section}] doit être une table TOML. "
            f"Corrigez le fichier de configuration."
        )
    if key not in table:
        return default
    value = table[key]
    # bool is a subclass of int: reject it explicitly for numeric settings.
    if isinstance(value, bool) or not isinstance(value, types):
        raise ConfigError(
            f"Configuration invalide : {section}.{key} = {value!r} n'a pas le bon type. "
            f"Corrigez le fichier de configuration."
        )
    return value


def _str_list(
    data: dict[str, Any], section: str, key: str, default: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    value = _get(data, section, key, list, None)
    if value is None:
        return default
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f"Configuration invalide : {section}.{key} doit être une liste de chaînes. "
            f"Corrigez le fichier de configuration."
        )
    return tuple(value)


def load_config(path: Path | None = None) -> GateConfig:
    """Load the gate configuration; a missing file yields the defaults. Unknown keys are ignored."""
    cfg_path = (path if path is not None else DEFAULT_CONFIG_PATH).expanduser()
    if not cfg_path.exists():
        return GateConfig()
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Fichier de configuration {cfg_path} illisible (TOML invalide : {exc}). "
            f"Corrigez-le ou supprimez-le pour revenir aux valeurs par défaut."
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Impossible de lire le fichier de configuration {cfg_path} : {exc}. Vérifiez ses permissions."
        ) from exc

    d = GateConfig()
    apk_dirs = _str_list(data, "policy", "apk_dirs", None)
    return GateConfig(
        host=_get(data, "server", "host", str, d.host),
        port=_get(data, "server", "port", int, d.port),
        token_env=_get(data, "server", "token_env", str, d.token_env),
        adb_path=_get(data, "adb", "path", str, d.adb_path),
        adb_timeout_s=float(_get(data, "adb", "timeout_s", (int, float), d.adb_timeout_s)),
        registry_path=_expand(_get(data, "storage", "registry", str, str(d.registry_path))),
        audit_log_path=_expand(_get(data, "storage", "audit_log", str, str(d.audit_log_path))),
        apk_dirs=tuple(_expand(p) for p in apk_dirs) if apk_dirs is not None else d.apk_dirs,
        shell_allowlist=_str_list(data, "policy", "shell_allowlist", d.shell_allowlist),
        max_log_bytes=_get(data, "policy", "max_log_bytes", int, d.max_log_bytes),
    )


def validate_for_http(cfg: GateConfig) -> None:
    """Refuse a non-loopback bind when no bearer token is configured (spec §6)."""
    if cfg.host in LOOPBACK_HOSTS:
        return
    if cfg.token() is None:
        raise ConfigError(
            f"Écoute sur {cfg.host} refusée : aucun token configuré. "
            f"Définissez la variable d'environnement {cfg.token_env} "
            f'(générez-en un avec `ia-gate token`) ou utilisez host = "127.0.0.1".'
        )
