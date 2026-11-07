"""Keep scripted Cursor requests out of human activity by registering before sending."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import TextIO

from .config import AutError
from .state import _locked, atomic_write_text, state_path

_UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")


def registry_path() -> Path:
    return state_path().with_name("cursor-automated.json")


def automated_sessions() -> set[str]:
    """Read trusted local registrations. Missing is empty; unreadable or corrupt is an error."""
    path = registry_path()
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise AutError("aut: restrict cursor-automated.json to its owner (mode 0600)")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        raise AutError("aut: Cursor origin registry is unreadable; it was not reset") from exc
    if (not isinstance(value, dict) or set(value) != {"version", "sessions"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["sessions"], list)
            or any(not isinstance(s, str) or not _UUID.fullmatch(s) for s in value["sessions"])):
        raise AutError("aut: Cursor origin registry is invalid; it was not reset")
    return set(value["sessions"])


def run(model: str, workspace: str, prompt: TextIO) -> None:
    """Create an empty native chat, persist its origin, then send the first request."""
    if not model.strip() or not workspace.strip():
        raise AutError("aut cursor-run: model and workspace must be explicit nonempty values")
    try:
        created = subprocess.run(["cursor-agent", "--workspace", workspace, "create-chat"],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        sid = created.stdout.strip()
        if created.returncode or not _UUID.fullmatch(sid):
            raise AutError("aut cursor-run: could not create an empty chat; no prompt was sent")
        path = registry_path()
        with _locked(path):
            registered = automated_sessions()
            registered.add(sid)
            atomic_write_text(path, json.dumps({"version": 1, "sessions": sorted(registered)}))
        result = subprocess.run(["cursor-agent", "--resume", sid, "--print", "--mode", "ask",
                                 "--trust", "--model", model, "--output-format", "json",
                                 "--workspace", workspace], input=prompt.read(), text=True)
        if result.returncode:
            raise AutError("aut cursor-run: Cursor failed; its automated origin is retained")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutError("aut cursor-run: could not finish; any registered origin is retained") from exc
