"""Coverage receipts expose real collection gaps without changing row ownership."""

import json

import pytest

from agent_usage_tracker import cli, scan
from agent_usage_tracker.collectors import Collector
from test_cli import _recording_sender

pytest_plugins = ["test_cli"]


def test_doctor_json_is_readonly_and_keeps_transport_secrets_private(world, capsys):
    before = {str(p): p.read_bytes() for p in world.rglob("*") if p.is_file()}
    assert cli.main(["doctor", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["version"] == 1
    assert "postgresql://x" not in output
    assert "capabilities" in str(report)
    assert before == {str(p): p.read_bytes() for p in world.rglob("*") if p.is_file()}


def test_scan_persists_parser_losses_beside_normalized_counts(world, monkeypatch):
    from agent_usage_tracker import coverage

    sent = _recording_sender(monkeypatch)
    log = world / ".claude/projects/-p/s1.jsonl"
    with log.open("a") as stream:
        stream.write("{malformed}\n")
    assert cli.main(["--agents", "claude_code", "scan"]) == 0
    assert len(sent) == 1
    observed = json.loads(coverage.observations_path().read_text())["collectors"]["claude_code"]
    assert observed["usage_events"] == 1
    assert observed["diagnostics"]["skipped"]["malformed_records"] == 1
    report = json.loads(coverage.report_path().read_text())
    assert any(issue["source_id"] == "collector:claude_code" for issue in report["issues"])


def test_scan_dry_run_does_not_create_coverage_receipts(world):
    from agent_usage_tracker import coverage

    assert cli.main(["scan", "--dry-run"]) == 0
    assert not coverage.observations_path().exists()
    assert not coverage.report_path().exists()


def test_collector_failure_still_records_coverage_and_preserves_exception(world, monkeypatch):
    from agent_usage_tracker import coverage

    _recording_sender(monkeypatch)

    def broken(_cfg, _wm):
        raise ValueError("fixture failure")
        yield  # pragma: no cover

    monkeypatch.setitem(scan.REGISTRY, "claude_code", Collector(broken))
    with pytest.raises(ValueError, match="fixture failure"):
        cli.main(["--agents", "claude_code", "scan"])
    observed = json.loads(coverage.observations_path().read_text())["collectors"]["claude_code"]
    assert observed["status"] == "error"
    assert observed["error"] == "ValueError"


def test_commit_link_does_not_certify_task_outcome(world):
    from datetime import datetime, timezone
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    stamp = datetime.now(timezone.utc).isoformat()
    coverage.save_scan_observations([{
        "collector": "claude_code", "status": "ok", "error": None,
        "started_at": stamp, "finished_at": stamp, "usage_events": 0,
        "human_classifications": 0, "activity": {"commit": 1}, "diagnostics": {},
    }])
    report = coverage.build_report(load_config())
    collector = next(row for row in report["collectors"] if row["collector"] == "claude_code")
    assert collector["capabilities"]["outcomes"]["observed"] is None



def test_uninspected_root_is_not_reported_available_when_budget_is_exhausted(world, monkeypatch):
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    monkeypatch.setattr(coverage, "MAX_ENTRIES", 1)
    report = coverage.build_report(load_config())
    codex = next(row for row in report["collectors"] if row["collector"] == "codex")
    assert codex["roots"][0]["limited"]
    assert codex["roots"][0]["status"] == "unknown"
    source = next(row for row in report["sources"] if row["id"] == "native:codex")
    assert source["availability"] == "unknown"


def test_doctor_exposes_declared_links_without_inventing_request_counts(world):
    from agent_usage_tracker import coverage, sources
    from agent_usage_tracker.config import load_config

    cfg = load_config()
    link = {"source_id": "newapp", "native_session_id": "engine-one",
            "harness_thread_id": "thread-one", "request_id": "request-one"}
    sources.registry_path(cfg.home).write_text(json.dumps({
        "version": 1, "sources": [{"id": "newapp", "harness": "arbitrary-harness",
        "engine": "codex", "location": "local", "home": str(world / "engine")}],
        "links": [link],
    }))
    report = coverage.build_report(cfg)
    assert report["session_links"][0]["native_session_id"] == "engine-one"
    assert report["session_links"][0]["association_scope"] == "declared_not_duplicate_or_human_proof"
    assert next(c for c in report["collectors"] if c["collector"] == "codex")[
        "observation"]["activity_state"] == "unknown"


def test_status_points_to_the_coverage_report(world, capsys):
    assert cli.main(["status"]) == 0
    assert "aut doctor --json" in capsys.readouterr().out



def test_nested_native_subagent_backlog_is_not_silently_omitted(world):
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    child = world / ".claude/projects/-p/parent/subagents/agent-child.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text("{}\n")
    report = coverage.build_report(load_config())
    claude = next(row for row in report["collectors"] if row["collector"] == "claude_code")
    assert claude["roots"][0]["files"] == 2
    assert claude["roots"][0]["unread_bytes"] >= 3


@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_invalid_native_root_type_is_unreadable(world, kind):
    import os
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    path = world / ".codex/sessions"
    path.parent.mkdir(parents=True)
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(path)
    else:
        path.write_text("{}")
    report = coverage.build_report(load_config())
    source = next(row for row in report["sources"] if row["id"] == "native:codex")
    assert source["availability"] == "unreadable"


def test_delayed_older_scan_writer_cannot_replace_newer_counts(world):
    from datetime import datetime, timedelta, timezone
    from agent_usage_tracker import coverage

    now = datetime.now(timezone.utc)
    def row(stamp, count):
        return {"collector": "codex", "status": "ok", "error": None,
                "started_at": stamp.isoformat(), "finished_at": stamp.isoformat(),
                "usage_events": count, "human_classifications": 0,
                "activity": {}, "diagnostics": {}}
    coverage.save_scan_observations([row(now, 2)])
    coverage.save_scan_observations([row(now - timedelta(minutes=1), 0)])
    stored = json.loads(coverage.observations_path().read_text())
    assert stored["collectors"]["codex"]["usage_events"] == 2


def test_delayed_older_report_cannot_clear_newer_warning(world):
    from datetime import datetime, timedelta, timezone
    from agent_usage_tracker import coverage

    now = datetime.now(timezone.utc)
    newer = {"version": 1, "generated_at": now.isoformat(), "issues": [
        {"code": "parser_loss", "source_id": "collector:codex", "severity": "warning"}]}
    coverage.save_report(newer)
    before = coverage.report_path().read_bytes()
    older = {"version": 1, "generated_at": (now - timedelta(minutes=1)).isoformat(),
             "issues": []}
    assert coverage.save_report(older) is None
    assert coverage.report_path().read_bytes() == before


def assessed(monkeypatch, *, at, issues, during=None):
    """Run the scan's coverage step with a fixed assessment; `during` runs inside its build."""
    from agent_usage_tracker import coverage

    def build(cfg):
        if during:
            during()
        return {"version": 1, "generated_at": at.isoformat(), "issues": issues}

    monkeypatch.setattr(coverage, "build_report", build)
    monkeypatch.setattr(coverage, "save_scan_observations", lambda rows: None)
    from agent_usage_tracker.config import load_config
    return scan._record_coverage(load_config(), [])


def test_an_older_overlapping_scan_publishes_no_coverage_row(world, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agent_usage_tracker import coverage

    now = datetime.now(timezone.utc)
    assert assessed(monkeypatch, at=now - timedelta(minutes=2), issues=[])["status"] == "ok"
    newer = assessed(monkeypatch, at=now, issues=[MISSING])
    assert newer["collector"] == "coverage" and newer["status"] == "degraded"
    assert newer["finished_at"] == now
    saved = coverage.report_path().read_bytes()
    assert assessed(monkeypatch, at=now - timedelta(minutes=1), issues=[]) is None
    assert coverage.report_path().read_bytes() == saved
    assert list(json.loads(saved)["alerts"]) == ["source_payload_missing:collector:cursor"]


def test_alert_state_merges_with_a_report_saved_during_the_build(world, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agent_usage_tracker import coverage

    now = datetime.now(timezone.utc)
    coverage.report_path().parent.mkdir(parents=True, exist_ok=True)
    coverage.report_path().write_text(json.dumps(       # written before alerts existed
        {"version": 1, "generated_at": (now - timedelta(hours=2)).isoformat(), "issues": []}))
    # Another scan saves the baseline while this one builds, so this scan's issue is new.
    row = assessed(monkeypatch, at=now, issues=[MISSING], during=lambda: assessed(
        monkeypatch, at=now - timedelta(hours=1), issues=[]))
    assert row["status"] == "degraded" and row["error"] == "new: cursor payloads missing"


def test_a_damaged_previous_report_does_not_hide_a_new_issue(world, monkeypatch):
    from datetime import datetime, timezone
    from agent_usage_tracker import coverage

    coverage.report_path().parent.mkdir(parents=True, exist_ok=True)
    coverage.report_path().write_text("{not json")
    row = assessed(monkeypatch, at=datetime.now(timezone.utc), issues=[MISSING])
    assert row["status"] == "degraded"


def test_heartbeat_goes_out_when_the_coverage_step_raises(world, monkeypatch):
    from agent_usage_tracker import push

    delivered = []
    monkeypatch.setattr(scan, "make_sender", lambda cfg: [].extend)
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: delivered.extend(rows))

    def broken(cfg, observations):
        raise RuntimeError("fixture coverage failure")

    monkeypatch.setattr(scan, "_record_coverage", broken)
    with pytest.raises(RuntimeError, match="fixture coverage failure"):
        cli.main(["--agents", "claude_code", "scan"])
    assert [row["collector"] for row in delivered] == ["claude_code"]



def test_doctor_exposes_bundle_candidates_without_claiming_active_work(world, monkeypatch):
    from agent_usage_tracker import coverage, harness_inventory
    from agent_usage_tracker.config import load_config

    monkeypatch.setattr(harness_inventory.sys, "platform", "darwin")
    applications = world / "Applications"
    (applications / "Rex Beta.app").mkdir(parents=True)
    (applications / "Unrelated private name.app").mkdir()
    monkeypatch.setattr(harness_inventory, "application_dirs", lambda _home: [
        applications])
    monkeypatch.setattr(harness_inventory, "command_dirs", lambda _home: [])
    report = coverage.build_report(load_config())
    observed = report["harness_inventory"]["observations"]
    assert observed[0]["harness_id"] == "rex"
    assert observed[0]["installation"] == "candidate"
    assert observed[0]["activity"] == "unknown"
    assert observed[0]["reader_qualified"] is False
    assert "Unrelated private name" not in json.dumps(report)



def test_doctor_never_creates_sidecars_for_closed_wal_database(world):
    import sqlite3
    from agent_usage_tracker import coverage, push

    path = push.outbox_path()
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE pending(kind TEXT)")
    connection.execute("INSERT INTO pending VALUES ('activity')")
    connection.commit()
    connection.close()
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert set(before) == {"pending.sqlite3"}
    assert coverage._delivery()["status"] == "unknown"
    assert before == {p.name: p.read_bytes() for p in path.parent.iterdir()}



def test_doctor_does_not_consume_a_nonregular_delivery_source(world):
    import os
    from agent_usage_tracker import coverage, push

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    path = push.outbox_path()
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(descriptor, b"retained-stream")
        assert coverage._delivery()["status"] == "unknown"
        try:
            remaining = os.read(descriptor, 100)
        except BlockingIOError:
            remaining = b""
        assert remaining == b"retained-stream"
    finally:
        os.close(descriptor)



def test_all_rejected_records_cannot_appear_as_a_quiet_source(world):
    from datetime import datetime, timezone
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    stamp = datetime.now(timezone.utc).isoformat()
    coverage.save_scan_observations([{
        "collector": "claude_code", "status": "ok", "error": None,
        "started_at": stamp, "finished_at": stamp, "usage_events": 0,
        "human_classifications": 0, "activity": {},
        "diagnostics": {"skipped": {"malformed_records": 2}},
    }])
    report = coverage.build_report(load_config())
    collector = next(row for row in report["collectors"] if row["collector"] == "claude_code")
    assert collector["observation"]["activity_state"] == "unknown"
    assert collector["capabilities"]["usage"]["observed"] is None
    assert any(issue["code"] == "parser_loss" for issue in report["issues"])



def test_large_valid_watermark_has_a_separate_read_budget(world):
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    state = {"collectors": {}, "retained_metadata": "x" * (8 * 1024 * 1024)}
    (world / "state.json").write_text(json.dumps(state))
    report = coverage.build_report(load_config())
    assert not any(issue["code"] == "state_unreadable" for issue in report["issues"])
    assert report["state_status"] == "available"
    assert report["read_limits"]["state_json_bytes"] == 64 * 1024 * 1024


def test_watermark_read_budget_is_reported_as_limited_not_corrupt(world, monkeypatch):
    from agent_usage_tracker import coverage
    from agent_usage_tracker.config import load_config

    monkeypatch.setattr(coverage, "MAX_STATE_JSON_BYTES", 32, raising=False)
    report = coverage.build_report(load_config())
    codes = {issue["code"] for issue in report["issues"]}
    assert "state_limited" in codes
    assert "state_unreadable" not in codes
    assert report["state_status"] == "limited"



def test_delivery_identity_uses_consistent_metadata_calls(world, monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from agent_usage_tracker import coverage, push

    path = push.outbox_path()
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE pending(kind TEXT)")
    connection.execute("INSERT INTO pending VALUES ('activity')")
    connection.commit()
    connection.close()
    original = coverage.os.fstat

    def descriptor_metadata(descriptor):
        metadata = original(descriptor)
        return SimpleNamespace(st_mode=metadata.st_mode, st_size=metadata.st_size,
            st_ino=0, st_dev=metadata.st_dev, st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns)

    monkeypatch.setattr(coverage.os, "fstat", descriptor_metadata)
    assert coverage._delivery() == {"status": "available", "pending": {"activity": 1}}


@pytest.fixture
def inventory_home(world, monkeypatch):
    from agent_usage_tracker import harness_inventory
    from agent_usage_tracker.config import load_config

    monkeypatch.setattr(harness_inventory.sys, "platform", "darwin")
    applications = world / "Applications"
    applications.mkdir(exist_ok=True)
    monkeypatch.setattr(harness_inventory, "application_dirs", lambda _home: [applications])
    monkeypatch.setattr(harness_inventory, "command_dirs", lambda _home: [])
    return load_config()


def harness_issues(cfg, agents=None):
    from dataclasses import replace

    from agent_usage_tracker import coverage

    report = coverage.build_report(replace(cfg, agents=agents) if agents else cfg)
    return {(i["code"], i["source_id"]): i["severity"] for i in report["issues"]
            if i["code"].startswith("harness_") and i["code"] != "harness_inventory_limited"}


def test_reader_qualified_t3_code_raises_no_harness_issue(inventory_home, world):
    from dataclasses import replace

    (world / "Applications" / "T3 Code.app").mkdir()
    cutover = replace(inventory_home, t3_activity_since="2026-11-01T00:00:00+00:00")
    issues = harness_issues(cutover, agents=("t3",))
    assert not [key for key in issues if key[1] == "t3-code"]
    assert ("harness_route_unverified", "t3-code") in harness_issues(cutover, agents=("codex",))


@pytest.mark.parametrize("cutover, issue", [
    ("2026-11-01T00:00:00+00:00", None), (None, "harness_active_unread")])
def test_recent_t3_use_is_read_only_with_its_cutover(inventory_home, cutover, issue):
    from dataclasses import replace

    db = inventory_home.home / ".t3/userdata/state.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("")
    issues = harness_issues(replace(inventory_home, t3_activity_since=cutover), agents=("t3",))
    assert {code for code, source in issues if source == "t3-code"} == ({issue} - {None})


def test_recent_unread_harness_is_a_warning_and_a_recent_wrapper_is_info(inventory_home):
    for path in (".hermes/sessions/new.json", "Library/Application Support/rex/servers/s.db"):
        target = inventory_home.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    issues = harness_issues(inventory_home, agents=("claude_code", "codex"))
    assert issues[("harness_active_unread", "hermes")] == "warning"
    assert issues[("harness_unattributed", "rex")] == "info"


def scan_with_issues(monkeypatch, extra):
    """Run a real scan with extra coverage issues and return the delivered coverage row."""
    from agent_usage_tracker import coverage, push

    delivered = []
    monkeypatch.setattr(scan, "make_sender", lambda cfg: [].extend)
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: delivered.extend(rows))
    real = coverage.build_report

    def with_issues(cfg):
        report = real(cfg)
        report["issues"] = report["issues"] + extra
        return report

    monkeypatch.setattr(coverage, "build_report", with_issues)
    assert cli.main(["--agents", "claude_code", "scan"]) == 0
    return [row for row in delivered if row["collector"] == "coverage"]


MISSING = {"code": "source_payload_missing", "source_id": "collector:cursor", "severity": "warning"}


def test_coverage_row_seeds_a_baseline_then_alerts_on_a_new_issue(world, monkeypatch):
    from agent_usage_tracker import coverage

    [first] = scan_with_issues(monkeypatch, [])
    assert first["status"] == "ok" and first["error"] is None and first["events"] == 0
    assert "alerts" in json.loads(coverage.report_path().read_text())
    [second] = scan_with_issues(monkeypatch, [MISSING])
    assert second["status"] == "degraded"
    assert second["error"] == "new: cursor payloads missing"
    assert str(world) not in second["error"]


def test_info_and_non_actionable_issues_never_alert(world, monkeypatch):
    scan_with_issues(monkeypatch, [])
    [row] = scan_with_issues(monkeypatch, [
        {**MISSING, "severity": "info"},
        {"code": "collector_failed", "source_id": "collector:codex", "severity": "warning"}])
    assert row["status"] == "ok"


def test_scan_dry_run_sends_no_coverage_row(world, monkeypatch):
    from agent_usage_tracker import push

    monkeypatch.setattr(push, "send_scan_rows", lambda *a: pytest.fail("a dry run sent a row"))
    assert cli.main(["scan", "--dry-run"]) == 0


def test_alert_state_ages_out_and_forgets_after_a_week():
    from datetime import datetime, timedelta, timezone
    from agent_usage_tracker.coverage import alert_state

    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    report = {"issues": [MISSING]}
    alerts, text = alert_state(report, {}, now)            # baseline: known at once
    assert text is None
    alerts, text = alert_state({"issues": []}, {"alerts": {}}, now)
    alerts, text = alert_state(report, {"alerts": alerts}, now)
    assert text == "new: cursor payloads missing"
    later = now + timedelta(hours=25)
    alerts, text = alert_state(report, {"alerts": alerts}, later)
    assert text is None                                      # known after 24 h
    gone, text = alert_state({"issues": []}, {"alerts": alerts}, later)
    assert text is None and gone                             # absent: kept for a week
    assert alert_state(report, {"alerts": gone}, later + timedelta(days=3))[1] is None
    # A scan gap alone never re-alerts a present issue; a week of absence forgets it.
    assert alert_state(report, {"alerts": alerts}, later + timedelta(days=10))[1] is None
    forgotten, _ = alert_state({"issues": []}, {"alerts": gone}, later + timedelta(days=7))
    assert forgotten == {}
    assert alert_state(report, {"alerts": forgotten}, later + timedelta(days=8))[1] == \
        "new: cursor payloads missing"
