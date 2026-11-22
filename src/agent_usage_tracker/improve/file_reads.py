"""Keep bounded evidence of exact file contents returned by a native tool."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

_LITERAL_PATH = re.compile(r"[/\.\w][-/\. \w]*")
_MAX_OUTPUT_BYTES = 65536


def _command_words(command: object) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command) if "\n" not in command and "\r" not in command else []
    if isinstance(command, list) and all(isinstance(word, str) for word in command):
        return command
    return []


def path_fingerprint(path: Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(path)))
    return hashlib.sha256(os.fsencode(normalized)).hexdigest()


def capture_file_read(item: dict[str, Any]) -> dict[str, str] | None:
    """Recognize a completed single-file cat using the recorded working directory."""
    output, cwd = item.get("aggregated_output"), item.get("cwd")
    if (type(item.get("exit_code")) is not int or item["exit_code"] != 0
            or item.get("status") not in (None, "completed")
            or not isinstance(output, str) or len(output) >= _MAX_OUTPUT_BYTES
            or not isinstance(cwd, str)):
        return None
    try:
        if cwd.startswith("file:"):
            uri = urlsplit(cwd)
            if uri.netloc or uri.query or uri.fragment:
                return None
            cwd = url2pathname(uri.path)
        workspace = Path(cwd)
        if not workspace.is_absolute() or ".." in workspace.parts or "\0" in cwd:
            return None
        words = _command_words(item.get("command"))
        for _ in range(2):
            if (len(words) != 3 or Path(words[0]).name not in {"sh", "bash", "zsh"}
                    or words[1] not in {"-c", "-lc"}):
                break
            words = _command_words(words[2])
        if len(words) not in (2, 3) or words[0] not in {"cat", "/bin/cat", "/usr/bin/cat"}:
            return None
        if (len(words) == 3 and words[1] != "--") or not _LITERAL_PATH.fullmatch(words[-1]):
            return None
        if ".." in Path(words[-1]).parts:
            return None
        raw = output.encode("utf-8")
        if len(raw) >= _MAX_OUTPUT_BYTES:
            return None
        return {"path_sha256": path_fingerprint(workspace / words[-1]),
                "output_sha256": hashlib.sha256(raw).hexdigest()}
    except (ValueError, UnicodeError):
        return None


def observed_file_read(source: dict[str, Any], end_id: str, turn: str, target: Path,
                       expected: str) -> dict[str, str] | None:
    from .evidence import event_links

    if (source["agent"] != "codex" or source.get("origin") != "interactive"
            or any(source["coverage"].get(key) for key in (
                "malformed_records", "missing_timestamps", "unknown_copy_boundary", "divergent_copy",
            ))):
        return None
    events = [event for event in source["events"] if event.get("native_turn_key") == turn]
    boundaries = [event for event in events if event["kind"] == "boundary"]
    if (len(boundaries) != 1 or not events or events[-1]["id"] != end_id
            or boundaries[0]["id"] != end_id or boundaries[0]["text"] != "turn_complete"
            or boundaries[0].get("turn_context", {}).get("status") != "observed"):
        return None
    by_id = {event["id"]: event for event in events}
    expected_receipt = {"path_sha256": path_fingerprint(target), "output_sha256": expected}
    for call, result in event_links(events).items():
        if (by_id[call].get("tool_representation") == "execution_item"
                and not by_id[call].get("tool_event_ambiguous")
                and not by_id[result].get("tool_event_ambiguous")
                and by_id[result].get("status") == "success"
                and by_id[result].get("native_file_read") == expected_receipt):
            return {"call_event_id": call, "result_event_id": result}
    return None
