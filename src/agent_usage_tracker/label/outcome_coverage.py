"""Pure recent/mature planner used by opt-in local coverage; activation defaults off."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import asdict

from .core import Request
from .tasks import OUTCOME_DAYS


def coverage_key(request: Request, policy_id: str) -> tuple[str, bool]:
    """A delivered mature request stays done as idle days increase, until its evidence changes."""
    age = request.facts.get("days_idle")
    if request.task != "soft_outcome" or type(age) is not int or age < 0:
        raise ValueError("outcome coverage requires a nonnegative integer idle age")
    mature = age >= OUTCOME_DAYS
    value = asdict(request)
    if mature:
        del value["facts"]["days_idle"]
    encoded = json.dumps({"request": value, "policy": policy_id,
                          "phase": "mature" if mature else "recent"},
                         sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest(), mature


def pending_outcomes(requests: list[Request], acknowledged: set[str],
                     policy_id: str, *, start_mature: bool = False) -> list[tuple[str, Request]]:
    """Interleave both queues, starting with the caller's persisted next phase.

    Callers must freeze one source snapshot and record keys only after delivery succeeds.
    Persist the opposite of the last delivered phase for the next invocation. Restarting
    recent-first each night cannot guarantee fairness with a one-request budget.
    Unknown predictions are delivered attempts too; this planner never interprets a label.
    """
    recent: deque[tuple[str, Request]] = deque()
    mature: deque[tuple[str, Request]] = deque()
    seen: dict[tuple[str, str], str] = {}
    for request in sorted(requests, key=lambda r: (r.ts, r.unit_id)):
        key, is_mature = coverage_key(request, policy_id)
        identity = request.unit, request.unit_id
        if identity in seen:
            if seen[identity] != key:
                raise ValueError("outcome coverage requires one current snapshot per unit")
            continue
        seen[identity] = key
        if key not in acknowledged:
            (mature if is_mature else recent).append((key, request))
    out = []
    queues = (mature, recent) if start_mature else (recent, mature)
    while recent or mature:
        for queue in queues:
            if queue:
                out.append(queue.popleft())
    return out
