import pytest
from conftest import NOW

from ia_gate.identity import Fingerprint
from ia_gate.tools import apps
from ia_gate.tools.apps import check_apk_path

FP_A = Fingerprint("R58M12AAAAA", "aaaa1111", "samsung", "SM-A505F")
PKG = "com.example.app"


def text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def data(result):
    structured = result.structured_content
    assert structured is not None, text(result)
    return structured["result"] if set(structured) == {"result"} else structured


@pytest.fixture
def apk_dir(cfg):
    [directory] = cfg.apk_dirs
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def device(ctx, bench):
    ctx.registry.enroll("VAL-042", FP_A, "", NOW)
    bench.add("R58A", FP_A, marker="VAL-042")
    return "R58A"


@pytest.fixture(autouse=True)
def no_pid_retry_delay(monkeypatch):
    monkeypatch.setattr(apps, "PID_RETRY_DELAY_S", 0)


def install_calls(fake):
    return [call for call in fake.calls if "install" in call]


def assert_refused_without_adb(result, fake):
    assert result.is_error
    assert install_calls(fake) == []
    assert fake.calls == []


async def test_install_ok(client, fake, apk_dir, device):
    apk = apk_dir / "app-debug.apk"
    apk.write_bytes(b"PK")
    fake.on(["install"], stdout="Performing Streamed Install\nSuccess\n", serial=device)

    result = await client.call_tool("install_apk", {"device_id": "val-042", "apk_path": str(apk)})

    assert not result.is_error, text(result)
    resolved = apk.resolve()
    assert data(result) == {"device_id": "VAL-042", "apk": str(resolved), "installed": True}
    assert ["adb", "-s", device, "install", "-r", str(resolved)] in fake.calls


async def test_install_without_replace(client, fake, apk_dir, device):
    apk = apk_dir / "app.apk"
    apk.write_bytes(b"PK")
    fake.on(["install"], stdout="Success\n", serial=device)

    result = await client.call_tool(
        "install_apk", {"device_id": "VAL-042", "apk_path": str(apk), "replace": False}
    )

    assert not result.is_error, text(result)
    assert install_calls(fake) == [["adb", "-s", device, "install", str(apk.resolve())]]


async def test_install_outside_apk_dirs_refused_without_adb_call(client, fake, tmp_path, apk_dir, device):
    outside = tmp_path / "elsewhere" / "app.apk"
    outside.parent.mkdir()
    outside.write_bytes(b"PK")

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(outside)})

    assert_refused_without_adb(result, fake)
    assert str(apk_dir.resolve()) in text(result)


async def test_install_sibling_dir_with_same_prefix_refused(client, fake, tmp_path, apk_dir, device):
    sibling = tmp_path / "apks-evil" / "app.apk"
    sibling.parent.mkdir()
    sibling.write_bytes(b"PK")

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(sibling)})

    assert_refused_without_adb(result, fake)


async def test_install_dotdot_traversal_refused(client, fake, tmp_path, apk_dir, device):
    outside = tmp_path / "secret" / "app.apk"
    outside.parent.mkdir()
    outside.write_bytes(b"PK")
    raw = f"{apk_dir}/../secret/app.apk"

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": raw})

    assert_refused_without_adb(result, fake)


async def test_install_symlink_escape_refused(client, fake, tmp_path, apk_dir, device):
    outside = tmp_path / "secret" / "app.apk"
    outside.parent.mkdir()
    outside.write_bytes(b"PK")
    link = apk_dir / "link.apk"
    link.symlink_to(outside)

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(link)})

    assert_refused_without_adb(result, fake)


async def test_install_non_apk_suffix_refused(client, fake, apk_dir, device):
    not_apk = apk_dir / "app.txt"
    not_apk.write_bytes(b"PK")

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(not_apk)})

    assert_refused_without_adb(result, fake)
    assert ".apk" in text(result)


async def test_install_directory_named_apk_refused(client, fake, apk_dir, device):
    directory = apk_dir / "dir.apk"
    directory.mkdir()

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(directory)})

    assert_refused_without_adb(result, fake)


async def test_install_missing_file_refused(client, fake, apk_dir, device):
    missing = apk_dir / "missing.apk"

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(missing)})

    assert_refused_without_adb(result, fake)
    assert "introuvable" in text(result)


async def test_install_incompatible_gives_advice(client, fake, apk_dir, device):
    apk = apk_dir / "app.apk"
    apk.write_bytes(b"PK")
    fake.on(
        ["install"],
        stdout="Performing Streamed Install\n",
        stderr="adb: failed to install app.apk: Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: "
        "Package com.example.app signatures do not match previously installed version]\n",
        returncode=1,
        serial=device,
    )

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(apk)})

    assert result.is_error
    assert "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in text(result)
    assert "désinstallez d'abord (uninstall_app)" in text(result)


async def test_install_downgrade_gives_advice(client, fake, apk_dir, device):
    apk = apk_dir / "app.apk"
    apk.write_bytes(b"PK")
    fake.on(
        ["install"],
        stderr="adb: failed to install app.apk: Failure [INSTALL_FAILED_VERSION_DOWNGRADE]\n",
        returncode=1,
        serial=device,
    )

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(apk)})

    assert result.is_error
    assert "INSTALL_FAILED_VERSION_DOWNGRADE" in text(result)
    assert "version inférieure à celle installée" in text(result)


async def test_install_other_failure_shows_raw_code(client, fake, apk_dir, device):
    apk = apk_dir / "app.apk"
    apk.write_bytes(b"PK")
    fake.on(
        ["install"], stdout="Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]\n", returncode=1, serial=device
    )

    result = await client.call_tool("install_apk", {"device_id": "VAL-042", "apk_path": str(apk)})

    assert result.is_error
    assert "INSTALL_FAILED_INSUFFICIENT_STORAGE" in text(result)
    assert "VAL-042" in text(result)


def test_check_apk_path_accepts_allowed_dir_behind_symlink(tmp_path):
    real = tmp_path / "share"
    real.mkdir()
    (real / "app.apk").write_bytes(b"PK")
    link_dir = tmp_path / "apks"
    link_dir.symlink_to(real)

    assert check_apk_path(str(link_dir / "app.apk"), [link_dir]) == (real / "app.apk").resolve()


def test_check_apk_path_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    apk_dir = tmp_path / "ia-gate-apks"
    apk_dir.mkdir()
    (apk_dir / "app.apk").write_bytes(b"PK")

    assert check_apk_path("~/ia-gate-apks/app.apk", [apk_dir]) == (apk_dir / "app.apk").resolve()


def test_check_apk_path_error_lists_allowed_dirs(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    outside = tmp_path / "x.apk"
    outside.write_bytes(b"PK")

    with pytest.raises(ValueError) as excinfo:
        check_apk_path(str(outside), [first, second])

    assert str(first.resolve()) in str(excinfo.value)
    assert str(second.resolve()) in str(excinfo.value)


async def test_uninstall_ok(client, fake, device):
    fake.on(["uninstall", PKG], stdout="Success\n", serial=device)

    result = await client.call_tool("uninstall_app", {"device_id": "VAL-042", "package": PKG})

    assert not result.is_error, text(result)
    assert data(result) == {"device_id": "VAL-042", "package": PKG, "uninstalled": True}
    assert ["adb", "-s", device, "uninstall", PKG] in fake.calls


async def test_launch_returns_pid(client, fake, device):
    fake.on(["shell", "monkey"], stdout="Events injected: 1\n", serial=device)
    fake.on(["shell", "pidof", PKG], stdout="4242\n", serial=device)

    result = await client.call_tool("launch_app", {"device_id": "VAL-042", "package": PKG})

    assert not result.is_error, text(result)
    assert data(result) == {"pid": 4242}
    monkey = [
        "adb",
        "-s",
        device,
        "shell",
        "monkey",
        "-p",
        PKG,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    ]
    assert monkey in fake.calls


async def test_launch_with_activity_uses_am_start(client, fake, device):
    fake.on(["shell", "am", "start"], stdout="Status: ok\nActivity: com.example.app/.Main\n", serial=device)
    fake.on(["shell", "pidof", PKG], stdout="4243\n", serial=device)

    result = await client.call_tool(
        "launch_app", {"device_id": "VAL-042", "package": PKG, "activity": ".MainActivity"}
    )

    assert not result.is_error, text(result)
    assert data(result) == {"pid": 4243}
    assert ["adb", "-s", device, "shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity"] in fake.calls
    assert not any("monkey" in call for call in fake.calls)


async def test_launch_am_start_error_is_tool_error(client, fake, device):
    fake.on(
        ["shell", "am", "start"],
        stdout="Starting: Intent { cmp=com.example.app/.Nope }\n"
        "Error type 3\nError: Activity class {com.example.app/com.example.app.Nope} does not exist.\n",
        serial=device,
    )

    result = await client.call_tool(
        "launch_app", {"device_id": "VAL-042", "package": PKG, "activity": ".Nope"}
    )

    assert result.is_error
    assert "does not exist" in text(result)
    assert not any("pidof" in call for call in fake.calls)


async def test_launch_monkey_without_launcher_activity(client, fake, device):
    # adb shell protocol v2 (Android 7+): monkey writes to stderr and exits with -4 (252).
    fake.on(
        ["shell", "monkey"],
        returncode=252,
        stderr="** No activities found to run, monkey aborted.\n",
        serial=device,
    )

    result = await client.call_tool("launch_app", {"device_id": "VAL-042", "package": PKG})

    assert result.is_error
    assert "Aucune activité de lancement" in text(result)
    assert "install_apk" in text(result)
    assert "`activity`" in text(result)
    assert not any("pidof" in call for call in fake.calls)


async def test_launch_monkey_without_launcher_activity_old_device(client, fake, device):
    # Pre-v2 shell protocol: stderr is merged into stdout and the exit code is always 0.
    fake.on(["shell", "monkey"], stdout="** No activities found to run, monkey aborted.\n", serial=device)

    result = await client.call_tool("launch_app", {"device_id": "VAL-042", "package": PKG})

    assert result.is_error
    assert "Aucune activité de lancement" in text(result)
    assert "`activity`" in text(result)
    assert not any("pidof" in call for call in fake.calls)


async def test_launch_monkey_other_failure_is_reported(client, fake, device):
    fake.on(["shell", "monkey"], returncode=1, stderr="monkey: permission denied\n", serial=device)

    result = await client.call_tool("launch_app", {"device_id": "VAL-042", "package": PKG})

    assert result.is_error
    assert "permission denied" in text(result)
    assert "Aucune activité de lancement" not in text(result)
    assert not any("pidof" in call for call in fake.calls)


async def test_launch_no_pid_suggests_logs(client, fake, device):
    fake.on(["shell", "monkey"], stdout="Events injected: 1\n", serial=device)
    fake.on(["shell", "pidof", PKG], returncode=1, serial=device)

    result = await client.call_tool("launch_app", {"device_id": "VAL-042", "package": PKG})

    assert result.is_error
    assert "l'app ne tourne pas (crash au démarrage ?), consultez get_logs" in text(result)


async def test_launch_retries_pidof_until_process_appears(client, ctx, fake, device):
    fake.on(["shell", "monkey"], stdout="Events injected: 1\n", serial=device)
    fake.on(["shell", "pidof", PKG], returncode=1, serial=device)
    calls = 0
    original = ctx.adb._runner

    async def runner(args, timeout):
        nonlocal calls
        if "pidof" in args:
            calls += 1
            if calls == 2:
                fake.on(["shell", "pidof", PKG], stdout="77\n", serial=device)
        return await original(args, timeout)

    ctx.adb._runner = runner

    result = await client.call_tool("launch_app", {"device_id": "VAL-042", "package": PKG})

    assert not result.is_error, text(result)
    assert data(result) == {"pid": 77}
    assert calls == 2


async def test_stop_app(client, fake, device):
    fake.on(["shell", "am", "force-stop", PKG], serial=device)

    result = await client.call_tool("stop_app", {"device_id": "VAL-042", "package": PKG})

    assert not result.is_error, text(result)
    assert data(result) == {"device_id": "VAL-042", "package": PKG, "stopped": True}
    assert ["adb", "-s", device, "shell", "am", "force-stop", PKG] in fake.calls


@pytest.mark.parametrize(
    ("tool", "package"),
    [
        ("uninstall_app", "com.example;reboot"),
        ("uninstall_app", "noDots"),
        ("launch_app", "com.example.app $(id)"),
        ("launch_app", "1com.example"),
        ("stop_app", "com..example"),
        ("stop_app", "-p com.example"),
    ],
)
async def test_invalid_package_name_refused(client, fake, device, tool, package):
    result = await client.call_tool(tool, {"device_id": "VAL-042", "package": package})

    assert result.is_error
    assert "Nom de package invalide" in text(result)
    assert fake.calls == []


@pytest.mark.parametrize("activity", ["com.example.app/.Main", ".Main; reboot", "", "-a"])
async def test_invalid_activity_refused(client, fake, device, activity):
    result = await client.call_tool(
        "launch_app", {"device_id": "VAL-042", "package": PKG, "activity": activity}
    )

    assert result.is_error
    assert "Activité invalide" in text(result)
    assert fake.calls == []
