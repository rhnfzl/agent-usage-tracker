"""Publish only advisory counts and operator-written adoption metadata to the private sink."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from pathlib import Path

from .config import AutError, Config, ledger_zone
from .improve import scores
from .improve.store import Store, digest
from .label.core import data_dir
from .push import job_heartbeat
from .sink import check_ledger_zone, write_many
from .state import _locked, atomic_write_text, state_path

SCORE_COLUMNS = ("day", "rubric_id", "rubric_version", "harness", "trigger",
                 "passed", "failed", "unknown", "qualified")
SCORE_SQL = ("INSERT INTO agents.improve_score_daily (day, rubric_id, rubric_version, harness, "
             "trigger, passed, failed, unknown, qualified, pushed_at) "
             "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING")
ADOPT_SQL = ("INSERT INTO agents.improve_adoption "
             "(adoption_id, adopted_at, title, kind, rubric_ids, pr) "
             "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (adoption_id) DO NOTHING")


def _text(value: Any, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 120 \
            or any(ord(c) < 32 or c in "/\\" for c in value) \
            or re.search(r"[a-fA-F0-9]{32,}", value) \
            or (identifier and not re.fullmatch(r"[a-z_]{1,40}", value)):
        raise AutError("publish-improve: unsafe public text field; edit the private metadata")
    return value


def _score(row: dict[str, Any]) -> None:
    if set(row) != set(SCORE_COLUMNS):
        raise AutError("publish-improve: unexpected score fields")
    _text(row["rubric_id"], identifier=True)
    day = date.fromisoformat(row["day"])
    if day.isoformat() != row["day"] or row["harness"] not in {"codex", "claude_code", "t3"} \
            or row["trigger"] not in {"control", "error_result", "merge"} \
            or any(type(row[k]) is not int or not 0 <= row[k] <= 2147483647
                   for k in ("rubric_version", "passed", "failed", "unknown")) \
            or type(row["qualified"]) is not bool:
        raise AutError("publish-improve: invalid aggregate fields")


def _key(row: dict[str, Any]) -> str:
    return json.dumps([row[k] for k in SCORE_COLUMNS[:5]])


def run_publish(cfg: Config, *, days: int = 90, dry_run: bool = False) -> dict[str, int]:
    started = datetime.now(timezone.utc)
    with job_heartbeat(cfg, "improve_publish", started, dry_run=dry_run) as beat:
        root = Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
        store = Store(root, read_only=True)
        zone = ledger_zone(cfg)
        path = state_path().parent / "improve_publish_cache.json"
        # Serialize reads, writes and cache commit so an older run cannot replace newer counts.
        with _locked(path):
            now = datetime.now(timezone.utc)
            today = now.astimezone(zone).date()
            rows = scores.daily(store, days=days, today=today, zone=zone)
            target = digest([cfg.pg_dsn, str(store.root), cfg.ledger_timezone])
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                cache = {"target": target, "rows": []}
            if not isinstance(cache, dict) or not isinstance(cache.get("rows"), list):
                raise AutError("publish-improve: invalid cache; preserve it before removing it")
            for row in [*rows, *cache["rows"]]:
                _score(row)
            previous = {_key(r): r for r in cache["rows"]} if cache.get("target") == target else {}
            current = {_key(r): r for r in rows}
            start = (today - timedelta(days=days - 1)).isoformat()
            # Falling outside the lookback window is not a deletion of historical work.
            for key, row in previous.items():
                if key not in current and start <= row["day"] <= today.isoformat():
                    current[key] = {**row, "passed": 0, "failed": 0, "unknown": 0}
            changed = [r for k, r in sorted(current.items()) if previous.get(k) != r]
            adoptions = []
            for key in store.keys("adoptions"):
                row = store.get("adoptions", key)
                title = _text(row["title"])
                kind = row["kind"]
                rubrics, pr = row["rubric_ids"], row["pr"]
                if kind not in {"instruction", "skill", "config", "code"} \
                        or not isinstance(rubrics, list) \
                        or (pr is not None and (type(pr) is not int or not 1 <= pr <= 2147483647)) \
                        or row.get("benefit_claim") is not False:
                    raise AutError("publish-improve: invalid adoption metadata")
                for rubric in rubrics:
                    _text(rubric, identifier=True)
                adopted = datetime.combine(date.fromisoformat(row["adopted_at"]), time(12), zone)
                adoptions.append((key[:16], adopted, title, kind, rubrics, pr))
            if not dry_run:
                dsn = cfg.require_pg("publish-improve")
                check_ledger_zone(dsn, cfg.ledger_timezone)
                write_many(dsn, SCORE_SQL,
                           [tuple(r[k] for k in SCORE_COLUMNS) + (now,) for r in changed])
                write_many(dsn, ADOPT_SQL, adoptions)
                atomic_write_text(path, json.dumps({"target": target,
                    "rows": list((previous | current).values())}, sort_keys=True) + "\n")
            beat.events = len(changed) + len(adoptions)
            return {"scores": len(rows), "changed": len(changed), "adoptions": len(adoptions)}
