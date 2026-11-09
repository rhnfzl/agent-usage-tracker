"""Hermetic tests for the Cursor usage API collector, against a local fake of the API."""

from __future__ import annotations

import http.server
import sqlite3
import tempfile
import threading
import urllib.error
from pathlib import Path

import pytest

from agent_usage_tracker.event import CollectorUnavailable
from test_collectors import _cfg


def test_a_stray_row_does_not_fail_the_page() -> None:
    from agent_usage_tracker.collectors import cursor_api
    assert cursor_api._page_events(["stray", None], "u", 0) == ([], 0, 0)


def test_cursor_api(monkeypatch) -> None:
    from agent_usage_tracker.collectors import cursor_api
    UID = 114903562
    # _map_event: maps my event; REJECTS a teammate's event (owningUser != uid); skips zero-token.
    mine = {"owningUser": UID, "timestamp": "1784314315897", "model": "composer-2.5",
            "conversationId": "conv-1",
            "tokenUsage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 500}}
    teammate = {**mine, "owningUser": 999999, "conversationId": "conv-2"}
    zero = {"owningUser": UID, "timestamp": "1784314315999", "model": "m", "conversationId": "c",
            "tokenUsage": {"inputTokens": 0, "outputTokens": 0}}
    ev = cursor_api._map_event(mine, UID)
    assert ev is not None and ev.total_tokens == 620 and ev.cache_write_tokens == 0
    assert ev.agent == "cursor" and ev.provider == "cursor" and ev.model == "composer-2.5"
    assert cursor_api._map_event(teammate, UID) is None      # SAFETY: never a teammate's event
    assert cursor_api._map_event(zero, UID) is None
    # privacy hardening: empty/None/'None' ids must NEVER match, even on both sides
    assert not cursor_api._same_user("", "")
    assert not cursor_api._same_user(None, UID)
    assert not cursor_api._same_user("None", None)
    assert not cursor_api._same_user(UID, "")
    assert not cursor_api._same_user([UID], [UID])           # equal containers must NOT match
    assert not cursor_api._same_user({"id": UID}, {"id": UID})
    assert not cursor_api._same_user(True, True)             # bool is an int subclass -> reject
    assert cursor_api._same_user(UID, UID) and cursor_api._same_user(str(UID), UID)
    assert cursor_api._map_event({**mine, "owningUser": ""}, "") is None   # empty both sides -> drop
    # non-str model coerced to None (no crash downstream)
    assert cursor_api._map_event({**mine, "model": {"x": 1}, "conversationId": "c3"}, UID).model is None

    # full collect() with injected HTTP: pagination + incremental watermark + safety filter
    calls = {"pages": []}
    page1 = {"usageEventsDisplay": [mine, teammate,
             {"owningUser": UID, "timestamp": "1784314310000", "model": "composer-2.5",
              "conversationId": "conv-3", "tokenUsage": {"inputTokens": 5, "outputTokens": 5}}]}

    def fake_post(cookie, name, body):
        if name == "get-me":
            return {"userId": UID}
        calls["pages"].append(body.get("page"))
        if body.get("page") != 1:
            return {"usageEventsDisplay": []}
        return {"usageEventsDisplay": [e for e in page1["usageEventsDisplay"]      # the date filter
                                       if body["startDate"] <= int(e["timestamp"]) <= body["endDate"]]}

    cfg = _cfg(Path("/tmp"))
    cfg.cursor_api = True
    monkeypatch.setattr(cursor_api, "_now_ms", lambda: 1784314315897 + 60_000)
    with monkeypatch.context() as m:
        m.setattr(cursor_api, "_auth", lambda c: ("cookie", 3867693))
        m.setattr(cursor_api, "_post", fake_post)
        wm: dict = {}
        events = []
        for batch, commit in cursor_api.collect(cfg, wm):
            events.extend(batch)
            commit()
        # 2 of my events emitted (teammate filtered out); watermark = newest ts
        assert len(events) == 2, events
        assert all(e.agent == "cursor" for e in events)
        assert wm["since_ms"] == 1784314315897, wm
        # a second scan with only-older events -> nothing new
        page1["usageEventsDisplay"] = [{"owningUser": UID, "timestamp": "1784314310000",
                                        "model": "m", "conversationId": "conv-3",
                                        "tokenUsage": {"inputTokens": 1, "outputTokens": 1}}]
        again = [e for batch, _ in cursor_api.collect(cfg, wm) for e in batch]
        assert again == [], again                            # older than watermark -> skipped
    # a same-ms event at the watermark must NOT be permanently skipped (stop is strictly-older)
    wm2 = {"since_ms": 1784314315897}
    page1["usageEventsDisplay"] = [
        {"owningUser": UID, "timestamp": "1784314315897", "model": "m", "conversationId": "same-ms",
         "tokenUsage": {"inputTokens": 3, "outputTokens": 3}},          # == watermark -> re-emitted
        {"owningUser": UID, "timestamp": "1784314310000", "model": "m", "conversationId": "older",
         "tokenUsage": {"inputTokens": 1, "outputTokens": 1}}]          # older -> stops paging
    with monkeypatch.context() as m:
        m.setattr(cursor_api, "_auth", lambda c: ("ck", 1))
        m.setattr(cursor_api, "_post", fake_post)
        boundary = [e for batch, _ in cursor_api.collect(cfg, wm2) for e in batch]
    assert len(boundary) == 1 and boundary[0].total_tokens == 6, boundary   # same-ms emitted, not skipped

    # _auth must NOT crash on a malformed stored token (non-JWT / bad base64) -> returns None
    for bad in ("not-a-jwt", "a.b", "x.@@@notbase64@@@.z", ""):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (bad,))
            con.commit()
            con.close()
            c = _cfg(Path(d))
            c.cursor_db_path = str(db)
            assert cursor_api._auth(c) is None, bad          # malformed -> no auth, no crash
    # disabled by default -> no network, no events
    assert list(cursor_api.collect(_cfg(Path("/tmp")), {})) == []


def test_cursor_cookie_not_sent_through_a_redirect(monkeypatch) -> None:
    from agent_usage_tracker.collectors import cursor_api
    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self) -> None:
            seen.append((self.path, self.headers.get("Cookie")))
            self.send_response(302 if self.path.endswith("get-me") else 200)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = _answer

        def log_message(self, *args) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(cursor_api, "_BASE", f"http://127.0.0.1:{srv.server_port}")
    try:
        try:
            cursor_api._post("secret-cookie", "get-me", {})
            raise AssertionError("a 3xx must not be followed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 302
    finally:
        srv.shutdown()
        srv.server_close()
    assert [p for p, _c in seen] == ["/get-me"], seen      # the cookie never reached /elsewhere


def test_cursor_api_token_gone_is_unavailable_not_silent() -> None:
    from agent_usage_tracker.collectors import cursor_api
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.vscdb"
        cfg = _cfg(Path(d))
        cfg.cursor_api, cfg.cursor_db_path = True, str(db)
        assert list(cursor_api.collect(cfg, {})) == []         # Cursor not installed: nothing to say
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        con.commit()
        con.close()
        try:
            list(cursor_api.collect(cfg, {}))
            raise AssertionError("a Cursor with no usable token must not look like a quiet week")
        except CollectorUnavailable as exc:
            assert "token" in str(exc)


def _fake_api(events: list[dict], calls: list[dict]):
    """A stand-in for the dashboard API: events in [startDate, endDate], newest first, paged."""
    def post(cookie, name, body):
        if name == "get-me":
            return {"userId": "u1"}
        calls.append(body)
        hits = sorted((e for e in events
                       if body["startDate"] <= int(e["timestamp"]) <= body["endDate"]),
                      key=lambda e: -int(e["timestamp"]))
        page, size = body["page"], body["pageSize"]
        return {"usageEventsDisplay": hits[(page - 1) * size:page * size]}
    return post


def test_a_long_gap_with_more_than_the_page_cap_loses_nothing(monkeypatch) -> None:
    from agent_usage_tracker.collectors import cursor_api
    now = 1_790_000_000_000
    since = now - 30 * cursor_api._DAY_MS
    events = [{"owningUser": "u1", "timestamp": str(since + i * 100_000), "model": "m",
               "conversationId": f"c{i}", "tokenUsage": {"inputTokens": 1, "outputTokens": 1}}
              for i in range(25_000)]                     # about 29 days, far past 200 pages x 100
    calls: list[dict] = []
    cfg = _cfg(Path("/tmp"))
    cfg.cursor_api = True
    monkeypatch.setattr(cursor_api, "_auth", lambda c: ("cookie", 1))
    monkeypatch.setattr(cursor_api, "_post", _fake_api(events, calls))
    monkeypatch.setattr(cursor_api, "_now_ms", lambda: now)
    wm: dict = {"since_ms": since}
    seen: set[str] = set()
    marks = []
    for batch, commit in cursor_api.collect(cfg, wm):
        seen.update(e.event_id for e in batch)
        commit()
        marks.append(wm["since_ms"])
    assert len(seen) == 25_000                            # none lost to the page cap
    assert len(marks) > 1 and marks == sorted(marks)      # the bookmark moves after each window
    assert all(c["endDate"] - c["startDate"] <= cursor_api._WINDOW_MS + 2 for c in calls)
    assert since + 24_999 * 100_000 <= wm["since_ms"] < now   # past the newest event, never `now`
    assert [e for b, _ in cursor_api.collect(cfg, wm) for e in b
            if e.event_id not in seen] == []              # a rerun finds nothing new


def test_a_crash_mid_gap_resumes_at_the_last_window(monkeypatch) -> None:
    from agent_usage_tracker.collectors import cursor_api
    now = 1_790_000_000_000
    since = now - 5 * cursor_api._DAY_MS
    events = [{"owningUser": "u1", "timestamp": str(since + i * 3_600_000), "model": "m",
               "conversationId": f"c{i}", "tokenUsage": {"inputTokens": 1, "outputTokens": 1}}
              for i in range(5 * 24)]
    cfg = _cfg(Path("/tmp"))
    cfg.cursor_api = True
    monkeypatch.setattr(cursor_api, "_auth", lambda c: ("cookie", 1))
    monkeypatch.setattr(cursor_api, "_post", _fake_api(events, []))
    monkeypatch.setattr(cursor_api, "_now_ms", lambda: now)
    wm: dict = {"since_ms": since}
    gen = cursor_api.collect(cfg, wm)
    _first, commit = next(gen)
    commit()                                              # one window delivered, then the run dies
    gen.close()
    assert wm["since_ms"] == since + cursor_api._WINDOW_MS
    rest = {e.event_id for b, c in cursor_api.collect(cfg, wm) for e in b}
    assert len(rest) == 5 * 24 - 24                       # the next run starts at that window


@pytest.mark.parametrize("exclusive", [False, True])
def test_capped_window_splits_and_keeps_boundary_events(monkeypatch, exclusive):
    from agent_usage_tracker.collectors import cursor_api
    events = [{"owningUser": "u1", "timestamp": str(i), "model": "m",
               "conversationId": f"c{i}", "tokenUsage": {"inputTokens": 1}}
              for i in range(100, 121)]
    calls = []
    api = _fake_api(events, calls)

    def post(cookie, name, body):
        if exclusive and name != "get-me":
            body = {**body, "startDate": body["startDate"] + 1,
                    "endDate": body["endDate"] - 1}
        return api(cookie, name, body)

    monkeypatch.setattr(cursor_api, "_post", post)
    monkeypatch.setattr(cursor_api, "_PAGE", 3)
    monkeypatch.setattr(cursor_api, "_MAX_PAGES", 2)
    found, newest = cursor_api._fetch_events("cookie", 1, "u1", 100, 100, 120)
    assert {e.event_id for e in found} == {
        cursor_api._map_event(e, "u1").event_id for e in events}
    assert len(found) == len(events)
    assert newest == 120
    assert len(calls) < 100


def test_same_timestamp_over_cap_refuses_watermark_ack(monkeypatch):
    from agent_usage_tracker.collectors import cursor_api
    events = [{"owningUser": "u1", "timestamp": "100", "model": "m",
               "conversationId": f"c{i}", "tokenUsage": {"inputTokens": 1}}
              for i in range(7)]
    calls = []
    cfg = _cfg(Path("/tmp"))
    cfg.cursor_api = True
    monkeypatch.setattr(cursor_api, "_auth", lambda cfg: ("cookie", 1))
    monkeypatch.setattr(cursor_api, "_post", _fake_api(events, calls))
    monkeypatch.setattr(cursor_api, "_now_ms", lambda: 120)
    monkeypatch.setattr(cursor_api, "_PAGE", 3)
    monkeypatch.setattr(cursor_api, "_MAX_PAGES", 2)
    wm = {"since_ms": 100}
    with pytest.raises(CollectorUnavailable, match="cap"):
        for _batch, commit in cursor_api.collect(cfg, wm):
            commit()
    assert wm == {"since_ms": 100}
    assert len(calls) < 30
