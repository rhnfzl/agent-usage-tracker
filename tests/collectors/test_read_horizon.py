"""A native scan finishes its admitted history while live writers continue."""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import asdict

import pytest

from agent_usage_tracker.collectors import _util, drain
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import UsageEvent


def config(home):
    return Config(machine="test", agents=(), webhook_url=None, webhook_secret=None,
                  pg_dsn=None, home=home)


def source(paths):
    def collect(cfg, wm):
        def rows(path, cur, off, lines, skipped):
            return [UsageEvent(raw.decode().strip(), "2020-01-01T00:00:00Z", "test", None,
                               None) for raw in lines]
        yield from _util.file_batches(
            "test", paths(), wm.setdefault("files", {}),
            lambda st: {"off": 0, "ino": st.st_ino}, rows)
    return collect


def consume(collect, home, wm):
    events = []
    passes = 0
    for batches in drain(collect, config(home), wm, "test"):
        passes += 1
        for rows, advance in batches:
            events.extend(asdict(row) for row in rows)
            advance()
    return events, passes


def test_live_appends_wait_for_the_next_run_without_losing_initial_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(_util, "_MAX_LINES", 2)
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"0\n1\n2\n3\n4\n")
    base = source(lambda: [str(path)])
    calls = 0

    def growing(cfg, wm):
        nonlocal calls
        yield from base(cfg, wm)
        calls += 1
        if calls < 8:
            with path.open("ab") as stream:
                stream.write(f"later-{calls}\n".encode())

    wm = {}
    first, passes = consume(growing, tmp_path, wm)
    assert [row["event_id"] for row in first] == [str(i) for i in range(5)]
    assert passes == 4
    rest, _ = consume(base, tmp_path, wm)
    actual = first + rest
    expected, _ = consume(base, tmp_path, {})
    assert actual == expected
    end_state = copy.deepcopy(wm)
    stable = {}
    consume(base, tmp_path, stable)
    assert end_state == stable


def test_new_paths_wait_but_all_initial_roots_are_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr(_util, "_MAX_LINES", 1)
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
        (root / "initial.jsonl").write_bytes(root.name.encode() + b"\nmore-" +
                                             root.name.encode() + b"\n")
    calls = 0

    def collect(cfg, wm):
        nonlocal calls
        calls += 1
        for root in roots:
            yield from source(lambda root=root: sorted(map(str, root.glob("*.jsonl"))))(cfg, wm)
        if calls == 1:
            (roots[0] / "new.jsonl").write_bytes(b"new\n")

    wm = {}
    first, _ = consume(collect, tmp_path, wm)
    assert {r["event_id"] for r in first} == {"one", "two", "more-one", "more-two"}
    rest, _ = consume(collect, tmp_path, wm)
    assert [r["event_id"] for r in rest] == ["new"]


@pytest.mark.parametrize("mode", ["export", "backfill"])
def test_one_shot_commands_drain_both_initial_claude_roots(tmp_path, monkeypatch, mode):
    import io

    from agent_usage_tracker import scan
    from test_claude_profiles import configured_profile
    from test_collectors import _write_jsonl

    monkeypatch.setattr(_util, "_MAX_LINES", 1)
    home = tmp_path / "home"
    profile = tmp_path / "profile"
    configured_profile(home, profile)
    for root in (home / ".claude", profile):
        _write_jsonl(root / "projects/p/s.jsonl", [
            {"type": "assistant", "uuid": f"{root.name}-{i}", "sessionId": root.name,
             "timestamp": "2020-01-01T00:00:00Z", "message": {"id": f"{root.name}-{i}",
             "model": "m", "usage": {"input_tokens": 1}}} for i in range(3)])
    if mode == "export":
        count, skipped, failed = scan.run_export(config(home), ["claude_code"], io.StringIO())
        assert (count, skipped, failed) == (6, 0, set())
    else:
        counts = scan.run_history_backfill(config(home), ["claude_code"], dry_run=True)
        assert counts["rows"] == 6 and not counts["failed"]


@pytest.mark.parametrize("replace", [False, True])
def test_changed_generation_waits_for_the_next_run(tmp_path, monkeypatch, replace):
    monkeypatch.setattr(_util, "_MAX_LINES", 1)
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\ntwo\nthree\n")
    base = source(lambda: [str(path)])
    changed = False

    def collect(cfg, wm):
        nonlocal changed
        yield from base(cfg, wm)
        if not changed:
            changed = True
            if replace:
                other = tmp_path / "replacement"
                other.write_bytes(b"new\n")
                other.replace(path)
            else:
                path.write_bytes(b"new\n")

    wm = {}
    first, _ = consume(collect, tmp_path, wm)
    assert [r["event_id"] for r in first] == ["one"]
    assert str(path) not in wm["files"]
    rest, _ = consume(base, tmp_path, wm)
    assert [r["event_id"] for r in rest] == ["new"]


def test_observed_truncation_then_regrowth_restarts_from_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(_util, "_MAX_LINES", 1)
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"initial-one\ninitial-two\n")
    base = source(lambda: [str(path)])
    shortened = False

    def collect(cfg, wm):
        nonlocal shortened
        yield from base(cfg, wm)
        if not shortened:
            shortened = True
            path.write_bytes(b"new\n")

    wm = {}
    first, _ = consume(collect, tmp_path, wm)
    assert [r["event_id"] for r in first] == ["initial-one"]
    assert str(path) not in wm["files"]
    with path.open("ab") as stream:
        stream.write(b"a-longer-replacement-prefix\n")
    rest, _ = consume(base, tmp_path, wm)
    assert [r["event_id"] for r in rest] == ["new", "a-longer-replacement-prefix"]


def test_replacement_between_stat_and_open_does_not_enter_the_snapshot(tmp_path, monkeypatch):
    import builtins

    path, replacement = tmp_path / "a.jsonl", tmp_path / "replacement"
    path.write_bytes(b"original\n")
    replacement.write_bytes(b"replacement\n")
    real_open = builtins.open
    replaced = False

    def opening(file, *args, **kwargs):
        nonlocal replaced
        if str(file) == str(path) and not replaced:
            replaced = True
            replacement.replace(path)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", opening)
    wm = {}
    base = source(lambda: [str(path)])
    assert consume(base, tmp_path, wm)[0] == []
    assert str(path) not in wm["files"]
    later, _ = consume(base, tmp_path, wm)
    assert [row["event_id"] for row in later] == ["replacement"]


def test_partial_final_line_completed_later_stays_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(_util, "_MAX_LINES", 1)
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\ntw")
    base = source(lambda: [str(path)])
    appended = False

    def collect(cfg, wm):
        nonlocal appended
        yield from base(cfg, wm)
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write(b"o\n")

    wm = {}
    first, _ = consume(collect, tmp_path, wm)
    assert [r["event_id"] for r in first] == ["one"]
    assert wm["files"][str(path)]["off"] == 4
    rest, _ = consume(base, tmp_path, wm)
    assert [r["event_id"] for r in rest] == ["two"]


def test_snapshot_scope_ends_before_yield_and_closes_abandoned_sources(tmp_path):
    first = tmp_path / "first"
    outside = tmp_path / "outside"
    first.write_bytes(b"first\n")
    outside.write_bytes(b"outside\n")
    closed = []

    def collect(cfg, wm):
        try:
            yield from source(lambda: [str(first)])(cfg, wm)
        finally:
            closed.append(True)

    scan = drain(collect, config(tmp_path), {}, "test")
    batches = next(scan)
    _rows, _advance = next(batches)
    # An unrelated direct read while the outer generator is suspended sees no snapshot.
    assert list(_util.changed_files([str(outside)], {}, lambda st: {"off": 0}, Counter()))
    scan.close()
    assert closed == [True]
    assert consume(source(lambda: [str(outside)]), tmp_path, {})[0][0]["event_id"] == "outside"


def test_nested_drain_keeps_separate_admitted_paths(tmp_path):
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    outer.write_bytes(b"outer\n")
    inner.write_bytes(b"inner\n")
    nested = []

    def collect(cfg, wm):
        nested.extend(consume(source(lambda: [str(inner)]), tmp_path, {})[0])
        yield from source(lambda: [str(outer)])(cfg, wm)

    rows, _ = consume(collect, tmp_path, {})
    assert [r["event_id"] for r in rows] == ["outer"]
    assert {r["event_id"] for r in nested} == {"inner"}


def test_source_failure_restores_direct_reader_scope(tmp_path):
    path = tmp_path / "a.jsonl"
    other = tmp_path / "other.jsonl"
    path.write_bytes(b"one\n")
    other.write_bytes(b"other\n")

    def collect(cfg, wm):
        yield from source(lambda: [str(path)])(cfg, wm)
        raise ValueError("source failed")

    with pytest.raises(ValueError, match="source failed"):
        consume(collect, tmp_path, {})
    assert list(_util.changed_files([str(other)], {}, lambda st: {"off": 0}, Counter()))


def test_failed_delivery_does_not_advance_the_snapshot(tmp_path):
    from agent_usage_tracker.scan import _CollectorPass, _blank_agent_tally
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\n")
    wm = {}

    def fail(rows):
        raise RuntimeError("delivery failed")

    run = _CollectorPass(_blank_agent_tally(), fail)
    with pytest.raises(RuntimeError, match="delivery failed"):
        run.drain_all(source(lambda: [str(path)]), config(tmp_path), wm, "test")
    assert wm == {"files": {}}


def test_delivery_failure_keeps_other_paths_at_their_acknowledged_offsets(tmp_path, monkeypatch):
    from agent_usage_tracker.scan import _CollectorPass, _blank_agent_tally

    monkeypatch.setattr(_util, "_MAX_LINES", 1)
    changed, other = tmp_path / "changed", tmp_path / "other"
    changed.write_bytes(b"initial-one\ninitial-two\n")
    other.write_bytes(b"first\nsecond\n")
    base = source(lambda: [str(changed), str(other)])
    shortened = False

    def collect(cfg, wm):
        nonlocal shortened
        yield from base(cfg, wm)
        if not shortened:
            shortened = True
            changed.write_bytes(b"new\n")

    deliveries = 0

    def deliver(rows):
        nonlocal deliveries
        deliveries += 1
        if deliveries == 2:
            raise RuntimeError("delivery failed")

    wm = {}
    run = _CollectorPass(_blank_agent_tally(), deliver)
    with pytest.raises(RuntimeError, match="delivery failed"):
        run.drain_all(collect, config(tmp_path), wm, "test")
    assert str(changed) not in wm["files"]
    assert wm["files"][str(other)]["off"] == len(b"first\n")
