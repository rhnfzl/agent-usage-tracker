"""Show which local evidence is available and where work can remain unobserved."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .state import _locked, atomic_write_text, state_path

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_STATE_JSON_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 8
MAX_COLLECTORS = 128
FRESH_HOURS = 12
_CAPABILITIES = ("requests", "commands", "results", "usage", "outcomes")
_NATIVE = {"claude_code", "codex", "pi"}
_ACTIVITY = {"steer", "interrupt", "rejection", "commit", "pr", "turn", "compaction",
             "invoke", "link", "label", "eval", "steer_estimate"}
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_CLASSES = {"ValueError", "TypeError", "OSError", "PermissionError", "FileNotFoundError",
                  "IsADirectoryError", "NotADirectoryError", "TimeoutError", "ConnectionError",
                  "ConnectionRefusedError", "BrokenPipeError", "RuntimeError", "KeyError",
                  "AttributeError", "OverflowError", "RecursionError", "UnicodeDecodeError",
                  "JSONDecodeError", "CollectorUnavailable", "AutError", "DeliveryError",
                  "OperationalError", "DatabaseError", "IntegrityError", "HTTPError", "URLError",
                  "KeyboardInterrupt", "SystemExit", "CancelledError"}


def observations_path() -> Path:
    return state_path().parent / "scan-observations.json"


def report_path() -> Path:
    return state_path().parent / "coverage.json"


def _read_json(path: Path, limit: int = MAX_JSON_BYTES) -> tuple[str, dict[str, Any]]:
    try:
        # A configured pipe/device must not block a read-only diagnosis.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            meta = os.fstat(handle.fileno())
            if not stat.S_ISREG(meta.st_mode):
                return "invalid", {}
            if meta.st_size > limit:
                return "limited", {}
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            return "limited", {}
        value = json.loads(raw)
        return ("available", value) if isinstance(value, dict) else ("invalid", {})
    except FileNotFoundError:
        return "missing", {}
    except (OSError, ValueError, RecursionError):
        return "unreadable", {}


def _count(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else 0


def _stamp(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        return stamp if stamp and stamp.tzinfo is not None else None
    except ValueError:
        return None


def _clean_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = row.get("collector")
    if not isinstance(name, str) or not _ID.fullmatch(name):
        return None
    from .diagnostics import Diagnostics, capture, observe_client

    diagnostics = row.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    clients = diagnostics.get("clients")
    observed = Diagnostics()
    with capture(observed):
        for client in clients[:32] if isinstance(clients, list) else []:
            if isinstance(client, str) and re.fullmatch(r"unknown:[a-f0-9]{16}", client):
                observed.clients.add(client)
            else:
                observe_client(client)
    skipped = diagnostics.get("skipped")
    skipped = skipped if isinstance(skipped, dict) else {}
    activity = row.get("activity")
    activity = activity if isinstance(activity, dict) else {}
    valid = (all(type(row.get(k)) is int and 0 <= row[k] <= 2**63 - 1
                 for k in ("usage_events", "human_classifications"))
             and isinstance(row.get("activity"), dict)
             and all(type(v) is int and 0 <= v <= 2**63 - 1 for v in activity.values())
             and _stamp(row.get("started_at")) is not None
             and _stamp(row.get("finished_at")) is not None)
    return {"collector": name,
            "status": row.get("status") if valid and row.get("status") in {
                "ok", "degraded", "error", "unavailable"}
            else "unknown",
            "error": row.get("error") if isinstance(row.get("error"), str)
            and row["error"] in _ERROR_CLASSES else "collector_error" if row.get("error") else None,
            "started_at": (_stamp(row.get("started_at")) or datetime.min.replace(
                tzinfo=timezone.utc)).isoformat(),
            "finished_at": (_stamp(row.get("finished_at")) or datetime.min.replace(
                tzinfo=timezone.utc)).isoformat(),
            "usage_events": _count(row.get("usage_events")),
            "human_classifications": _count(row.get("human_classifications")),
            "activity": {kind: _count(activity.get(kind)) for kind in sorted(_ACTIVITY)
                         if _count(activity.get(kind))},
            "diagnostics": {"skipped": {key: _count(skipped.get(key)) for key in
                ("malformed_records", "activity_errors", "unreadable_files", "missing_payloads")
                if _count(skipped.get(key))}, "clients": sorted(observed.clients),
                "client_overflow": bool(diagnostics.get("client_overflow")) or
                    isinstance(clients, list) and len(clients) > 32}}


def save_scan_observations(rows: list[dict[str, Any]]) -> None:
    """Merge collector receipts without replacing another collector's most recent run."""
    path = observations_path()
    with _locked(path):
        _, data = _read_json(path)
        previous = data.get("collectors", {})
        merged = {}
        if isinstance(previous, dict):
            for value in list(previous.values())[:MAX_COLLECTORS]:
                clean = _clean_row(value) if isinstance(value, dict) else None
                if clean:
                    merged[clean["collector"]] = clean
        for row in rows[:MAX_COLLECTORS]:
            clean = _clean_row(row)
            if clean:
                previous_row = merged.get(clean["collector"], {})
                previous_at = _stamp(previous_row.get("finished_at"))
                incoming_at = _stamp(clean.get("finished_at"))
                if (previous_at and previous_at <= datetime.now(timezone.utc)
                        and (incoming_at is None or previous_at > incoming_at)):
                    continue
                merged[clean["collector"]] = clean
        if len(merged) > MAX_COLLECTORS:
            raise ValueError("scan receipt collector limit exceeded")
        atomic_write_text(path, json.dumps({"version": 1, "collectors": merged}, sort_keys=True))


def _issue(issues: list[dict[str, str]], code: str, source: str, severity: str = "warning") -> None:
    item = {"code": code, "source_id": source, "severity": severity}
    if item not in issues:
        issues.append(item)


def _receipt(raw: object, now: datetime) -> dict[str, Any]:
    row = _clean_row(raw) if isinstance(raw, dict) else None
    if row is None:
        return {"receipt_status": "missing", "activity_state": "unknown"}
    counts_valid = (isinstance(raw, dict) and all(
        type(raw.get(k)) is int and 0 <= raw[k] <= 2**63 - 1
        for k in ("usage_events", "human_classifications"))
        and isinstance(raw.get("activity"), dict)
        and all(type(v) is int and 0 <= v <= 2**63 - 1 for v in raw["activity"].values()))
    stamp = _stamp(row["finished_at"])
    start = _stamp(raw.get("started_at")) if isinstance(raw, dict) else None
    status = ("unknown" if not counts_valid or stamp is None or stamp > now
              or start is None or start > stamp or row["status"] == "unknown" else
              "stale" if now - stamp > timedelta(hours=FRESH_HOURS) else "fresh")
    active = row["usage_events"] + row["human_classifications"] + sum(row["activity"].values())
    lost = any(row["diagnostics"]["skipped"].values())
    return {**row, "receipt_status": status,
            "activity_state": ("observed" if active else "unknown" if lost else "idle")
            if status == "fresh" and row["status"] == "ok" else "unknown",
            "count_scope": "locally_normalized_emitted_not_acknowledged"}


def _roots(cfg: Config, engine: str) -> list[Path]:
    from .sources import engine_homes

    if engine in _NATIVE:
        suffix = "projects" if engine == "claude_code" else "sessions"
        return [home / suffix for home in engine_homes(cfg.home, engine)]
    if engine == "cursor":
        from .collectors.cursor import cursor_db
        return [cursor_db(cfg)]
    if engine == "cursor_cli":
        return [cfg.home / ".cursor/chats", cfg.home / ".cursor/projects"]
    if engine == "t3":
        from .collectors.t3 import database_path
        return [database_path(cfg.home)]
    # Globs, network services and credentials are never probed by doctor.
    return []


def _metadata(root: Path, files: dict[str, Any], budget: list[int], engine: str,
              limit: int | None = None) -> dict[str, Any]:
    stop_at = min(MAX_ENTRIES, budget[0] + limit) if limit is not None else MAX_ENTRIES
    info: dict[str, Any] = {"path": str(root), "status": "available", "files": 0,
                            "bytes": 0, "unread_bytes": 0, "limited": False}

    def visit(path: Path, depth: int) -> None:
        if budget[0] >= stop_at:
            info["limited"] = True
            if path == root:
                info["status"] = "unknown"
            return
        budget[0] += 1
        try:
            meta = path.lstat()
            if stat.S_ISLNK(meta.st_mode):
                # The root can be an explicitly registered alias; never follow child links.
                if path != root:
                    info["limited"] = True
                    return
                meta = path.stat()
            if path == root and (
                    engine in _NATIVE and not stat.S_ISDIR(meta.st_mode)
                    or engine in {"cursor", "t3"} and not stat.S_ISREG(meta.st_mode)):
                info["status"] = "unreadable"
                return
            if stat.S_ISREG(meta.st_mode):
                recognized = (path == root or
                    engine == "claude_code" and path.suffix == ".jsonl" or
                    engine == "codex" and path.name.startswith("rollout-") and path.suffix == ".jsonl" or
                    engine == "pi" and path.suffix == ".jsonl" or
                    engine == "cursor_cli" and path.name in {"store.db", "index.db"})
                if not recognized:
                    return
                info["files"] += 1
                info["bytes"] += meta.st_size
                previous = files.get(str(path))
                off = _count(previous.get("off")) if isinstance(previous, dict) else 0
                same = isinstance(previous, dict) and previous.get("ino") == meta.st_ino
                # SQLite watermarks are logical IDs, not byte offsets.
                if path.suffix == ".jsonl":
                    info["unread_bytes"] += (meta.st_size - off
                                             if same and off <= meta.st_size else meta.st_size)
            elif stat.S_ISDIR(meta.st_mode):
                if depth >= MAX_DEPTH:
                    info["limited"] = True
                    return
                with os.scandir(path) as entries:
                    for entry in entries:
                        if budget[0] >= stop_at:
                            info["limited"] = True
                            break
                        visit(Path(entry.path), depth + 1)
        except FileNotFoundError:
            info["status"] = "missing" if path == root else "changed_during_read"
        except (OSError, ValueError):
            info["status"] = "unreadable"

    visit(root, 0)
    return info


def _capabilities(engine: str, observation: dict[str, Any], available: bool) -> dict[str, Any]:
    transcript = _NATIVE | {"t3", "cursor_cli"}
    support = {"requests": transcript | {"cursor"}, "commands": transcript,
               "results": transcript, "outcomes": transcript,
               "usage": _NATIVE | {"t3", "cursor", "cursor_sdk", "cursor_api", "generic_sdk"}}
    return {cap: {
        "support": ("commit_or_pr_links_only" if cap == "outcomes" else
                    "native_parser" if engine in transcript else "normalized_parser")
        if engine in support[cap] else "unsupported",
        "observed": (None if not available or observation.get("receipt_status") != "fresh"
                     or observation.get("status") != "ok"
                     or observation.get("activity_state") == "unknown"
                     or engine not in support[cap] else
                     bool(observation.get("usage_events")) if cap == "usage" else
                     bool(observation.get("human_classifications")) if cap == "requests" else
                     None),
        "evidence_scope": ("collector_classifications_not_human_origin_proof" if cap == "requests"
                           else "collector_commit_or_pr_links_not_verified_outcomes" if cap == "outcomes"
                           else "collector_usage_rows" if cap == "usage"
                           else "no_per_capability_evidence_in_scan_receipt"),
        "completeness": "unknown"} for cap in _CAPABILITIES}


def _delivery() -> dict[str, Any]:
    from .push import outbox_path

    path = outbox_path()
    try:
        if not path.exists():
            return {"status": "available", "pending": {}}
        before = path.stat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {"status": "unknown", "pending": {}}
            header = handle.read(100)
        sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
        if (len(header) != 100
                or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01"
                or any(item.exists() for item in sidecars)):
            # shortcut: live or persisted WAL stays unknown until a safe snapshot route exists.
            return {"status": "unknown", "pending": {}}
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1",
                                     uri=True, timeout=0.1)) as con:
            con.execute("PRAGMA query_only=ON")
            remaining = [1000]

            def bounded() -> int:
                remaining[0] -= 1
                return int(remaining[0] <= 0)

            con.set_progress_handler(bounded, 1000)
            rows = con.execute("SELECT kind, COUNT(*) FROM pending GROUP BY kind LIMIT 16").fetchall()
        after = path.stat()
        identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(before, key) != getattr(after, key) for key in identity)
                or any(item.exists() for item in sidecars)):
            return {"status": "unknown", "pending": {}}
        allowed = {"usage", "session", "activity", "time"}
        if any(kind not in allowed or type(count) is not int or count < 0 for kind, count in rows):
            return {"status": "unknown", "pending": {}}
        return {"status": "available", "pending": dict(rows)}
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "unknown", "pending": {}}


def _publication(declaration: dict[str, str], now: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {"id": declaration["id"], "timezone": declaration["timezone"],
                             "status": "unknown", "reason": "receipt_unavailable"}
    try:
        zone = ZoneInfo(declaration["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        result["reason"] = "invalid_timezone"
        return result
    required = now.astimezone(zone).date() - timedelta(days=1)
    result["required_completed_through"] = required.isoformat()
    status, raw = _read_json(Path(declaration["path"]), 64 * 1024)
    if status != "available":
        result["reason"] = "receipt_" + status
        return result
    verified, published = _stamp(raw.get("verified_at")), _stamp(raw.get("published_at"))
    try:
        completed = date.fromisoformat(raw["completed_through"])
    except (KeyError, ValueError, TypeError):
        result["reason"] = "completed_day_invalid"
        return result
    revision, live = raw.get("published_revision"), raw.get("live_revision")
    if (verified is None or published is None or verified > now or published > now
            or published > verified or completed > required):
        result["reason"] = "receipt_time_invalid"
    elif (not isinstance(revision, str) or not revision or len(revision) > 256
          or not isinstance(live, str) or not live or len(live) > 256):
        result["reason"] = "revision_unverified"
    elif completed != required or now - verified > timedelta(hours=FRESH_HOURS) or revision != live:
        result.update(status="stale", reason="day_revision_or_verification_stale")
    else:
        result.update(status="fresh", reason="local_receipt_verified")
    result["verification_scope"] = "local_receipt_only_no_live_network_check"
    return result


def build_report(cfg: Config, *, now: datetime | None = None) -> dict[str, Any]:
    """Inspect bounded local metadata. Never run collectors, models, delivery or state recovery."""
    from .sources import Registry, SourceError, load_registry

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("coverage time requires a timezone")
    issues: list[dict[str, str]] = []
    try:
        registry = load_registry(cfg.home)
        registry_status = "available"
    except (SourceError, OSError, ValueError):
        registry = Registry((), (), ())
        registry_status = "unreadable"
        _issue(issues, "registry_unreadable", "registry")
    state_status, state = _read_json(state_path(), MAX_STATE_JSON_BYTES)
    watermark = state.get("collectors", {})
    if state_status == "limited":
        _issue(issues, "state_limited", "state")
        watermark = {}
    elif (state_status not in {"available", "missing"} or not isinstance(watermark, dict)
            or state_status == "available" and "collectors" not in state):
        _issue(issues, "state_unreadable", "state")
        watermark = {}
    receipt_status, receipt_data = _read_json(observations_path())
    receipts = receipt_data.get("collectors", {})
    if (receipt_status not in {"available", "missing"} or not isinstance(receipts, dict)
            or receipt_status == "available" and (type(receipt_data.get("version")) is not int
                                                  or receipt_data.get("version") != 1)):
        _issue(issues, "scan_receipts_unreadable", "scan")
        receipts = {}
    budget = [0]
    collectors: list[dict[str, Any]] = []
    all_engines = list(dict.fromkeys((*cfg.agents, "claude_code", "codex", "pi",
                                    *(s.engine for s in registry.sources))))[:MAX_COLLECTORS]
    planned_roots: dict[str, list[Path]] = {}
    for engine in all_engines:
        try:
            planned_roots[engine] = _roots(cfg, engine)
        except (OSError, ValueError, RuntimeError):
            planned_roots[engine] = []
            _issue(issues, "source_discovery_failed", "collector:" + engine)
    root_count = sum(len(roots) for roots in planned_roots.values())
    root_budget = max(1, MAX_ENTRIES // max(1, root_count))
    for engine in all_engines:
        observation = _receipt(receipts.get(engine), now)
        configured = engine in cfg.agents
        if configured and observation["receipt_status"] != "fresh":
            _issue(issues, "scan_receipt_" + observation["receipt_status"], "collector:" + engine)
        if configured and observation.get("status") not in {None, "ok"}:
            _issue(issues, "collector_failed", "collector:" + engine)
        skipped = observation.get("diagnostics", {}).get("skipped", {})
        if any(skipped.get(k) for k in ("malformed_records", "activity_errors")):
            _issue(issues, "parser_loss", "collector:" + engine)
        if skipped.get("missing_payloads"):
            _issue(issues, "source_payload_missing", "collector:" + engine)
        if skipped.get("unreadable_files"):
            _issue(issues, "source_unreadable", "collector:" + engine)
        clients = observation.get("diagnostics", {}).get("clients", [])
        if any(c.startswith("unknown:") for c in clients):
            _issue(issues, "unattributed_client", "collector:" + engine, "info")
        wm = watermark.get(engine, {})
        files = wm.get("files", {}) if isinstance(wm, dict) else {}
        if not isinstance(wm, dict) or not isinstance(files, dict):
            _issue(issues, "state_unreadable", "collector:" + engine)
        files = files if isinstance(files, dict) else {}
        roots = [_metadata(root, files, budget, engine, root_budget)
                 for root in planned_roots[engine]]
        for root in roots:
            if root["limited"]:
                _issue(issues, "discovery_limited", "collector:" + engine)
            if root["status"] not in {"available", "missing", "unknown"}:
                _issue(issues, "source_unreadable", "collector:" + engine)
            if configured and root["unread_bytes"]:
                _issue(issues, "source_byte_backlog", "collector:" + engine)
        collectors.append({"collector": engine, "configured": configured, "roots": roots,
                           "observation": observation,
                           "capabilities": _capabilities(engine, observation, True)})
    by_engine = {c["collector"]: c for c in collectors}
    sources: list[dict[str, Any]] = []
    declarations: list[dict[str, Any]] = [
        {"id": "native:" + e, "harness": e, "engine": e, "location": "local",
         "home": None, "registered": False, "transport": "native_files"}
        for e in all_engines if e in cfg.agents or e in _NATIVE]
    declarations.extend({"id": s.id, "harness": s.harness, "engine": s.engine,
                         "location": s.location, "home": s.home, "registered": True,
                         "transport": s.transport}
                        for s in registry.sources)
    for declaration in declarations:
        engine = declaration["engine"]
        collector = by_engine.get(engine, {})
        roots = collector.get("roots", [])
        if declaration["home"] is not None:
            home = declaration["home"].resolve()
            roots = [r for r in roots if home == Path(r["path"]).resolve().parent]
        supported = engine in _NATIVE | {"cursor", "cursor_cli", "cursor_sdk", "cursor_api",
                                        "generic_sdk", "activitywatch", "t3"}
        route_supported = (not declaration["registered"] or engine in _NATIVE
                           and declaration["home"] is not None
                           and declaration["transport"] in {"native_files", "structured_cli"})
        available = ("unknown" if declaration["location"] == "remote" or not supported
                     or not route_supported or not roots
                     or any(r["status"] == "unknown" for r in roots) else
                     "unreadable" if any(r["status"] not in {"available", "missing"} for r in roots)
                     else "available" if any(r["status"] == "available" for r in roots) else "missing")
        if declaration["registered"] and (not supported or not route_supported
                                          or declaration["location"] == "remote"):
            _issue(issues, "registered_source_unsupported", declaration["id"])
        if declaration["registered"] and available == "unreadable":
            _issue(issues, "source_unreadable", declaration["id"])
        sources.append({k: v for k, v in declaration.items() if k != "home"} | {
            "availability": available, "roots": [r["path"] for r in roots],
            "reason": "host_local_evidence_required" if declaration["location"] == "remote" else
                      "no_collector_for_engine" if not supported else
                      "declared_transport_not_collected" if not route_supported else
                      "metadata_only_no_transcript_read",
            "session_associations": sum(link["source_id"] == declaration["id"] for link in registry.links),
            "capabilities": _capabilities(engine if route_supported else "", {}, False)})
    declared_sources = {source.id: source for source in registry.sources}
    session_links = [{**link, "engine": declared_sources[link["source_id"]].engine,
                      "harness": declared_sources[link["source_id"]].harness,
                      "association_scope": "declared_not_duplicate_or_human_proof"}
                     for link in registry.links[:512]]
    if len(registry.links) > len(session_links):
        _issue(issues, "session_links_limited", "registry", "info")
    publications = [_publication(p, now) for p in registry.publications]
    for publication in publications:
        if publication["status"] != "fresh":
            _issue(issues, "publication_" + publication["status"], publication["id"])
    delivery = _delivery()
    if delivery["status"] == "unknown":
        _issue(issues, "delivery_unknown", "delivery")
    elif sum(delivery["pending"].values()):
        _issue(issues, "delivery_backlog", "delivery")
    catalog_status, catalog = _read_json(
        Path(__file__).with_name("coverage_catalog.json"), 1024 * 1024)
    harnesses = catalog.get("harnesses", [])
    if (catalog_status != "available" or catalog.get("version") != 1
            or not isinstance(harnesses, list) or len(harnesses) > 128
            or any(not isinstance(entry, dict) for entry in harnesses)):
        harnesses = []
        _issue(issues, "catalog_unavailable", "catalog", "info")
    from .harness_inventory import discover
    agents = frozenset(cfg.agents)
    inventory = discover(cfg.home, harnesses, readers=agents)
    if inventory["status"] == "limited":
        _issue(issues, "harness_inventory_limited", "inventory", "info")
    wraps = {entry.get("id"): entry.get("wraps") for entry in harnesses}
    for candidate in inventory["observations"]:
        harness = candidate["harness_id"]
        if candidate["reader_qualified"]:
            continue
        if candidate["activity"] != "recent":
            _issue(issues, "harness_route_unverified", harness, "info")
        elif isinstance(wraps.get(harness), list) and agents & set(wraps[harness]):
            # It drives native CLIs whose logs AUT reads: the work is counted, only not attributed.
            _issue(issues, "harness_unattributed", harness, "info")
        else:
            _issue(issues, "harness_active_unread", harness)
    return {"version": 1, "generated_at": now.isoformat(), "completeness": "unknown",
            "thresholds": {"collector_evidence_hours": FRESH_HOURS,
                           "publication_verification_hours": FRESH_HOURS},
            "state_status": state_status,
            "read_limits": {"json_bytes": MAX_JSON_BYTES, "state_json_bytes": MAX_STATE_JSON_BYTES,
                            "metadata_entries": MAX_ENTRIES,
                            "metadata_entries_per_root": root_budget,
                            "metadata_depth": MAX_DEPTH, "metadata_entries_visited": budget[0]},
            "registry_status": registry_status, "sources": sources, "collectors": collectors,
            "session_links": session_links, "session_links_omitted": len(registry.links) - len(session_links),
            "publications": publications, "delivery": delivery, "issues": issues,
            "catalog": harnesses, "harness_inventory": inventory}


ALERT_HOURS = 24
ALERT_FORGET_DAYS = 7
# degraded pages only with the homelab `agent coverage` rule; "error" pages through the
# existing rule after 3 scans.
ALERT_STATUS: Final = "degraded"
_ALERT_PHRASES = {
    "harness_active_unread": "%s harness active, no AUT reader",
    "discovery_limited": "%s discovery limited",
    "source_payload_missing": "%s payloads missing",
    "parser_loss": "%s parser loss",
    "source_unreadable": "%s source unreadable",
}


def alert_state(report: dict[str, Any], previous: dict[str, Any],
                now: datetime) -> tuple[dict[str, dict[str, str]], str | None]:
    """Carry per-issue alert state and return it with the text of the issues that are new.
    An actionable warning alerts for ALERT_HOURS after it first appears, then it is known.
    A key absent for ALERT_FORGET_DAYS is forgotten, so flapping issues do not page again.
    The text holds fixed phrases and source ids only, never paths."""
    prior = previous.get("alerts")
    baseline = not isinstance(prior, dict)      # first report after upgrade: today's issues are known
    prior = prior if isinstance(prior, dict) else {}
    forget = timedelta(days=ALERT_FORGET_DAYS)

    def live(entry: object) -> dict[str, Any] | None:
        last = _stamp(entry.get("last_seen")) if isinstance(entry, dict) else None
        return entry if isinstance(entry, dict) and last and now - last < forget else None

    current = sorted({f"{i['code']}:{i['source_id']}" for i in report.get("issues", [])
                      if isinstance(i, dict) and i.get("code") in _ALERT_PHRASES
                      and i.get("severity") != "info" and isinstance(i.get("source_id"), str)})
    alerts = {key: entry for key, entry in prior.items() if key not in current and live(entry)}
    start = now - timedelta(hours=ALERT_HOURS) if baseline else now
    for key in current:
        # A present key keeps its first sighting even after a gap in scans; only absence forgets it.
        entry = prior.get(key)
        first = _stamp(entry.get("first_seen")) if isinstance(entry, dict) else None
        alerts[key] = {"first_seen": (first or start).isoformat(), "last_seen": now.isoformat()}
    new = [key for key in current
           if now - (_stamp(alerts[key]["first_seen"]) or now) < timedelta(hours=ALERT_HOURS)]
    if not new:
        return alerts, None
    phrases = []
    for key in new:
        code, _, source = key.partition(":")
        phrases.append(_ALERT_PHRASES[code] % source.removeprefix("collector:"))
    return alerts, ("new: " + "; ".join(phrases))[:300]


def save_report(report: dict[str, Any]) -> bool:
    """Keep the latest private assessment and identify changes to the current issue set."""
    signature = hashlib.sha256(json.dumps(sorted(
        (str(i.get("code", "")), str(i.get("source_id", "")), str(i.get("severity", "")))
        for i in report.get("issues", []) if isinstance(i, dict))).encode()).hexdigest()
    path = report_path()
    with _locked(path):
        _, previous = _read_json(path)
        previous_at = _stamp(previous.get("generated_at"))
        incoming_at = _stamp(report.get("generated_at"))
        if (previous_at and previous_at <= datetime.now(timezone.utc)
                and (incoming_at is None or previous_at > incoming_at)):
            return False
        atomic_write_text(path, json.dumps({**report, "issue_signature": signature}, sort_keys=True))
    return previous.get("issue_signature") != signature
