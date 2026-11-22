"""Select one recorded tool argument by RFC 6901 JSON pointer."""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import AutError
from ..redact import redact


def parse_pointer(path: str) -> tuple[str, ...]:
    if len(path) > 500 or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        raise AutError("evidence: use a JSON pointer of up to 500 characters for the argument path")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:])


def argument_text(text: str, pointer: tuple[str, ...], *, redacted: bool) -> str | None:
    if not pointer:
        return text
    try:
        value: Any = json.loads(text)
        if redacted:
            # Decode escapes before masking while credential keys still surround their values.
            value = json.loads(redact(json.dumps(value, ensure_ascii=False)))
        for key in pointer:
            if isinstance(value, dict):
                value = value[key]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]{0,8}", key):
                value = value[int(key)]
            else:
                return None
        selected = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        selected.encode("utf-8")
        return selected
    except (ValueError, KeyError, IndexError, RecursionError):
        return None
