"""Count advisory verdicts on the ledger day the work happened, without changing the store."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from ..config import DEFAULT_LEDGER_TZ, AutError
from ..event import parse_ts
from .groups import _journal
from .rubric import legacy_version
from .store import Store


def daily(store: Store, *, days: int = 90, today: date | None = None,
          zone: tzinfo | None = None) -> list[dict[str, Any]]:
    if type(days) is not int or not 1 <= days <= 366:
        raise AutError("publish-improve: days must be 1..366")
    zone = zone or ZoneInfo(DEFAULT_LEDGER_TZ)
    today = today or datetime.now(zone).date()
    start = today - timedelta(days=days - 1)
    jobs: dict[str, int] = {}
    judgments: dict[str, int] = {}
    for index, event in enumerate(_journal(store)):
        report = event if event.get("kind") == "diagnosis" else (
            event.get("diagnosis") or event.get("progress", {}).get("diagnosis", {})
        ) if event.get("kind") == "cycle_finished" else {}
        if report.get("job_id"):
            jobs[report["job_id"]] = index
        for key in report.get("judgment_ids", []):
            judgments[key] = index
    qualifications: dict[tuple[str, int], tuple[datetime, bool]] = {}
    for key in store.keys("qualifications"):
        row = store.get("qualifications", key)
        stamp = parse_ts(row["computed_at"])
        if stamp is None or stamp.tzinfo is None or type(row["qualified"]) is not bool:
            raise AutError("publish-improve: invalid qualification")
        qualification_key = (row["rubric_id"], row["rubric_version"])
        if qualification_key not in qualifications or stamp >= qualifications[qualification_key][0]:
            qualifications[qualification_key] = (stamp, row["qualified"])
    newest: dict[tuple[str, str, int, str], tuple[tuple[int, str], dict[str, Any]]] = {}
    for key in store.keys("judgments"):
        row = store.get("judgments", key)
        version = row.get("rubric_version")
        if version is None:
            version = legacy_version(store.get("diagnoses", row["job_id"]), row["rubric"])
        case = store.get("cases", row["case_id"])
        trigger = row.get("trigger", case["trigger"])
        judgment_key = (row["case_id"], row["rubric"], version, trigger)
        rank = (max(jobs.get(row["job_id"], -1), judgments.get(key, -1)), key)
        if judgment_key not in newest or rank > newest[judgment_key][0]:
            newest[judgment_key] = (rank, {**row, "rubric_version": version, "trigger": trigger})
    counts: dict[tuple[str, str, int, str, str], dict[str, Any]] = {}
    for _, row in newest.values():
        case = store.get("cases", row["case_id"])
        stamp = next((stamp for event in case["events"]
                      if (stamp := parse_ts(event.get("timestamp"))) is not None), None)
        if stamp is None:
            continue
        day = stamp.replace(tzinfo=timezone.utc).astimezone(zone).date() if stamp.tzinfo is None \
            else stamp.astimezone(zone).date()
        if not start <= day <= today:
            continue
        slot = (day.isoformat(), row["rubric"], row["rubric_version"], case["agent"], row["trigger"])
        result = counts.setdefault(slot, dict(zip(
            ("day", "rubric_id", "rubric_version", "harness", "trigger"), slot),
            passed=0, failed=0, unknown=0,
            qualified=qualifications.get((row["rubric"], row["rubric_version"]),
                                         (datetime.min, False))[1]))
        verdict = "unknown" if row.get("reason") == "incomplete_evidence" else row["verdict"]
        if verdict not in {"pass", "fail", "unknown"}:
            raise AutError("publish-improve: invalid verdict")
        result[{"pass": "passed", "fail": "failed", "unknown": "unknown"}[verdict]] += 1
    return [counts[key] for key in sorted(counts)]
