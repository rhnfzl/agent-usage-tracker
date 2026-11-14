"""Find known app-name candidates without reading app contents or claiming activity."""

from __future__ import annotations

from itertools import islice
import os
from pathlib import Path
import re
import sys
from typing import Any

MAX_PER_ROOT = 512
MAX_TOTAL = 1024


def application_dirs(home: Path) -> tuple[Path, Path]:
    return Path("/Applications"), home / "Applications"


def _name(value: str) -> str:
    value = value.casefold()
    if value.endswith(".app"):
        value = value[:-4]
    value = re.sub(r"[^a-z0-9]", "", value)
    return value[:-4] if value.endswith("beta") else value


def _known_names(catalog: list[dict[str, Any]]) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {}
    for entry in catalog[:128]:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", identifier):
            continue
        aliases = entry.get("aliases", [])
        aliases = aliases[:32] if isinstance(aliases, list) else []
        for value in [identifier, entry.get("name"), *aliases]:
            if isinstance(value, str) and len(value) <= 256 and (key := _name(value)):
                known.setdefault(key, set()).add(identifier)
    return known


def discover(home: Path, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Read only directory names and metadata in the two standard macOS app folders."""
    report: dict[str, Any] = {
        "status": "unknown", "observations": [], "completeness": "unknown", "roots": [],
        "entries_inspected": 0,
        "limits": {"entries_per_root": MAX_PER_ROOT, "entries_total": MAX_TOTAL},
        "claim_scope": "bundle_name_candidates_do_not_prove_installation_or_use",
    }
    if sys.platform != "darwin":
        report["reason"] = "platform_inventory_unavailable"
        return report
    known = _known_names(catalog)
    found: set[str] = set()
    limited = False
    for index, root in enumerate(application_dirs(home)[:2]):
        probe: dict[str, Any] = {"scope": "system" if index == 0 else "user",
                                 "status": "available", "entries_inspected": 0}
        remaining = min(MAX_PER_ROOT, MAX_TOTAL - report["entries_inspected"])
        try:
            if root.is_symlink():
                probe["status"] = "symlink_omitted"
                limited = True
            elif remaining <= 0:
                probe["status"] = "limited"
                limited = True
            else:
                with os.scandir(root) as entries:
                    for entry in islice(entries, remaining):
                        probe["entries_inspected"] += 1
                        report["entries_inspected"] += 1
                        if not entry.name.casefold().endswith(".app"):
                            continue
                        if entry.is_symlink():
                            probe["status"] = "symlinks_omitted"
                            limited = True
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        matches = known.get(_name(entry.name), set())
                        if len(matches) == 1:
                            found.update(matches)
                if probe["entries_inspected"] == remaining:
                    # Reaching the bound leaves completeness unknown without reading another name.
                    probe["status"] = "limited"
                    limited = True
        except FileNotFoundError:
            probe["status"] = "missing"
        except (OSError, ValueError):
            probe["status"] = "unreadable"
            limited = True
        report["roots"].append(probe)
    report["status"] = "limited" if limited else "available"
    report["observations"] = [
        {"harness_id": identifier, "evidence": "bundle_directory_name", "installation": "candidate",
         "activity": "unknown", "reader_qualified": False} for identifier in sorted(found)]
    return report
