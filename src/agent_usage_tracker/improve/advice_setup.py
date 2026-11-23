"""Register small user-level hooks that call one persistent AUT executable."""

from __future__ import annotations

import json
import os
import shlex
import shutil
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..config import AutError
from ..state import _locked, atomic_write_text

_CLIENTS = {
    "claude": (".claude", "settings.json", ("claude",), ("PostToolBatch",)),
    "codex": (".codex", "hooks.json", ("codex",), ("PostToolUse",)),
    "cursor": (".cursor", "hooks.json", ("cursor", "cursor-agent", "agent"),
               ("postToolUse", "postToolUseFailure")),
}
CLIENTS = (*_CLIENTS, "pi")


def _home(home: Path, client: str, directory: str) -> Path:
    variable = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}.get(client)
    custom = os.getenv(variable) if variable else None
    return Path(custom).expanduser().absolute() if custom else home / directory


def _registration(client: str, executable: Path, mode: str) -> dict[str, Any]:
    command = shlex.join([str(executable), "advise", "hook", "--client", client, "--mode", mode])
    handler = {"type": "command", "command": command, "timeout": 5}
    if client == "cursor":
        return {**handler, "matcher": "^Shell$"}
    if client == "codex":
        return {"matcher": "^Bash$", "hooks": [handler]}
    return {"hooks": [handler]}


def _ours(item: object, client: str) -> bool:
    if not isinstance(item, dict):
        return False
    handlers = item.get("hooks") if client != "cursor" else [item]
    if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
        return False
    command = handlers[0].get("command")
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return (len(argv) == 7 and Path(argv[0]).name == "aut"
            and argv[1:6] == ["advise", "hook", "--client", client, "--mode"]
            and argv[6] in ("record", "show"))


def _update(path: Path, client: str, events: tuple[str, ...], entry: dict[str, Any],
            *, apply: bool) -> dict[str, Any]:
    if path.is_symlink():
        raise AutError("config is a symlink")
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original) if original is not None else {}
    if not isinstance(data, dict):
        raise AutError("config must contain a JSON object")
    if client == "cursor":
        if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
            raise AutError("unsupported Cursor hook config version")
        data["version"] = 1
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AutError("hooks must contain a JSON object")

    changed = False
    for event in events:
        items = hooks.get(event, [])
        if not isinstance(items, list):
            raise AutError("hook event must contain a JSON list")
        replacement = [item for item in items if not _ours(item, client)] + [entry]
        if items != replacement:
            hooks[event] = replacement
            changed = True
    if not changed:
        return {"status": "unchanged"}
    if not apply:
        return {"status": "would install"}
    backup = None
    if original is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.pre-aut-advice.{stamp}")
        atomic_write_text(backup, original.decode("utf-8"), newline="")
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {"status": "installed", **({"backup": str(backup)} if backup else {})}


def configure(home: Path, executable: Path, *, apply: bool, mode: str,
              clients: list[str] | None = None) -> list[dict[str, Any]]:
    result = []
    for client, (directory, filename, binaries, events) in _CLIENTS.items():
        config = _home(home, client, directory)
        if clients is not None and client not in clients:
            continue
        if clients is None and not config.is_dir() and not any(shutil.which(binary) for binary in binaries):
            if client != "cursor" or not Path("/Applications/Cursor.app").is_dir():
                continue
        path = config / filename
        entry = _registration(client, executable, mode)
        try:
            if apply:
                config.mkdir(mode=0o700, parents=True, exist_ok=True)
                with _locked(path.with_name("aut-advice-setup")):
                    report = _update(path, client, events, entry, apply=True)
            else:
                report = _update(path, client, events, entry, apply=False)
        except (AutError, OSError, ValueError):
            report = {"status": "unavailable", "reason": "config is unreadable or has an unsupported format"}
        result.append({"client": client, "path": str(path), **report})
    pi_home = Path(os.getenv("PI_CODING_AGENT_DIR") or home / ".pi" / "agent").expanduser()
    if (clients is not None and "pi" in clients
            or clients is None and (pi_home.is_dir() or shutil.which("pi"))):
        path = pi_home / "extensions" / "aut-session-advice.ts"
        try:
            template = files(__package__).joinpath("advice_pi.ts").read_text(encoding="utf-8")
            source = template.replace("__AUT_MODE__", json.dumps(mode)).replace(
                "__AUT_EXECUTABLE__", json.dumps(str(executable)))
            if apply:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with _locked(path):
                    status = _pi_update(path, source, apply=True)
            else:
                status = _pi_update(path, source, apply=False)
        except (AutError, OSError, ValueError):
            status = "unavailable"
        result.append({"client": "pi", "path": str(path), "status": status})
    return result


def _pi_update(path: Path, source: str, *, apply: bool) -> str:
    if path.is_symlink():
        raise AutError("extension is a symlink")
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if not current.startswith("// Managed by AUT session advice.\n"):
            raise AutError("extension is not managed by AUT")
        if current == source:
            return "unchanged"
    if not apply:
        return "would install"
    atomic_write_text(path, source)
    return "installed"
