"""Synthetic log fixtures exercise exact IDs, local-only routing, and historical boundaries."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_usage_tracker.collectors import cursor_cli
from agent_usage_tracker.collectors import codex
from agent_usage_tracker.event import CollectorUnavailable, make_event_id
from agent_usage_tracker.label import context, tasks
from label_helpers import FakeSource, _cfg
from test_cursor_cli import A, B, _msg, _prompt, _store

UNTIL = datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc)


def write_log(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def codex_log(home, sid="thread", source="cli", fork=False):
    ts = "2026-11-01T10:01:00Z"
    prompt = {"type": "event_msg", "timestamp": ts,
              "payload": {"type": "user_message", "message": "fix bob@example.com"}}
    copied = {**prompt, "payload": {"type": "user_message", "message": "parent only"}}
    meta = {"id": sid, "source": source}
    if fork:
        meta.update(forked_from_id="parent", subagent_history_start_ordinal=2)
    rows = [{"type": "session_meta", "payload": meta}]
    if fork:
        rows.append(copied)
    rows += [prompt, {"type": "response_item", "timestamp": "2026-11-01T10:02:00Z",
                     "payload": {"type": "message", "role": "assistant", "content": [
                         {"type": "reasoning", "text": "hidden"},
                         {"type": "output_text", "text": "fixed first"}]}},
             {"type": "event_msg", "timestamp": "2026-11-01T10:03:00Z",
              "payload": {"type": "user_message", "message": "now review"}},
             {"type": "event_msg", "timestamp": "2026-11-01T10:05:00Z",
              "payload": {"type": "agent_message", "message": "future reply"}}]
    path = home / ".codex/sessions/2026/rollout-fixture.jsonl"
    write_log(path, rows)
    return [make_event_id("codex", "steer", sid, stamp, i) for i, stamp in enumerate(
        [ts, "2026-11-01T10:03:00Z"], 1)]


@pytest.mark.parametrize("fork", [False, True])
def test_codex_ids_forks_replies_and_until(tmp_path, fork):
    first, second = codex_log(tmp_path, fork=fork)
    got = context.codex_context(tmp_path, {"thread"}, {first, second}, UNTIL, tasks.Source(_cfg(tmp_path)).keys)
    assert got[first] == context.Context("fix bob@example.com", "", "fixed first")
    assert got[second] == context.Context("now review", "fixed first", "")
    assert context.codex_context(tmp_path, {"thread"}, {first, second}, None, tasks.Source(_cfg(tmp_path)).keys)[second].after \
        == "future reply"


@pytest.mark.parametrize("source", ["exec", {"subagent": "reviewer"}])
def test_codex_software_prompts_are_not_human_context(tmp_path, source):
    ids = codex_log(tmp_path, source=source)
    assert context.codex_context(tmp_path, {"thread"}, set(ids), None, tasks.Source(_cfg(tmp_path)).keys) == {}


def test_pi_copied_history_and_assistant_only_text(tmp_path):
    path = tmp_path / ".pi/agent/sessions/p/session.jsonl"
    rows = [{"type": "session", "id": "pi-thread", "parentSession": "parent.jsonl",
             "timestamp": "2026-11-01T10:00:00Z"},
            {"type": "message", "id": "old", "timestamp": "2026-10-30T10:00:00Z",
             "message": {"role": "user", "content": "copied"}},
            {"type": "message", "id": "u1", "timestamp": "2026-11-01T10:01:00Z",
             "message": {"role": "user", "content": "write tests"}},
            {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-11-01T10:02:00Z",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "secret"},
                 {"type": "text", "text": "tests pass"}]}},
            {"type": "message", "id": "t1", "timestamp": "2026-11-01T10:03:00Z",
             "message": {"role": "toolResult", "content": "raw tool output"}}]
    write_log(path, rows)
    eid = make_event_id("pi", "steer", "pi-thread|u1")
    old = make_event_id("pi", "steer", "pi-thread|old")
    got = context.pi_context(tmp_path, {"pi-thread"}, {eid, old}, UNTIL)
    assert got == {eid: context.Context("write tests", "", "tests pass")}


def test_cursor_prompts_followup_ids_and_no_unverifiable_reply(tmp_path):
    first = _prompt("r1", "first")
    follow = _prompt("r1", "followup")
    summary = _msg("user", [{"type": "text", "text": "summary"}], requestId="sum", isSummary=True)
    _store(tmp_path, A, [[first, _msg("assistant", [{"type": "text", "text": "later"}]),
                          follow, summary]])
    _store(tmp_path, B, [[_prompt("sub", "agent generated")]], subagentInfo={"parentAgentId": A})
    import hashlib
    mid = hashlib.sha256(json.dumps(follow).encode()).hexdigest()
    eid = make_event_id("cursor_cli", "steer", f"{A}|r1")
    eid2 = make_event_id("cursor_cli", "steer", f"{A}|r1|{mid}")
    keys = tasks.Source(_cfg(tmp_path)).keys
    got = context.cursor_context(tmp_path, {A, B}, {eid, eid2}, UNTIL, keys)
    assert got[eid].text.strip() == "first" and got[eid2].text.strip() == "followup"
    assert all(value.before == value.after == "" for value in got.values())


def test_cursor_read_is_sqlite_read_only(tmp_path, monkeypatch):
    path = _store(tmp_path, A, [[_prompt("r", "hello")]])
    before = path.read_bytes()
    called = []
    original = cursor_cli.connect_ro

    def readonly(path):
        con = original(path)
        assert con.execute("PRAGMA query_only").fetchone() == (1,)
        called.append(path)
        return con

    monkeypatch.setattr(context, "connect_ro", readonly)
    context.cursor_context(tmp_path, {A}, set(), None, tasks.Source(_cfg(tmp_path)).keys)
    assert called and before == path.read_bytes()


def test_unreadable_cursor_store_fails_without_silent_partial_coverage(tmp_path, monkeypatch):
    _store(tmp_path, A, [[_prompt("r", "hello")]])
    monkeypatch.setattr(context, "connect_ro", lambda path: None)
    with pytest.raises(CollectorUnavailable):
        context.cursor_context(tmp_path, {A}, set(), None, tasks.Source(_cfg(tmp_path)).keys)


def test_source_keeps_cloud_boundary_and_redacts_local_text(tmp_path):
    eid = codex_log(tmp_path)[0]
    rows = [{"event_id": eid, "agent": "codex", "session_id": "thread"}]
    assert tasks.Source(_cfg(tmp_path)).texts(rows) == {}
    local = tasks.Source(_cfg(tmp_path), local_context=True)
    assert local.texts(rows) == {eid: "fix [redacted]"}
    assert local.replies(rows, UNTIL) == {eid: ("", "fixed first")}


def test_nonclaude_inputs_route_through_all_local_tasks(tmp_path):
    eid = codex_log(tmp_path)[0]
    at = UNTIL - timedelta(minutes=4)
    row = {"event_id": eid, "agent": "codex", "session_id": "thread", "ts": at, "attrs": {}}
    ep = {"id": "episode", "first_ts": at, "last_ts": at, "outcome": "no_code",
          "members": [{"session_id": "thread"}]}
    src = FakeSource(_cfg(tmp_path), [row], [ep])
    src.context_agents = context.AGENTS
    assert tasks.units("steer_intent", src, at, UNTIL)[0].facts["text"] == "fix [redacted]"
    day = at.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assert tasks.units("work_category", src, day, day + timedelta(days=1))[0].facts["first_steer"] \
        == "fix [redacted]"
    assert tasks.units("soft_outcome", src, at, UNTIL)[0].facts["last_steers"] == ["fix [redacted]"]


def test_reply_to_unrequested_prompt_does_not_attach_to_previous_input():
    assert context._join([("one", "first"), ("two", "second"), (None, "reply second")],
                         {"one"}) == {"one": context.Context("first")}


def test_codex_distinct_and_repeated_metadata_matches_collector_ids(tmp_path):
    codex_log(tmp_path)
    path = next((tmp_path / ".codex/sessions").rglob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["payload"]["session_id"] = "legacy-seed"
    rows.insert(3, {"type": "session_meta", "payload": {"id": "copied-parent",
                                                         "session_id": "next-seed"}})
    write_log(path, rows)
    keys = tasks.Source(_cfg(tmp_path)).keys
    cur = codex._fresh_state(path.stat())
    from collections import Counter
    records = codex.parsed_records(path.read_bytes().splitlines(), 0, cur, Counter())
    events = codex._chunk_rows(str(path), records, cur, keys, Counter())
    steers = [e for e in events if getattr(e, "kind", None) == "steer"]
    assert len(steers) == 2 and {e.session_id for e in steers} == {"thread"}
    got = context.codex_context(tmp_path, {"thread"}, {e.event_id for e in steers}, UNTIL, keys)
    assert {value.text for value in got.values()} == {"fix bob@example.com", "now review"}


def test_pi_replies_follow_parent_links_not_append_order(tmp_path):
    path = tmp_path / ".pi/agent/sessions/p/branch.jsonl"

    def msg(rid, parent, role, text):
        return {"type": "message", "id": rid, "parentId": parent,
                "timestamp": "2026-11-01T10:01:00Z", "message": {"role": role, "content": text}}

    write_log(path, [{"type": "session", "id": "s"},
                     msg("u1", None, "user", "first branch"),
                     msg("a1", "u1", "assistant", "first reply"),
                     msg("u2", None, "user", "other branch"),
                     msg("a2", "u1", "assistant", "alternate first reply")])
    one, two = (make_event_id("pi", "steer", f"s|{u}") for u in ("u1", "u2"))
    got = context.pi_context(tmp_path, {"s"}, {one, two}, None)
    assert got[two] == context.Context("other branch")
    assert got[one] == context.Context("first branch")  # two terminal replies are ambiguous


def test_source_reads_one_context_snapshot_for_text_and_replies(tmp_path, monkeypatch):
    first = codex_log(tmp_path)[0]
    rows = [{"event_id": first, "agent": "codex", "session_id": "thread"}]
    calls = []
    original = context.codex_context

    def counted(*args, **kw):
        calls.append(True)
        return original(*args, **kw)

    monkeypatch.setattr(context, "codex_context", counted)
    src = tasks.Source(_cfg(tmp_path), local_context=True)
    assert src.texts(rows, UNTIL)
    assert src.replies(rows, UNTIL)
    assert len(calls) == 1


@pytest.mark.parametrize("incremental", [False, True])
def test_codex_old_to_new_format_preserves_collected_ordinals(tmp_path, incremental):
    from collections import Counter
    path = tmp_path / ".codex/sessions/rollout-transition.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "transition", "source": "cli"}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:01:00Z", "payload": {
                "type": "item_completed", "item": {"type": "UserMessage", "content": [
                    {"type": "text", "text": "old prompt"}]}}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:02:00Z", "payload": {
                "type": "user_message", "message": "new prompt"}}]
    write_log(path, rows)
    keys = tasks.Source(_cfg(tmp_path)).keys
    cur = codex._fresh_state(path.stat())
    chunks = [rows[:2], rows[2:]] if incremental else [rows]
    events = []
    for offset, chunk in enumerate(chunks):
        parsed = codex.parsed_records([json.dumps(o).encode() for o in chunk], offset,
                                      cur, Counter())
        events += codex._chunk_rows(str(path), parsed, cur, keys, Counter())
    steers = [e for e in events if getattr(e, "kind", None) == "steer"]
    assert len(steers) == (2 if incremental else 1)
    got = context.codex_context(tmp_path, {"transition"}, {e.event_id for e in steers}, UNTIL, keys)
    assert set(got) == {e.event_id for e in steers}
    assert {v.text for v in got.values()} == ({"old prompt", "new prompt"}
                                             if incremental else {"new prompt"})


def test_outcome_context_stays_inside_its_own_episode(tmp_path):
    from label_helpers import _claude_log, _steer
    at = UNTIL - timedelta(hours=1)
    _claude_log(tmp_path, "s", [("old", "copied old task"), ("current", "current task"),
                                  ("next", "unrelated later task")])
    rows = [_steer("old", "s", at-timedelta(days=2)), _steer("current", "s", at),
            _steer("next", "s", at+timedelta(days=2))]
    episode = {"id": "e", "first_ts": at, "last_ts": at, "outcome": "no_code",
               "members": [{"session_id": "s"}]}
    src = FakeSource(_cfg(tmp_path), rows, [episode])
    req = tasks.units("soft_outcome", src, at-timedelta(days=3), at+timedelta(days=3))[0]
    assert req.facts["last_steers"] == ["current task"]


def test_codex_paired_old_and_modern_representation_does_not_shift_later_ids(tmp_path):
    from collections import Counter
    path = tmp_path / ".codex/sessions/rollout-paired.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "paired", "source": "cli"}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:01:00Z", "payload": {
                "type": "item_completed", "item": {"type": "UserMessage", "content": [
                    {"type": "text", "text": "first"}]}}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:01:00Z",
             "payload": {"type": "user_message", "message": "first"}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:02:00Z",
             "payload": {"type": "agent_message", "message": "reply first"}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:03:00Z",
             "payload": {"type": "user_message", "message": "second"}},
            {"type": "event_msg", "timestamp": "2026-11-01T10:04:00Z",
             "payload": {"type": "agent_message", "message": "reply second"}}]
    write_log(path, rows)
    keys = tasks.Source(_cfg(tmp_path)).keys
    cur = codex._fresh_state(path.stat())
    parsed = codex.parsed_records(path.read_bytes().splitlines(), 0, cur, Counter())
    events = codex._chunk_rows(str(path), parsed, cur, keys, Counter())
    steers = [e for e in events if getattr(e, "kind", None) == "steer"]
    got = context.codex_context(tmp_path, {"paired"}, {e.event_id for e in steers}, UNTIL, keys)
    assert len(got) == 2
    assert {v.text: v.after for v in got.values()} == {"first": "reply first", "second": "reply second"}


def test_outcome_does_not_use_a_reply_after_episode_end_or_to_an_earlier_input(tmp_path):
    from label_helpers import _steer
    at = datetime(2026, 11, 1, 10, 1, tzinfo=timezone.utc)
    path = tmp_path / ".claude/projects/p/s.jsonl"

    def rec(kind, uid, minute, text):
        return {"type": kind, "uuid": uid, "sessionId": "s",
                "timestamp": f"2026-11-01T10:{minute:02d}:00Z", "message": {"content": text}}

    write_log(path, [rec("user", "u1", 1, "first task"), rec("assistant", "a1", 2, "first done"),
                     rec("user", "u2", 3, "still pending"), rec("assistant", "a2", 5, "later done")])
    rows = [_steer("u1", "s", at), _steer("u2", "s", at+timedelta(minutes=2))]
    episode = {"id": "e", "first_ts": at, "last_ts": at+timedelta(minutes=2),
               "outcome": "no_code", "members": [{"session_id": "s"}]}
    src = FakeSource(_cfg(tmp_path), rows, [episode])
    assert tasks.units("soft_outcome", src, at, UNTIL+timedelta(hours=1))[0].facts[
        "last_agent_reply"] == ""
    src.require_outcome_reply = True
    assert tasks.units("soft_outcome", src, at, UNTIL+timedelta(hours=1)) == []
    assert src.incomplete_outcome_units == [at]
