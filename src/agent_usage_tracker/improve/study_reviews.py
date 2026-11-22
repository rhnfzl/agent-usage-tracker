"""Record an operator's acceptance decision on a frozen ordinary-work case."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import AutError
from ..event import parse_ts
from .case_view import _attempt
from .store import Store

RESULTS = ("accepted", "rejected", "unknown")
RECORD_KIND = "study_case_reviews"


def record_review(store: Store, case_id: str, family: str, criterion: str, result: str,
                  citations: list[str]) -> dict[str, Any]:
    family, criterion = family.strip(), criterion.strip()
    if not 1 <= len(family) <= 200 or not 1 <= len(criterion) <= 800 or result not in RESULTS:
        raise AutError("improve: a review needs a task family, criterion, and result")
    if len(citations) > 20 or len(set(citations)) != len(citations) or (result != "unknown" and not citations):
        raise AutError("improve: cite up to 20 distinct events and at least one for a reviewed outcome")
    case = store.get("cases", case_id)
    session = store.get("sessions", case["session_snapshot"])
    events = _attempt(session, case)
    end = events[-1]
    reviewed = datetime.now(timezone.utc)
    finished = parse_ts(end.get("timestamp"))
    if (events[0]["kind"] != "prompt" or end["kind"] != "boundary"
            or end.get("text") != "turn_complete" or not finished or not finished.tzinfo
            or finished > reviewed):
        raise AutError("improve: review a completed and dated ordinary-work attempt")
    by_id = {event["id"]: event for event in events}
    if not set(citations) <= by_id.keys():
        raise AutError("improve: review citations must belong to the selected attempt")
    if result != "unknown" and not any(by_id[event]["kind"] in {"reply", "result"}
            and not by_id[event].get("truncated") and (str(by_id[event].get("text") or "").strip()
            or by_id[event].get("status") in {"success", "error"}) for event in citations):
        raise AutError("improve: cite at least one unclipped reply or result for a reviewed outcome")
    record = {"case_id": case_id, "session_snapshot": case["session_snapshot"], "family": family,
              "criterion": criterion, "result": result, "event_ids": citations,
              "reviewed_at": reviewed.isoformat(), "authority": "operator"}
    key = store.put(RECORD_KIND, record)
    return {"review_id": key, **record}


def read_review(store: Store, review_id: str, case_id: str, family: str) -> dict[str, Any]:
    if not isinstance(review_id, str):
        raise AutError("improve: supply a task review ID")
    row = store.get(RECORD_KIND, review_id)
    case = store.get("cases", case_id)
    stamp = parse_ts(row.get("reviewed_at"))
    if (row.get("case_id") != case_id or row.get("session_snapshot") != case["session_snapshot"]
            or row.get("family") != family or row.get("result") not in RESULTS
            or row.get("authority") != "operator" or not stamp or not stamp.tzinfo
            or not isinstance(row.get("criterion"), str) or not row["criterion"].strip()):
        raise AutError("improve: task review must refer to the selected case and task family")
    return row


def baseline_reviews(store: Store, policy: dict[str, Any], frozen: datetime) -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    criteria: dict[str, str] = {}
    for case, review_id in policy.get("baseline_reviews", {}).items():
        family = policy["baseline_cases"][case]
        row = read_review(store, review_id, case, family)
        reviewed = datetime.fromisoformat(row["reviewed_at"])
        if row["result"] != "accepted" or reviewed > frozen:
            raise AutError("improve: freeze only baseline cases already accepted by the operator")
        if criteria.setdefault(family, row["criterion"]) != row["criterion"]:
            raise AutError("improve: use one frozen acceptance criterion per task family")
        reviews[case] = row
    return reviews
