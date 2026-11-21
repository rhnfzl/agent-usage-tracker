import argparse
import json
import os
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_usage_tracker import setup_cli, setup_schedule
from agent_usage_tracker.config import AutError, load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key in ("AUT_CONFIG", "AUT_STATE", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def setup(*args):
    parser = argparse.ArgumentParser()
    setup_cli.register(parser.add_subparsers(required=True))
    setup_cli.run(parser.parse_args(["setup", *args]))


def test_preview_creates_nothing_and_receipt_keeps_three_states(home, capsys, monkeypatch):
    monkeypatch.setattr(setup_schedule.subprocess, "run", lambda *a, **kw: pytest.fail("scheduler"))
    setup("--json", "--schedule")
    report = json.loads(capsys.readouterr().out)
    assert report["configured"]["status"] == "missing"
    assert report["observed"]["scan_status"] == "not_observed"
    assert report["delivered"]["status"] == "not_configured"
    assert report["schedule"]["mode"] == "preview"
    assert not list(home.iterdir())


def test_apply_preserves_existing_config_and_uses_offline_defaults(home, capsys):
    setup("--apply", "--json")
    path = home / ".config/agent-usage-tracker/config.toml"
    assert path.exists()
    assert "cursor_api" not in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('machine = "my-machine"\n[transport]\npg_dsn = "private-value"\n')
    setup("--apply", "--json")
    output = capsys.readouterr().out.splitlines()[-1]
    assert "private-value" not in output
    report = json.loads(output)
    assert report["configured"]["action"] == "kept"
    assert report["delivered"]["status"] == "unverified"
    assert 'machine = "my-machine"' in path.read_text()


def test_apply_keeps_default_machine_identity_and_sources(home, monkeypatch):
    monkeypatch.setattr("agent_usage_tracker.config.socket.gethostname", lambda: "fixture-host.example")
    before = load_config()
    assert before.machine == "fixture-host"
    setup("--apply")
    after = load_config()
    assert (after.machine, after.agents) == (before.machine, before.agents)


def test_uninstall_can_remove_managed_timer_with_unreadable_config(home, monkeypatch, capsys):
    path = home / ".config/agent-usage-tracker/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[invalid", encoding="utf-8")
    calls = []

    def remove(action, executable):
        calls.append((action, executable))
        return "removed"

    monkeypatch.setattr(setup_schedule, "change", remove)
    setup("--uninstall", "--json")
    assert calls == [("uninstall", None)]
    report = json.loads(capsys.readouterr().out)
    assert report["schedule"]["status"] == "removed"
    assert report["configured"]["status"] == "unreadable"
    assert report["observed"]["scan_status"] == report["delivered"]["status"] == "unknown"
    assert path.read_text(encoding="utf-8") == "[invalid"


def test_launchd_install_and_remove_only_managed_files(home, monkeypatch):
    monkeypatch.setattr(setup_schedule, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(setup_schedule.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(setup_schedule.shutil, "which", lambda name: f"/bin/{name}")
    path = home / "Library/LaunchAgents/dev.aut.scan.plist"
    aut = home / "an aut/bin/aut"
    active = False
    calls = []

    def launchctl(argv, **kwargs):
        nonlocal active
        calls.append(argv[1:])
        action = argv[1]
        if action == "print":
            return subprocess.CompletedProcess(argv, 0 if active else 113, f"path = {path}", "")
        active = action == "bootstrap"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(setup_schedule.subprocess, "run", launchctl)
    path.parent.mkdir(parents=True)
    path.write_text("legacy launch agent")
    unrelated = path.with_name("dev.aut.retry.plist")
    unrelated.write_text("other job")
    with pytest.raises(AutError, match="existing"):
        setup_schedule.change("install", aut)
    with pytest.raises(AutError, match="existing"):
        setup_schedule.change("uninstall", None)
    assert calls == []
    assert path.read_text() == "legacy launch agent"
    path.unlink()
    assert setup_schedule.change("install", aut) == "installed"
    contents = path.read_bytes()
    assert plistlib.loads(contents)["ProgramArguments"] == [str(aut), "scan"]
    assert setup_schedule.change("install", home / "other/aut") == "already_installed"
    assert path.read_bytes() == contents
    assert setup_schedule.change("uninstall", None) == "removed"
    assert not path.exists() and unrelated.read_text() == "other job"
    assert calls.count(("bootstrap", "gui/501", str(path))) == 1
    assert ("bootout", "gui/501/dev.aut.scan") in calls


def test_systemd_does_not_touch_unowned_units(home, monkeypatch):
    monkeypatch.setattr(setup_schedule, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(setup_schedule.shutil, "which", lambda name: f"/bin/{name}")
    units = home / ".config/systemd/user"
    unit = units / "agent-usage-tracker.service"
    units.mkdir(parents=True)
    unit.write_text("existing unit")
    monkeypatch.setattr(setup_schedule.subprocess, "run", lambda *a, **kw: pytest.fail("systemd"))
    for action in ("install", "uninstall"):
        with pytest.raises(AutError, match="existing"):
            setup_schedule.change(action, home / "bin/aut")
    assert unit.read_text() == "existing unit"
    assert list(units.iterdir()) == [unit]


def test_systemd_install_and_remove_use_only_managed_units(home, monkeypatch):
    monkeypatch.setattr(setup_schedule, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(setup_schedule.shutil, "which", lambda name: f"/bin/{name}")
    units = home / ".config/systemd/user"
    calls = []

    def systemctl(argv, **kwargs):
        calls.append(argv[2:])
        unit = units / argv[-1]
        if argv[2] == "show":
            if unit.exists():
                return subprocess.CompletedProcess(
                    argv, 0, f"LoadState=loaded\nFragmentPath={unit}\n", "")
            return subprocess.CompletedProcess(argv, 1, "LoadState=not-found\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(setup_schedule.subprocess, "run", systemctl)
    aut = home / "some space%/aut"
    assert setup_schedule.change("install", aut) == "installed"
    service = units / "agent-usage-tracker.service"
    timer = units / "agent-usage-tracker.timer"
    escaped = str(aut).replace(chr(92), chr(92) * 2).replace("%", "%%")
    assert f'ExecStart="{escaped}" scan' in service.read_text()
    timer.write_text(timer.read_text().replace("10min", "20min"))
    assert setup_schedule.change("install", home / "unused") == "already_installed"
    assert "20min" in timer.read_text()
    assert setup_schedule.change("uninstall", None) == "removed"
    assert not list(units.iterdir())
    assert calls.count(("enable", "--now", timer.name)) == 1
    assert ("disable", "--now", timer.name) in calls
    assert ("stop", service.name) in calls


def test_windows_uses_packaged_task_script_without_force(home, monkeypatch):
    monkeypatch.setattr(setup_schedule, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(setup_schedule.shutil, "which", lambda name: f"/bin/{name}")

    def powershell(argv, **kwargs):
        assert argv[1:6] == ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File")
        script = Path(argv[6]).read_text()
        assert "-Force" not in script
        assert "Managed by aut setup" in script
        assert argv[7:10] == ("-Mode", "install", "-Managed")
        assert argv[10:] == ("-AutExecutable", str(home / "bin/aut.exe"))
        return subprocess.CompletedProcess(argv, 0, "installed\n", "")

    monkeypatch.setattr(setup_schedule.subprocess, "run", powershell)
    assert setup_schedule.change("install", home / "bin/aut.exe") == "installed"


def test_unsupported_platform_guides_manual_use(home, capsys, monkeypatch):
    monkeypatch.setattr(setup_schedule, "sys", SimpleNamespace(platform="freebsd"))
    setup("--schedule")
    assert setup_schedule.MANUAL in capsys.readouterr().out
    assert not list(home.iterdir())


def test_systemd_partial_install_can_retry_or_uninstall(home, monkeypatch):
    monkeypatch.setattr(setup_schedule, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(setup_schedule.shutil, "which", lambda name: f"/bin/{name}")
    units = home / ".config/systemd/user"
    service = units / "agent-usage-tracker.service"
    timer = units / "agent-usage-tracker.timer"
    attempt = 0

    def systemctl(argv, **kwargs):
        nonlocal attempt
        action = argv[2]
        if action == "show":
            return subprocess.CompletedProcess(argv, 1, "LoadState=not-found\n", "")
        if action == "enable":
            attempt += 1
            if attempt == 1:
                return subprocess.CompletedProcess(argv, 1, "", "startup failed")
        if action == "stop" or action == "disable" and "--now" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "unit not loaded")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(setup_schedule.subprocess, "run", systemctl)
    with pytest.raises(AutError, match="startup failed"):
        setup_schedule.change("install", home / "first/aut")
    first_service = service.read_bytes()
    assert setup_schedule.change("install", home / "second/aut") == "installed"
    assert service.read_bytes() == first_service
    assert timer.exists()
    assert setup_schedule.change("uninstall", None) == "removed"
    assert not list(units.iterdir())
