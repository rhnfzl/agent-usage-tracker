"""Replay labeled retrieval questions against an authorized, frozen evidence scope."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from ..config import AutError
from .access import Evidence
from .ranked_search import SearchMode

FILTERS = {"agent", "kind", "status", "since", "until", "tool", "workspace", "session_id", "argument_path"}


def evaluate(evidence: Evidence, path: Path, modes: Sequence[SearchMode], k: int) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            raw = source.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("evaluation set exceeds 1 MiB")
            data = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise AutError("evidence: evaluation set must be readable JSON") from exc
    if not (isinstance(data, dict) and data.get("version") == 1
            and data.get("audit_id") == evidence.audit_id and data.get("content") == evidence.content
            and isinstance(data.get("cases"), list) and 1 <= len(data["cases"]) <= 200
            and 1 <= k <= 100):
        raise AutError("evidence: evaluation requires version 1, matching audit and content, "
                       "1..200 cases and a top-k of 1..100")

    cases = []
    seen: set[str] = set()
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise AutError("evidence: invalid evaluation case")
        name, category, query = (case.get(key) for key in ("id", "category", "query"))
        filters, references = case.get("filters", {}), case.get("relevant")
        if not (isinstance(name, str) and name.strip() and isinstance(category, str)
                and category.strip() and isinstance(query, str) and query.strip() and name not in seen and isinstance(filters, dict) and filters.keys() <= FILTERS
                and all(isinstance(v, str) for v in filters.values())
                and isinstance(references, list) and len(references) <= 100):
            raise AutError("evidence: invalid evaluation case fields")
        seen.add(name)
        labels = set()
        cached_sessions: dict[str, set[str]] = {}
        for ref in references:
            if (not isinstance(ref, list) or len(ref) != 2
                    or not all(isinstance(value, str) for value in ref)):
                raise AutError("evidence: labels must be [session_id, event_id] pairs")
            session, event = ref
            if session not in cached_sessions:
                cached_sessions[session] = {item["id"] for item in
                                            evidence._read(session, include_text=False)["events"]}
            if event not in cached_sessions[session] or (session, event) in labels:
                raise AutError("evidence: evaluation has missing or duplicate event labels")
            labels.add((session, event))
        cases.append((name, category, query, filters, labels))

    reports = {}
    for mode in modes:
        totals: Counter[str] = Counter()
        rows = []
        reciprocal_rank = 0.0
        event_recall = session_recall = 0.0
        for name, category, query, filters, labels in cases:
            start = perf_counter()
            result = evidence.search(query, mode=mode, limit=k, **filters)
            hits = list(result["items"])
            response_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            realized_mode = result.get("search_mode", mode)
            cursor = result["next_cursor"]
            while cursor and len(hits) < k:
                result = evidence.search(query, mode=mode, limit=k - len(hits), cursor=cursor, **filters)
                response_bytes += len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                hits.extend(result["items"])
                cursor = result["next_cursor"]
            elapsed = (perf_counter() - start) * 1000
            ids = [(item["session_id"], item["event_id"]) for item in hits]
            sessions = {session for session, _ in labels}
            event_rank = next((rank for rank, ref in enumerate(ids, 1) if ref in labels), None)
            session_rank = next((rank for rank, ref in enumerate(ids, 1) if ref[0] in sessions), None)
            matched = len(set(ids) & labels)
            matched_sessions = len({session for session, _ in ids} & sessions)
            if labels:
                totals["answerable"] += 1
                totals["event_hits"] += event_rank is not None
                totals["session_hits"] += session_rank is not None
                reciprocal_rank += 1 / event_rank if event_rank else 0.0
                event_recall += matched / len(labels)
                session_recall += matched_sessions / len(sessions)
            else:
                totals["unanswerable"] += 1
                totals["empty_unanswerable"] += not ids
            rows.append({"id": name, "category": category, "returned": len(ids),
                         "search_mode": realized_mode,
                         "event_rank": event_rank, "session_rank": session_rank,
                         "relevant_returned": matched, "relevant_total": len(labels),
                         "relevant_sessions_returned": matched_sessions,
                         "relevant_sessions_total": len(sessions),
                         "response_bytes": response_bytes, "elapsed_ms": round(elapsed, 2)})
        answerable = totals["answerable"]
        reports[mode] = {"counts": dict(totals), "cases": rows,
                         "mean_reciprocal_rank": reciprocal_rank / answerable if answerable else None,
                         "mean_event_recall": event_recall / answerable if answerable else None,
                         "mean_session_recall": session_recall / answerable if answerable else None}
    return {"audit_id": evidence.audit_id, "content": evidence.content, "k": k, "modes": reports}
