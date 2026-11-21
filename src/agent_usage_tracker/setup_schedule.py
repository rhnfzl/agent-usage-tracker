"""Manage the per-user scan job from the shipped scheduler templates."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

from .config import AutError
from .state import _locked, atomic_write_text

_MANAGED = "Managed by aut setup"
_LABEL = "dev.aut.scan"
_UNIT = "agent-usage-tracker"
_TEMPLATES = {"darwin": "launchd", "linux": "systemd", "win32": "windows"}
_MANAGERS = {"darwin": "launchctl", "linux": "systemctl", "win32": "powershell.exe"}
MANUAL = "Run aut summary on demand, or schedule aut scan yourself after configuring a sink."


def manager() -> str | None:
    return _MANAGERS.get(sys.platform)


def _template(name: str) -> Traversable:
    asset = files("agent_usage_tracker").joinpath("setup_assets", name)
    if asset.is_file():
        return asset
    return Path(__file__).resolve().parents[2] / "packaging" / _TEMPLATES[sys.platform] / name


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        raise AutError(f"aut setup: {Path(argv[0]).name} timed out. Retry the same command.") from None


def _check(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise AutError(f"aut setup: scheduler exit {result.returncode}: {detail}")


def _owned(path: Path) -> bool:
    if not path.is_symlink() and not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise AutError(f"aut setup: existing scheduler entry was kept: {path}")
    markers = {f"# {_MANAGED}", f"<!-- {_MANAGED} -->"}
    with path.open(encoding="utf-8", errors="replace") as entry:
        if markers.isdisjoint(entry.read(256).splitlines()):
            raise AutError(f"aut setup: existing scheduler entry was kept: {path}")
    return True


def _launchd(executable: str, action: str, aut: Path | None) -> str:
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{_LABEL}"
    path = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
    owned = _owned(path)
    if action == "uninstall" and not owned:
        return "absent"
    status = _run(executable, "print", service)
    if status.returncode != 113:
        _check(status)
        if not owned or f"path = {path}" not in map(str.strip, status.stdout.splitlines()):
            raise AutError("aut setup: existing launchd job was kept")
    active = status.returncode == 0
    if action == "uninstall":
        if active:
            _check(_run(executable, "bootout", service))
        path.unlink()
        return "removed"
    if active:
        return "already_installed"
    if not owned:
        settings = plistlib.loads(_template(path.name).read_bytes())
        settings["ProgramArguments"][0] = str(aut)
        settings["StandardOutPath"] = str(path.parent.parent / "Logs" / "aut-scan.log")
        settings["StandardErrorPath"] = str(path.parent.parent / "Logs" / "aut-scan.error.log")
        (path.parent.parent / "Logs").mkdir(parents=True, exist_ok=True)
        xml = plistlib.dumps(settings).decode()
        xml = xml.replace("<plist", f"<!-- {_MANAGED} -->\n<plist", 1)
        atomic_write_text(path, xml)
    _check(_run(executable, "bootstrap", domain, str(path)))
    return "installed"


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
        "%", "%%").replace("$", "$$") + '"'


def _systemd(executable: str, action: str, aut: Path | None) -> str:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    service, timer = (base / f"{_UNIT}.{extension}" for extension in ("service", "timer"))
    has_service, has_timer = _owned(service), _owned(timer)
    if action == "uninstall" and not (has_service or has_timer):
        return "absent"
    cmd = (executable, "--user")
    for path, owned in ((service, has_service), (timer, has_timer)):
        result = _run(*cmd, "show", "--property=LoadState,FragmentPath", path.name)
        info = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if info.get("LoadState") == "not-found":
            continue
        _check(result)
        if not owned or info.get("FragmentPath") != str(path):
            raise AutError(f"aut setup: existing systemd unit was kept: {path.name}")
    if action == "uninstall":
        if has_timer:
            _check(_run(*cmd, "disable", "--now", timer.name))
        if has_service:
            _check(_run(*cmd, "stop", service.name))
        if has_timer:
            timer.unlink()
        if has_service:
            service.unlink()
        _check(_run(*cmd, "daemon-reload"))
        return "removed"
    if has_service and has_timer and not _run(*cmd, "is-active", "--quiet", timer.name).returncode:
        return "already_installed"
    if not has_service:
        template = _template(service.name).read_text(encoding="utf-8")
        body = template.replace("%h/.local/bin/aut", _systemd_quote(str(aut)), 1)
        atomic_write_text(service, f"# {_MANAGED}\n{body}")
    if not has_timer:
        body = _template(timer.name).read_text(encoding="utf-8")
        atomic_write_text(timer, f"# {_MANAGED}\n{body}")
    _check(_run(*cmd, "enable", "--now", timer.name))
    return "installed"


def change(action: str, aut: Path | None) -> str:
    name = manager()
    executable = shutil.which(name) if name else None
    if not executable:
        return "unavailable"
    if action == "install" and aut is None:
        raise AutError("aut setup: install aut as a tool on PATH before adding a timer")
    if aut is not None and any(ord(char) < 32 for char in str(aut)):
        raise AutError("aut setup: scheduler executable path contains a control character")
    with _locked(Path.home() / ".config" / "agent-usage-tracker" / "setup-timer"):
        if sys.platform == "darwin":
            return _launchd(executable, action, aut)
        if sys.platform == "linux":
            return _systemd(executable, action, aut)
        with as_file(_template("register-task.ps1")) as script:
            argv = [executable, "-NoProfile", "-NonInteractive", "-File", str(script),
                    "-Mode", action, "-Managed"]
            if aut is not None:
                argv.extend(("-AutExecutable", str(aut)))
            result = _run(*argv)
            _check(result)
            return result.stdout.strip()
