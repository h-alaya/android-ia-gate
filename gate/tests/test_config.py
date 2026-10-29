from pathlib import Path

import pytest

from ia_gate.config import ConfigError, load_config, validate_for_http


def test_defaults_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "absent.toml")
    assert (cfg.host, cfg.port, cfg.token_env) == ("127.0.0.1", 8765, "IA_GATE_TOKEN")
    assert cfg.adb_path == "adb" and cfg.adb_timeout_s == 120
    assert cfg.max_log_bytes == 200_000
    assert "pm list" in cfg.shell_allowlist and cfg.registry_path == Path.home() / ".ia-gate/devices.json"


def test_file_overrides_and_tilde_expansion(tmp_path):
    (tmp_path / "c.toml").write_text('[server]\nport = 9000\n[policy]\napk_dirs = ["~/apks"]\n')
    cfg = load_config(tmp_path / "c.toml")
    assert cfg.port == 9000 and cfg.apk_dirs == (Path.home() / "apks",)


def test_lan_bind_without_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_GATE_TOKEN", raising=False)
    (tmp_path / "c.toml").write_text('[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(ConfigError, match="token"):
        validate_for_http(load_config(tmp_path / "c.toml"))


def test_lan_bind_with_token_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_GATE_TOKEN", "s3cret")
    (tmp_path / "c.toml").write_text('[server]\nhost = "0.0.0.0"\n')
    validate_for_http(load_config(tmp_path / "c.toml"))  # no raise


def test_allowed_hosts_loaded_from_server_section(tmp_path):
    toml = '[server]\nallowed_hosts = ["192.168.1.10", "gatepc.local", "fe80::1"]\n'
    (tmp_path / "c.toml").write_text(toml)
    cfg = load_config(tmp_path / "c.toml")
    assert cfg.allowed_hosts == ("192.168.1.10", "gatepc.local", "fe80::1")
    assert load_config(tmp_path / "absent.toml").allowed_hosts == ()


@pytest.mark.parametrize(
    "entry", ["", "0.0.0.0", "gatepc.local:8765", "http://gatepc", "gate pc", "*", "*.local", "a,b", "-bad"]
)
def test_allowed_hosts_invalid_entry_refused(tmp_path, entry):
    (tmp_path / "c.toml").write_text(f'[server]\nallowed_hosts = ["{entry}"]\n')
    with pytest.raises(ConfigError, match="server.allowed_hosts"):
        load_config(tmp_path / "c.toml")


def test_allowed_hosts_normalized(tmp_path):
    (tmp_path / "c.toml").write_text('[server]\nallowed_hosts = ["GatePC.local", "FE80::0001"]\n')
    assert load_config(tmp_path / "c.toml").allowed_hosts == ("gatepc.local", "fe80::1")


def test_utf8_bom_is_accepted(tmp_path):
    (tmp_path / "c.toml").write_bytes(b"\xef\xbb\xbf[server]\nport = 9000\n")  # Windows Notepad
    assert load_config(tmp_path / "c.toml").port == 9000


def test_non_utf8_file_is_config_error(tmp_path):
    (tmp_path / "c.toml").write_bytes('[policy]\napk_dirs = ["C:/Users/hélène"]\n'.encode("cp1252"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(tmp_path / "c.toml")


def test_explicit_missing_path_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(tmp_path / "typo.tml", explicit=True)


@pytest.mark.parametrize(
    "toml",
    ["[server]\nport = 0\n", "[server]\nport = 70000\n", "[adb]\ntimeout_s = 0\n",
     "[adb]\ntimeout_s = -1\n", "[adb]\ntimeout_s = nan\n", "[policy]\nmax_log_bytes = 0\n"],
)
def test_out_of_range_numbers_refused(tmp_path, toml):
    (tmp_path / "c.toml").write_text(toml)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "c.toml")
