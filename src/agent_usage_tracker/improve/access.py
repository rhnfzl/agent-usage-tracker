"""Scoped, read-only queries over frozen session evidence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal

from ..config import AutError
from ..event import parse_ts
from ..redact import redact
from .evidence import event_links
from .ranked_search import RankedSearch
from .setup_git import COMMIT_ID
from .store import Store, canonical, digest

MODES = ("metadata", "redacted", "full")
BUNDLE_LIMIT = 64 * 1024 * 1024
PAGE_SIZE = 100
SEARCH_BUDGET = 10_000
IDENTITY = re.compile(r"[a-f0-9]{64}")
EVENT_METADATA = ("id", "kind", "status", "timestamp", "line", "byte_offset", "truncated")
EVENT_KINDS = {"prompt", "call", "result", "reply", "boundary", "compaction", "tool_gap"}
EVENT_STATUSES = {"error", "success", "unknown", "completed"}
EVENT_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
EVENT_LABELS = ("tool", "call_id", "provider", "run_id", "table", "record_id")
SESSION_LABELS = ("session_id", "model", "cwd", "origin")


def _coverage(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items()
            if re.fullmatch(r"[a-z_]{1,60}", key) and type(value) in (int, bool)}


def _page(start: int, limit: int, maximum: int = PAGE_SIZE) -> None:
    if type(start) is not int or type(limit) is not int or start < 0 or not 1 <= limit <= maximum:
        raise AutError(f"evidence: use a nonnegative offset and a limit of 1..{maximum}")


class Evidence:
    def __init__(self, content: str, store: Store | None = None, audit_id: str | None = None,
                 bundle: Path | None = None) -> None:
        if content not in MODES or bool(audit_id) == bool(bundle):
            raise AutError("evidence: select exactly one audit or bundle and a valid content mode")
        self.content = content
        self.store = store
        self.bundled: dict[str, Any] = {}
        self.supplemental: set[str] = set()
        self._ranked = RankedSearch()
        if bundle is not None:
            self.bundled = _read_bundle(bundle)
            self.keys = list(self.bundled["sessions"])
            self.audit_id = self.bundled["audit_id"]
            self.coverage = self.bundled["coverage"]
            self.content = min((content, self.bundled["content"]), key=MODES.index)
            return
        assert store is not None and audit_id is not None
        audit = store.get("audits", audit_id)
        self.keys = audit["session_ids"]
        self.audit_id = audit_id
        self.coverage = audit["coverage"]
        self.ambiguous = set(audit.get("ambiguous_session_ids", []))
        self.supplemental = set(audit.get("supplemental_session_ids", []))

    def _read(self, key: str, *, include_text: bool = True,
              observe_file: Callable[[Path], None] | None = None) -> dict[str, Any]:
        if key not in self.keys and key not in self.supplemental:
            raise AutError("evidence: session is outside this audit or bundle")
        if self.bundled:
            return self.bundled["sessions"][key]
        assert self.store is not None
        if observe_file is not None:
            observe_file(self.store.path("sessions", key))
        row = self.store.get("sessions", key)
        coverage = _coverage(row["coverage"])
        if key in self.ambiguous:
            coverage.update(complete=False, divergent_copy=True)
        output = {"id": key, "agent": row["agent"], "coverage": coverage,
                  "events": row["events"],
                  "related_sessions": [sid for sid in row.get("supplemental_sources", [])
                                       if sid in self.supplemental]}
        if self.content != "metadata":
            output.update({field: row[field] for field in SESSION_LABELS if field in row})
        ref = row.get("full_content_id")
        if include_text and ref and self.content != "metadata":
            path = self.store.path("session_content", ref)
            if observe_file is not None:
                observe_file(path)
            if path.is_file():
                original = self.store.get("session_content", ref)
                output["events"] = [
                    {**event, **original[event["id"]],
                     "truncated": original[event["id"]]["attachment_omitted"],
                     "text_source": "captured_full"}
                    if event["id"] in original else event for event in row["events"]]
        return output

    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        result = {key: row[key] for key in ("id", "agent", "related_sessions")}
        result["coverage"] = _coverage(row["coverage"])
        if self.content != "metadata":
            result.update({field: self._label(row[field]) for field in SESSION_LABELS
                           if isinstance(row.get(field), str)})
        return result

    def _label(self, value: str) -> str:
        return redact(value) if self.content == "redacted" else value

    def _event(self, event: dict[str, Any], *, text: bool = True,
               selected: set[str] | None = None) -> dict[str, Any]:
        result = {field: event[field] for field in EVENT_METADATA if field in event}
        receipt = event.get("codex_model_call_receipt")
        if event["kind"] == "boundary" and isinstance(receipt, dict):
            count = receipt.get("count")
            started = parse_ts(receipt.get("started_at"))
            if type(count) is int and count > 0 and started and started.tzinfo:
                result["codex_model_call_receipt"] = {"count": count, "started_at": started.isoformat()}
        context = event.get("turn_context")
        if isinstance(context, dict) and context.get("status") in ("observed", "missing", "ambiguous"):
            visible = {"status": context["status"]}
            source = context.get("source_session")
            if source is not None and (not isinstance(source, str)
                    or source not in self.keys and source not in self.supplemental
                    or selected is not None and source not in selected):
                visible["status"] = "missing"
            if visible["status"] == "observed":
                source_event, matching_items = context.get("source_event"), context.get("matching_items")
                if (isinstance(source, str) and isinstance(source_event, str)
                        and EVENT_ID.fullmatch(source_event) and type(matching_items) is int
                        and matching_items >= 2):
                    visible.update(source_session=source, source_event=source_event,
                                   matching_items=matching_items)
                record = context.get("record")
                if isinstance(record, dict):
                    visible["record"] = {name: record[name] for name in ("line", "byte_offset")
                                         if name in record and (record[name] is None
                                                               or type(record[name]) is int)}
                if self.content != "metadata":
                    visible.update({name: self._label(context[name]) for name in ("model", "cwd")
                                    if isinstance(context.get(name), str)})
                    revision = context.get("session_start_revision")
                    if isinstance(revision, str) and COMMIT_ID.fullmatch(revision):
                        visible["session_start_revision"] = revision
            result["turn_context"] = visible
        if self.content != "metadata":
            result.update({field: self._label(event[field]) for field in EVENT_LABELS
                           if isinstance(event.get(field), str)})
            if text:
                result["text"] = self._label(event.get("text", ""))
                origin = event.get("text_source", "snapshot_redacted")
                result["text_source"] = "redacted_capture" if (
                    self.content == "redacted" and origin == "captured_full") else origin
        return result

    def describe(self) -> dict[str, Any]:
        """Report the fixed evidence scope, content mode, and capture coverage."""
        return {"audit_id": self.audit_id, "content": self.content,
                "sessions": len(self.keys), "coverage": _coverage(self.coverage)}

    def search(self, query: str = "", agent: str = "", kind: str = "", status: str = "",
               since: str = "", until: str = "", cursor: str = "", limit: int = 20,
               tool: str = "", workspace: str = "",
               mode: Literal["exact", "ranked"] = "exact") -> dict[str, Any]:
        """Find exact phrases or rank word matches, returning event references and previews."""
        _page(0, limit)
        if len(query) > 500 or ((query or tool or workspace) and self.content == "metadata"):
            raise AutError("evidence: text, tool and workspace filters require content access; "
                           "queries can contain up to 500 characters")
        start, end = parse_ts(since), parse_ts(until)
        if (since and (start is None or start.tzinfo is None)) or (
                until and (end is None or end.tzinfo is None)) or (start and end and start >= end):
            raise AutError("evidence: use timezone-aware timestamps in increasing order")
        def matches(event: dict[str, Any]) -> bool:
            if (kind and event["kind"] != kind) or (status and event.get("status") != status):
                return False
            if tool and self._label(event.get("tool") or "") != tool:
                return False
            if start or end:
                stamp = parse_ts(event.get("timestamp"))
                return bool(stamp and stamp.tzinfo and (not start or stamp >= start)
                            and (not end or stamp < end))
            return True

        def sessions(first: int, *, with_text: bool, observe_file: Callable[[Path], None] | None = None
                     ) -> Iterator[tuple[int, str, dict[str, Any]]]:
            for index in range(first, len(self.keys)):
                key = self.keys[index]
                row = self._read(key, include_text=with_text, observe_file=observe_file)
                if ((agent and row["agent"] != agent)
                        or (workspace and self._label(row.get("cwd") or "") != workspace)):
                    continue
                yield index, key, row

        def hit(key: str, row: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
            return {"session_id": key, "event_id": event["id"], "agent": row["agent"],
                    **self._event(event, text=bool(query))}

        if mode == "ranked":
            if self.content == "metadata":
                raise AutError("evidence: ranked search requires content access")
            binding = digest([self.audit_id, self.keys, self.content, query,
                              agent, kind, status, since, until, tool, workspace])[:24]
            offset = 0
            if cursor:
                match = re.fullmatch(rf"ranked:([0-9]{{1,6}}):{binding}", cursor)
                if not match:
                    raise AutError("evidence: ranked cursor does not match this query and scope")
                offset = int(match[1])
            def entries(observe_file: Callable[[Path], None]) -> Iterator[dict[str, Any]]:
                for _, key, row in sessions(0, with_text=True, observe_file=observe_file):
                    for event in row["events"]:
                        if matches(event):
                            yield hit(key, row, event)

            scope = digest([self.keys, self.content, agent, kind, status, since, until, tool, workspace])
            ranked_hits, more, count = self._ranked.search(scope, query, entries, offset, limit)
            continuation = f"ranked:{offset + limit}:{binding}" if more else None
            return {"items": ranked_hits, "next_cursor": continuation, "indexed_events": count,
                    "search_mode": "ranked", **self.describe()}

        if cursor and not re.fullmatch(r"[0-9]{1,6}:[0-9]{1,8}", cursor):
            raise AutError("evidence: invalid search cursor")
        first, offset = map(int, (cursor or "0:0").split(":"))
        needle = query.casefold()
        hits: list[dict[str, Any]] = []
        scanned = 0
        for index, key, row in sessions(first, with_text=bool(query)):
            events = row["events"]
            for position in range(offset if index == first else 0, len(events)):
                if len(hits) >= limit or scanned >= SEARCH_BUDGET:
                    return {"items": hits, "next_cursor": f"{index}:{position}", **self.describe()}
                scanned += 1
                event = events[position]
                if not matches(event):
                    continue
                result = hit(key, row, event)
                text = result.pop("text", "")
                if needle and needle not in text.casefold():
                    continue
                if needle:
                    found = text.casefold().find(needle)
                    left = max(0, found - 80)
                    result["preview"] = text[left:left + 240]
                hits.append(result)
        return {"items": hits, "next_cursor": None, **self.describe()}

    def session(self, session_id: str, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        """Read a page of event references and related snapshots in one session."""
        _page(offset, limit)
        row = self._read(session_id, include_text=False)
        events = row["events"]
        items = [self._event(event, text=False) for event in events[offset:offset + limit]]
        return {"session": self._summary(row), "items": items,
                "next_offset": offset + limit if offset + limit < len(events) else None,
                **self.describe()}

    def event(self, session_id: str, event_id: str, offset: int = 0,
              limit: int = 8_000) -> dict[str, Any]:
        """Read a captured event and page through the available text."""
        _page(offset, limit, 32_000)
        for event in self._read(session_id)["events"]:
            if event["id"] != event_id:
                continue
            result = self._event(event)
            text = result.get("text")
            if text is not None:
                result.update(text=text[offset:offset + limit], text_chars=len(text),
                              next_offset=offset + limit if offset + limit < len(text) else None)
            return {"session_id": session_id, **result, **self.describe()}
        raise AutError("evidence: event is missing from this session")

    def commits(self, commit: str, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        """Find captured Git commit results in the selected sessions."""
        from .outputs import commit_evidence

        _page(offset, limit)
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            raise AutError("evidence: use a hexadecimal commit prefix of 7..40 characters")
        commit = commit.lower()
        items: list[dict[str, str]] = []
        matched = 0
        for key in self.keys:
            row = self._read(key, include_text=self.content == "full")
            commits = row.get("commits")
            if commits is None:
                commits = commit_evidence(row["events"], row.get("links"))
            for link in commits:
                observed = link["observed_sha"]
                if not (commit.startswith(observed) or observed.startswith(commit)):
                    continue
                matched += 1
                if matched <= offset:
                    continue
                if len(items) == limit:
                    return {"items": items, "next_offset": offset + limit, **self.describe()}
                kind = ("equal_recorded_id" if observed == commit else
                        "observed_prefix" if commit.startswith(observed) else "query_prefix")
                items.append({"session_id": key, **link, "match": kind})
        return {"items": items, "next_offset": None, **self.describe()}

    def compare(self, baseline: str, candidate: str, cases: bool = False) -> dict[str, Any]:
        """Compare observed session or attempt counts and the first differing tool outcome."""
        def observed(key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            if cases:
                from .case_view import _attempt
                case = self._case(key)
                row = self._read(case["session_snapshot"], include_text=False)
                events = _attempt(row, case)
                summary = {**self._summary(row), "case_id": key,
                           "coverage": _coverage(case["coverage"])}
            else:
                row = self._read(key, include_text=False)
                events = row["events"]
                summary = self._summary(row)
            kinds = Counter(event["kind"] for event in events)
            errors = [event["id"] for event in events
                      if event["kind"] == "result" and event.get("status") == "error"]
            summary.update(error_event_ids=errors[:100], counts={
                "prompts": kinds["prompt"], "calls": kinds["call"],
                "errors": len(errors), "compactions": kinds["compaction"]})
            links = event_links(events) if cases or not self.bundled else row["links"]
            return summary, self._tool_outcomes(events, links)

        before, before_tools = observed(baseline)
        after, after_tools = observed(candidate)
        different = None
        for position, (left, right) in enumerate(zip_longest(before_tools, after_tools)):
            fields = ("tool", "status")
            if (left is None or right is None
                    or any(left.get(field) != right.get(field) for field in fields)):
                different = {"position": position, "baseline": left, "candidate": right}
                break
        return {"baseline": before, "candidate": after, "first_tool_difference": different,
                "delta": {key: after["counts"][key] - value for key, value in before["counts"].items()},
                **self.describe()}

    def _tool_outcomes(self, events: list[dict[str, Any]],
                       links: dict[str, str]) -> list[dict[str, Any]]:
        by_id = {event["id"]: event for event in events}
        rows = []
        for event in events:
            if event["kind"] != "call":
                continue
            result = by_id.get(links.get(event["id"], ""), {})
            row = {"call_event_id": event["id"], "result_event_id": result.get("id"),
                   "status": result.get("status", "unknown")}
            if self.content != "metadata":
                row["tool"] = self._label(event.get("tool", ""))
            rows.append(row)
        return rows

    def _case(self, case_id: str) -> dict[str, Any]:
        if self.store is None:
            raise AutError("evidence: case records require a private store")
        case = self.store.get("cases", case_id)
        if case["session_snapshot"] not in self.keys:
            raise AutError("evidence: case is outside this audit or bundle")
        return case

    def case(self, case_id: str) -> dict[str, Any]:
        """Resolve a saved attempt to the captured session and events."""
        case = self._case(case_id)
        from .outputs import commits_in
        session = self._read(case["session_snapshot"], include_text=False)
        return {"case_id": case_id, "session_id": case["session_snapshot"], "trigger": case["trigger"],
                "commit_ids": commits_in(case, session),
                "event_ids": [event["id"] for event in case["events"]],
                "coverage": _coverage(case["coverage"]),
                "omitted_task_events": case.get("omitted_task_events", 0)}

    def ordinary_work(self, metric: Literal["tool_calls", "tool_errors", "codex_model_calls"] = "tool_calls",
                      coverage_basis: Literal["full_case", "tool_events"] | None = None,
                      require_turn_context: bool = False, agent: str = "", before: str = "",
                      include_excluded: bool = False, cursor: str = "", limit: int = 30) -> dict[str, Any]:
        """Find completed attempts with measurable evidence and return references to freeze as cases."""
        _page(0, limit)
        if self.store is None:
            raise AutError("evidence: ordinary-work selection requires a private audit")
        from .study_candidates import ordinary_work_page
        policy: dict[str, Any] = {"metric": metric, "require_turn_context": require_turn_context}
        if coverage_basis is not None:
            policy["coverage_basis"] = coverage_basis
        return ordinary_work_page(self.store, self.describe(), self.keys, self.ambiguous, policy,
                                  agent, before, include_excluded, cursor, limit, self._label)

    def export(self, path: Path, ids: list[str]) -> dict[str, Any]:
        from .outputs import commit_evidence

        if not ids or len(set(ids)) != len(ids):
            raise AutError("evidence: select distinct session IDs for export")
        sessions = {}
        size = 0
        included = set(ids)
        for key in ids:
            row = self._read(key)
            summary = self._summary(row)
            summary["related_sessions"] = [sid for sid in row["related_sessions"] if sid in ids]
            events = row["events"]
            links = row["links"] if self.bundled else event_links(events)
            commits = row.get("commits")
            if commits is None:
                commits = list(commit_evidence(events, links))
            projected = {**summary, "events": [self._event(event, selected=included)
                                               for event in events],
                         "links": links, "commits": commits}
            size += len(canonical(projected).encode("utf-8"))
            if size > BUNDLE_LIMIT:
                raise AutError("evidence: select fewer sessions for the 64 MiB bundle limit")
            sessions[key] = projected
        payload = {"version": 1, "audit_id": self.audit_id, "content": self.content,
                   "coverage": _coverage(self.coverage), "sessions": sessions}
        data = (canonical({"sha256": digest(payload), "evidence": payload}) + "\n").encode("utf-8")
        if len(data) > BUNDLE_LIMIT:
            raise AutError("evidence: select fewer sessions for the 64 MiB bundle limit")
        _create_bundle(path, data)
        return {"path": str(path), "sessions": len(ids), "content": self.content}


def _create_bundle(path: Path, data: bytes) -> None:
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(data)
                stream.close()
                os.link(temp, path)
            finally:
                temp.unlink()
    except FileExistsError as exc:
        raise AutError("evidence: bundle destination already exists") from exc
    except OSError as exc:
        raise AutError("evidence: could not write bundle") from exc


def _read_bundle(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(BUNDLE_LIMIT + 1)
        if len(raw) > BUNDLE_LIMIT:
            raise ValueError
        envelope = json.loads(raw)
        value = envelope["evidence"]
        if (value["version"] != 1 or digest(value) != envelope["sha256"]
                or value["content"] not in MODES or not IDENTITY.fullmatch(value["audit_id"])
                or not isinstance(value["coverage"], dict) or not isinstance(value["sessions"], dict)):
            raise ValueError
        for key, row in value["sessions"].items():
            if (not IDENTITY.fullmatch(key) or row["id"] != key
                    or not isinstance(row["events"], list) or not isinstance(row["coverage"], dict)
                    or not isinstance(row["related_sessions"], list) or not isinstance(row["links"], dict)
                    or row["agent"] not in {"claude_code", "codex", "pi", "cursor_cli", "t3"}
                    or any(related not in value["sessions"] for related in row["related_sessions"])
                    or not all(_valid_event(event) for event in row["events"])):
                raise ValueError
            positions = {event["id"]: (n, event["kind"]) for n, event in enumerate(row["events"])}
            if (len(positions) != len(row["events"])
                    or len(set(row["links"].values())) != len(row["links"]) or any(
                call not in positions or result not in positions or
                positions[call][1] != "call" or positions[result][1] != "result" or
                positions[call][0] >= positions[result][0]
                for call, result in row["links"].items()
            )):
                raise ValueError
            commits = row.get("commits", [])
            if not isinstance(commits, list) or not all(
                isinstance(link, dict) and set(link) == {"observed_sha", "call_event_id", "result_event_id"}
                and re.fullmatch(r"[0-9a-f]{7,40}", link["observed_sha"])
                and isinstance(link["result_event_id"], str)
                and row["links"].get(link["call_event_id"]) == link["result_event_id"]
                for link in commits
            ):
                raise ValueError
        return value
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as exc:
        raise AutError("evidence: bundle is unreadable, too large, or invalid") from exc


def _valid_event(value: Any) -> bool:
    if not isinstance(value, dict) or not EVENT_ID.fullmatch(value.get("id", "")):
        return False
    if value.get("kind") not in EVENT_KINDS or value.get("status", "unknown") not in EVENT_STATUSES:
        return False
    stamp = value.get("timestamp")
    if stamp is not None and parse_ts(stamp) is None:
        return False
    if any(value.get(pos) is not None and type(value[pos]) is not int
           for pos in ("line", "byte_offset")):
        return False
    return (type(value.get("truncated", False)) is bool
            and isinstance(value.get("text", ""), str)
            and value.get("text_source", "snapshot_redacted") in {
                "snapshot_redacted", "captured_full", "redacted_capture"})
