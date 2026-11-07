"""Index paths once per Source; keep bounded context reads fresh and targeted."""

import json
from datetime import timedelta

import pytest

from agent_usage_tracker.collectors import claude_code
from agent_usage_tracker.event import make_event_id
from agent_usage_tracker.label import context, tasks
from label_helpers import DAY1, _cfg, _steer
from test_cursor_cli import A, B, _prompt, _store
from test_label_context import UNTIL, codex_log, write_log


def log(home, sid):
    path = home / ".claude/projects/p" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"type": "user", "uuid": sid, "sessionId": sid, "timestamp": DAY1.isoformat(),
                "message": {"content": "synthetic question"}}]
    records.extend({"type": "assistant", "uuid": f"{sid}-{i}", "sessionId": sid,
                    "timestamp": (DAY1+timedelta(minutes=i)).isoformat(),
                    "message": {"content": f"answer {i}"}} for i in (1, 10))
    path.write_text("\n".join(map(json.dumps, records)))
    return str(path)


def test_twenty_unit_pairs_discover_once_with_identical_cutoff_results(tmp_path, monkeypatch):
    selected = log(tmp_path, "selected")
    for i in range(30):
        log(tmp_path, f"unrelated-{i}")
    steer = _steer("selected", "selected", DAY1)
    eid = steer["event_id"]
    cutoffs = [DAY1+timedelta(minutes=n+1) for n in range(20)]
    expected = [(claude_code.steer_texts(tmp_path, {"selected"}, {eid}),
                 claude_code.agent_replies(tmp_path, {"selected"}, {eid}, cutoff))
                for cutoff in cutoffs]
    discoveries, reads = [], []
    discover = claude_code.main_session_files
    read_texts, read_replies = claude_code.steer_texts, claude_code.agent_replies

    def paths(home):
        discoveries.append(home)
        return discover(home)

    def texts(*args, **kw):
        reads.append(list(kw["paths"]))
        return read_texts(*args, **kw)

    def replies(*args, **kw):
        reads.append(list(kw["paths"]))
        return read_replies(*args, **kw)

    monkeypatch.setattr(claude_code, "main_session_files", paths)
    monkeypatch.setattr(claude_code, "steer_texts", texts)
    monkeypatch.setattr(claude_code, "agent_replies", replies)
    src = tasks.Source(_cfg(tmp_path), local_context=True)
    actual = [(src.texts([steer], cutoff), src.replies([steer], cutoff)) for cutoff in cutoffs]
    assert actual == expected
    assert len(discoveries) == 1
    assert reads == [[selected]] * 40
    assert actual[0][1][eid][1] == "" and actual[-1][1][eid][1] == "answer 10"


def test_new_source_sees_new_files_but_snapshot_does_not_change(tmp_path):
    log(tmp_path, "first")
    cfg = _cfg(tmp_path)
    src = tasks.Source(cfg, local_context=True)
    first = _steer("first", "first", DAY1)
    assert src.texts([first])
    log(tmp_path, "new")
    new = _steer("new", "new", DAY1)
    assert src.texts([new]) == {}
    assert tasks.Source(cfg, local_context=True).texts([new]) == {
        new["event_id"]: "synthetic question"}


def test_index_preserves_discovery_order_for_duplicate_event_ids(tmp_path, monkeypatch):
    a, b = log(tmp_path, "a"), log(tmp_path, "b")
    for path, text in [(a, "first result"), (b, "second result")]:
        with open(path) as source:
            rows = [json.loads(line) for line in source]
        rows[0]["uuid"] = "shared"
        rows[0]["message"]["content"] = text
        with open(path, "w") as out:
            out.write("\n".join(map(json.dumps, rows)))
    monkeypatch.setattr(claude_code, "main_session_files", lambda home: [b, a])
    steers = [_steer("shared", sid, DAY1) for sid in ("a", "b")]
    assert tasks.Source(_cfg(tmp_path)).texts(steers) == {
        steers[0]["event_id"]: "first result"}


@pytest.mark.parametrize("agent", ["codex", "pi", "cursor_cli"])
def test_other_agent_indexes_use_canonical_identity_and_only_selected_paths(tmp_path, monkeypatch, agent):
    if agent == "codex":
        eid = codex_log(tmp_path)[0]
        sid = "thread"
        selected = str(tmp_path / ".codex/sessions/2026/rollout-fixture.jsonl")
        write_log(tmp_path / ".codex/sessions/rollout-other.jsonl", [
            {"type": "session_meta", "payload": {"id": "other", "source": "cli"}}])
        module, discovery, reader = context.codex, "session_files", "codex_context"
    elif agent == "pi":
        sid = "pi-thread"
        selected = str(tmp_path / ".pi/agent/sessions/arbitrary-filename.jsonl")
        write_log(tmp_path / ".pi/agent/sessions/arbitrary-filename.jsonl", [
            {"type": "session", "id": sid}, {"type": "message", "id": "u",
             "timestamp": "2026-11-01T10:01:00Z",
             "message": {"role": "user", "content": "write tests"}}])
        write_log(tmp_path / ".pi/agent/sessions/other.jsonl", [{"type": "session", "id": "other"}])
        eid = make_event_id("pi", "steer", f"{sid}|u")
        module, discovery, reader = context.pi, "session_files", "pi_context"
    else:
        sid = A
        selected = str(_store(tmp_path, A, [[_prompt("r1", "human question")]]))
        _store(tmp_path, B, [[_prompt("r2", "other question")]])
        eid = make_event_id("cursor_cli", "steer", f"{sid}|r1")
        module, discovery, reader = context.cursor_cli, "chat_stores", "cursor_context"
    discoveries, reads = [], []
    discover, read = getattr(module, discovery), getattr(context, reader)

    def paths(home):
        discoveries.append(home)
        return discover(home)

    def bounded(*args, **kw):
        reads.append(list(kw["paths"]))
        return read(*args, **kw)

    monkeypatch.setattr(module, discovery, paths)
    monkeypatch.setattr(context, reader, bounded)
    src = tasks.Source(_cfg(tmp_path), local_context=True)
    rows = [{"agent": agent, "session_id": sid, "event_id": eid}]
    for cutoff in (UNTIL, UNTIL+timedelta(minutes=1)):
        assert src.texts(rows, cutoff)
        src.replies(rows, cutoff)
    assert len(discoveries) == 1 and reads == [[selected], [selected]]
