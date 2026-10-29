"""`ia-gate` command line: serve, devices, enroll, token."""

from __future__ import annotations

import pytest
from conftest import NOW

import ia_gate.cli as cli
from ia_gate.identity import Fingerprint
from ia_gate.registry import DeviceRegistry

FP_A = Fingerprint("R58M12AAAAA", "aaaa1111", "samsung", "SM-A505F")
FP_B = Fingerprint("R58M12BBBBB", "bbbb2222", "Zebra", "TC52")


@pytest.fixture
def config_file(tmp_path):
    def write(server: str = "") -> str:
        path = tmp_path / "config.toml"
        path.write_text(
            f"[server]\n{server}\n"
            "[storage]\n"
            f'registry = "{(tmp_path / "state" / "devices.json").as_posix()}"\n'
            f'audit_log = "{(tmp_path / "state" / "audit.jsonl").as_posix()}"\n',
            encoding="utf-8",
        )
        return str(path)

    return write


@pytest.fixture
def no_uvicorn(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("uvicorn.run must not be called")

    monkeypatch.setattr(cli.uvicorn, "run", fail)


def test_cli_serve_http_lan_without_token_exits_2(config_file, monkeypatch, capsys, no_uvicorn):
    monkeypatch.delenv("IA_GATE_TOKEN", raising=False)
    path = config_file('host = "0.0.0.0"')
    assert cli.main(["serve", "--transport", "http", "--config", path]) == 2
    err = capsys.readouterr().err
    assert "IA_GATE_TOKEN" in err


def test_cli_invalid_config_exits_2(tmp_path, capsys, no_uvicorn):
    path = tmp_path / "config.toml"
    path.write_text("[server\n", encoding="utf-8")
    assert cli.main(["serve", "--transport", "http", "--config", str(path)]) == 2
    assert "TOML invalide" in capsys.readouterr().err


def test_cli_serve_http_runs_uvicorn_on_configured_address(config_file, monkeypatch):
    monkeypatch.setenv("IA_GATE_TOKEN", "x")
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    path = config_file('host = "0.0.0.0"\nport = 9000')
    assert cli.main(["serve", "--transport", "http", "--config", path]) == 0
    assert len(calls) == 1
    assert calls[0][1]["host"] == "0.0.0.0"
    assert calls[0][1]["port"] == 9000


def test_cli_token_prints_43_chars(capsys):
    assert cli.main(["token"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out) == 43
    assert cli.main(["token"]) == 0
    assert capsys.readouterr().out.strip() != out


def test_cli_devices_prints_table(config_file, tmp_path, bench, monkeypatch, capsys):
    monkeypatch.setattr(cli, "subprocess_runner", bench.run)
    DeviceRegistry(tmp_path / "state" / "devices.json").enroll("VAL-042", FP_A, "Caisse 1", NOW)
    bench.add("R58M12AAAAA", FP_A, marker="VAL-042")
    bench.add("192.168.1.50:5555", FP_B)

    assert cli.main(["devices", "--config", config_file()]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["DEVICE_ID", "STATE", "ADB_SERIAL", "MODEL", "LABEL"]
    val = next(line for line in lines if line.startswith("VAL-042"))
    assert val.split()[:4] == ["VAL-042", "online", "R58M12AAAAA", "SM-A505F"]
    assert "Caisse 1" in val
    unenrolled = next(line for line in lines if "192.168.1.50:5555" in line)
    assert unenrolled.split()[:4] == ["-", "online", "192.168.1.50:5555", "TC52"]


def test_cli_enroll_writes_registry(config_file, tmp_path, bench, monkeypatch, capsys):
    monkeypatch.setattr(cli, "subprocess_runner", bench.run)
    bench.add("R58M12AAAAA", FP_A)

    assert (
        cli.main(["enroll", "R58M12AAAAA", "val-042", "--label", "Caisse 1", "--config", config_file()]) == 0
    )
    assert "VAL-042" in capsys.readouterr().out
    record = DeviceRegistry(tmp_path / "state" / "devices.json").get("VAL-042")
    assert record is not None
    assert record.label == "Caisse 1"
    assert record.fingerprint == FP_A


def test_cli_enroll_unknown_serial_exits_1(config_file, bench, monkeypatch, capsys):
    monkeypatch.setattr(cli, "subprocess_runner", bench.run)
    bench.add("R58M12AAAAA", FP_A)

    assert cli.main(["enroll", "NOPE", "VAL-042", "--config", config_file()]) == 1
    err = capsys.readouterr().err
    assert "NOPE" in err


def test_cli_serve_http_wildcard_without_allowed_hosts_warns(config_file, monkeypatch, capsys):
    monkeypatch.setenv("IA_GATE_TOKEN", "x")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: None)
    assert cli.main(["serve", "--transport", "http", "--config", config_file('host = "0.0.0.0"')]) == 0
    assert "server.allowed_hosts" in capsys.readouterr().err

    path = config_file('host = "0.0.0.0"\nallowed_hosts = ["192.168.1.10"]')
    assert cli.main(["serve", "--transport", "http", "--config", path]) == 0
    assert capsys.readouterr().err == ""


def test_cli_serve_does_not_hide_unexpected_value_errors(config_file, monkeypatch):
    # Only the gate's own errors are user errors; a bug elsewhere keeps its traceback.
    def broken(app, **kwargs):
        raise ValueError("bug in uvicorn")

    monkeypatch.setattr(cli.uvicorn, "run", broken)
    with pytest.raises(ValueError, match="bug in uvicorn"):
        cli.main(["serve", "--transport", "http", "--config", config_file()])
