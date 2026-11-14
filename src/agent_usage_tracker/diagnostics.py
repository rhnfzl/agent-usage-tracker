"""Bounded, content-free facts from the collector currently being observed."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
from typing import Iterator

_KNOWN_CLIENTS = frozenset({
    "cli", "sdk-py", "sdk-cli", "t3-code", "t3 code", "claude code", "codex",
    "codex_cli_rs", "codex_vscode", "codex_exec", "vscode", "exec", "mcp",
    "automation", "rex", "super.engineering", "zeron", "paseo", "diri", "emdash",
    "monocode", "synara", "bb", "orca", "traycer", "claude desktop", "capy",
    "cloudroom", "superset", "paradev", "conductor",
})
_MAX_CLIENTS = 32
_MAX_COUNT = 2**63 - 1


@dataclass
class Diagnostics:
    skipped: Counter[str] = field(default_factory=Counter)
    clients: set[str] = field(default_factory=set)
    client_overflow: bool = False

    def snapshot(self) -> dict[str, object]:
        return {"skipped": dict(self.skipped), "clients": sorted(self.clients),
                "client_overflow": self.client_overflow}


_current: ContextVar[Diagnostics | None] = ContextVar("aut_collector_diagnostics", default=None)


@contextmanager
def capture(observed: Diagnostics | None = None) -> Iterator[Diagnostics]:
    observed = observed if observed is not None else Diagnostics()
    token = _current.set(observed)
    try:
        yield observed
    finally:
        _current.reset(token)


def observe_client(value: object) -> None:
    observed = _current.get()
    if observed is None or not isinstance(value, str) or not value:
        return
    normalized = value.strip().casefold() if len(value) <= 1024 else ""
    label = normalized if normalized in _KNOWN_CLIENTS else (
        "unknown:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16])
    if label in observed.clients:
        return
    if len(observed.clients) >= _MAX_CLIENTS:
        observed.client_overflow = True
        return
    observed.clients.add(label)


def note_skips(skipped: Counter[str]) -> None:
    observed = _current.get()
    if observed is None:
        return
    for key, count in skipped.items():
        if type(count) is not int or count <= 0:
            continue
        category = ("malformed_records" if key == "malformed" else
                    "unreadable_files" if key == "unreadable" else
                    "missing_payloads" if key == "missing_payloads" else
                    "activity_errors" if key.startswith("activity:") else None)
        if category:
            observed.skipped[category] = min(_MAX_COUNT, observed.skipped[category] + count)
