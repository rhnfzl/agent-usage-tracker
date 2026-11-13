from dataclasses import replace
import pytest
from agent_usage_tracker.config import Config, DEFAULT_AGENTS, AutError
from agent_usage_tracker.collectors import generic_sdk
from agent_usage_tracker.sdk import emit_usage


def cfg(tmp_path):
    return Config(
        "test",
        ("generic_sdk",),
        None,
        None,
        None,
        tmp_path,
        generic_sdk_paths=str(tmp_path / "*.jsonl"),
    )


def record(**changes):
    r = dict(
        version=1,
        kind="usage",
        id="event1",
        timestamp="2026-11-13T10:00:00Z",
        input_tokens=20,
        input_tokens_total=35,
        cache_read_tokens=10,
        cache_write_tokens=5,
        output_tokens=12,
        reasoning_tokens=4,
    )
    r.update(changes)
    return r


def test_retry_copy_rewrite_and_metadata(tmp_path):
    path = tmp_path / "events.jsonl"
    emit_usage(path, record(), producer="app", model="model1", provider="provider1")
    wm = {}
    events, ack = next(generic_sdk.collect(cfg(tmp_path), wm))
    assert not wm
    retry, _ = next(generic_sdk.collect(cfg(tmp_path), wm))
    assert retry == events
    assert events[0].total_tokens == 47 and events[0].reasoning_tokens == 4
    assert events[0].model == "model1" and events[0].provider == "provider1"
    (tmp_path / "copy.jsonl").write_bytes(path.read_bytes())
    ack()
    assert all(not rows for rows, _ in generic_sdk.collect(cfg(tmp_path), wm))
    path.write_text("")
    emit_usage(path, record(id="event2"), producer="app")
    rows, _ = next(generic_sdk.collect(cfg(tmp_path), wm))
    assert len(rows) == 1 and rows[0].model is None and rows[0].provider is None


def test_partial_malformed_and_conflict(tmp_path):
    path = tmp_path / "events.jsonl"
    emit_usage(path, record(), producer="app")
    original = path.read_text()
    path.write_text(original + "{")
    wm = {}
    rows, ack = next(generic_sdk.collect(cfg(tmp_path), wm))
    assert len(rows) == 1
    ack()
    path.write_text(original + "{}\n")
    with pytest.raises(AutError, match="malformed"):
        list(generic_sdk.collect(cfg(tmp_path), wm))
    path.write_text("")
    emit_usage(path, record(output_tokens=13), producer="app")
    with pytest.raises(AutError, match="conflict"):
        list(generic_sdk.collect(cfg(tmp_path), wm))


def test_unknown_and_counter_not_rows(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    emit_usage(path, record(cache_write_tokens=None), producer="app")
    emit_usage(
        path, record(id="counter", counter="series", temporality="cumulative"), producer="app"
    )
    assert next(generic_sdk.collect(cfg(tmp_path), {}))[0] == []
    warning = capsys.readouterr().err
    assert "unsupported=1" in warning and "unavailable=1" in warning


def test_overlap_is_explicit_and_default_disabled(tmp_path):
    assert "generic_sdk" not in DEFAULT_AGENTS
    path = tmp_path / "events.jsonl"
    emit_usage(path, record(), producer="app", native_source="codex")
    with pytest.raises(AutError, match="overlap"):
        list(generic_sdk.collect(replace(cfg(tmp_path), agents=("codex", "generic_sdk")), {}))
    assert len(next(generic_sdk.collect(cfg(tmp_path), {}))[0]) == 1


def test_missing_invalid_source_and_emitter(tmp_path):
    with pytest.raises(AutError, match="paths"):
        list(generic_sdk.collect(replace(cfg(tmp_path), generic_sdk_paths=None), {}))
    with pytest.raises(AutError, match="matched"):
        list(generic_sdk.collect(cfg(tmp_path), {}))
    with pytest.raises(ValueError):
        emit_usage(tmp_path / "events.jsonl", record(input_tokens=-1), producer="app")
    assert not (tmp_path / "events.jsonl").exists()


def test_config_and_registry(tmp_path):
    from agent_usage_tracker.config import _config_from, ALL_AGENTS
    from agent_usage_tracker.collectors import REGISTRY

    default = _config_from({}, tmp_path / "config.toml")
    assert default.agents == DEFAULT_AGENTS and default.generic_sdk_paths is None
    explicit = _config_from(
        {"agents": ["generic_sdk"], "generic_sdk_paths": "/local/*.jsonl"}, tmp_path / "config.toml"
    )
    assert explicit.agents == ("generic_sdk",) and explicit.generic_sdk_paths == "/local/*.jsonl"
    assert set(REGISTRY) == set(ALL_AGENTS)
    with pytest.raises(TypeError):
        _config_from({"generic_sdk_paths": []}, tmp_path / "config.toml")


def test_invalid_envelope_bounds_and_partial_append(tmp_path, monkeypatch):
    from agent_usage_tracker import sdk

    path = tmp_path / "events.jsonl"
    path.write_text("{")
    with pytest.raises(ValueError, match="incomplete"):
        emit_usage(path, record(), producer="app")
    assert path.read_text() == "{"
    path.write_text("")
    monkeypatch.setattr(sdk, "MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="limit"):
        emit_usage(path, record(), producer="app")
    assert path.read_text() == ""
    with pytest.raises(ValueError):
        sdk.validate_envelope({"version": 1, "producer": "app", "usage": record(), "prompt": "no"})


def test_changed_copies_rejected_before_ack(tmp_path):
    emit_usage(tmp_path / "a.jsonl", record(), producer="app")
    emit_usage(tmp_path / "b.jsonl", record(output_tokens=13), producer="app")
    wm = {}
    with pytest.raises(AutError, match="conflict"):
        list(generic_sdk.collect(cfg(tmp_path), wm))
    assert wm == {}


def backlog(tmp_path, count=10001):
    import json
    from agent_usage_tracker.sdk import validate_envelope

    path = tmp_path / "events.jsonl"
    with path.open("w") as stream:
        for number in range(count):
            value = validate_envelope(
                dict(version=1, producer="app", usage=record(id=f"e{number}"))
            )
            stream.write(json.dumps(value) + "\n")
        stream.write("{")
    return path


def test_backlog_later_batch_retry_and_export(tmp_path):
    import io
    from agent_usage_tracker.scan import run_export

    backlog(tmp_path)
    wm = {}
    batches = generic_sdk.collect(cfg(tmp_path), wm)
    first, ack = next(batches)
    assert len(first) == 10000
    ack()
    second, _ = next(batches)
    assert len(second) == 1 and len(wm["records"]) == 10000
    retry, ack = next(generic_sdk.collect(cfg(tmp_path), wm))
    assert retry == second
    ack()
    assert len(wm["records"]) == 10001
    assert next(generic_sdk.collect(cfg(tmp_path), wm))[0] == []
    output = io.StringIO()
    assert run_export(cfg(tmp_path), ("generic_sdk",), output) == (10001, 0, set())
    assert len(output.getvalue().splitlines()) == 10002


def test_effective_selection_overlap_in_all_scan_paths(tmp_path):
    import io
    from agent_usage_tracker import scan

    emit_usage(tmp_path / "events.jsonl", record(), producer="app", native_source="codex")
    selected = ("generic_sdk", "codex")
    with pytest.raises(AutError, match="overlap"):
        scan.run_export(cfg(tmp_path), selected, io.StringIO())
    with pytest.raises(AutError, match="overlap"):
        scan.run_scan(cfg(tmp_path), selected, dry_run=True)
    with pytest.raises(AutError, match="overlap"):
        scan.run_history_backfill(cfg(tmp_path), selected, dry_run=True)


def test_backlog_dryrun_drains_without_persisting(tmp_path):
    from agent_usage_tracker import scan
    from agent_usage_tracker.state import load_state

    backlog(tmp_path)
    report = scan.run_scan(cfg(tmp_path), ("generic_sdk",), dry_run=True)
    assert report["generic_sdk"]["events"] == 10001
    assert "generic_sdk" not in load_state().get("collectors", {})


def test_scan_sender_failure_between_batches_retries_only_pending(tmp_path):
    from agent_usage_tracker.scan import _CollectorPass, _blank_agent_tally

    backlog(tmp_path)
    wm = {}
    sent = []

    def fail_second(rows):
        if sent:
            raise OSError("delivery failed")
        sent.append([row.event_id for row in rows])

    runner = _CollectorPass(_blank_agent_tally(), fail_second)
    with pytest.raises(OSError, match="delivery failed"):
        runner.drain_all(generic_sdk.collect, cfg(tmp_path), wm, "generic_sdk")
    assert len(wm["records"]) == 10000 and len(sent[0]) == 10000
    retried = []
    retry = _CollectorPass(_blank_agent_tally(), lambda rows: retried.extend(rows))
    retry.drain_all(generic_sdk.collect, cfg(tmp_path), wm, "generic_sdk")
    assert len(retried) == 1 and retried[0].event_id not in sent[0]
    assert len(wm["records"]) == 10001


def test_late_backlog_conflict_delivers_nothing(tmp_path):
    path = backlog(tmp_path)
    # Remove the pending partial tail so the writer can append a complete changed copy.
    path.write_bytes(path.read_bytes()[:-1])
    emit_usage(path, record(id="e10000", output_tokens=13), producer="app")
    wm = {}
    with pytest.raises(AutError, match="conflict"):
        next(generic_sdk.collect(cfg(tmp_path), wm))
    assert wm == {}


def test_inclusive_input_mismatch_refused_before_delivery_or_ack(tmp_path):
    import json
    path = tmp_path / 'events.jsonl'
    bad = record(input_tokens=10, input_tokens_total=100, cache_read_tokens=0,
                 cache_write_tokens=0, output_tokens=5)
    with pytest.raises(ValueError):
        emit_usage(path, bad, producer='app')
    assert not path.exists()
    path.write_text(json.dumps(dict(version=1, producer='app', usage=bad)) + '\n')
    watermark = {}
    with pytest.raises(AutError):
        next(generic_sdk.collect(cfg(tmp_path), watermark))
    assert watermark == {}
