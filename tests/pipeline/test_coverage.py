"""Coverage receipts describe evidence without reading or changing conversations."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import coverage
from agent_usage_tracker.config import Config

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state" / "state.json"))
    return Config("test", ("codex",), None, None, None, tmp_path / "home")


def registry(cfg, **kwargs):
    path = cfg.home / ".config/agent-usage-tracker/sources.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sources": [], **kwargs}))


def native_file(cfg, data=b"not read as a transcript\n"):
    path = cfg.home / ".codex/sessions/2026/11/14/rollout-one.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def row(name="codex", **kwargs):
    return {"collector": name, "status": "ok", "error": None,
            "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(),
            "usage_events": 0, "human_classifications": 0, "activity": {},
            "diagnostics": {"skipped": {}, "clients": [], "client_overflow": False}, **kwargs}


def source(report, name):
    return next(s for s in report["sources"] if s["id"] == name)


def codes(report):
    return {i["code"] for i in report["issues"]}


def test_missing_is_not_idle_or_proof_of_no_work(cfg):
    report = coverage.build_report(cfg, now=NOW)
    assert source(report, "native:codex")["availability"] == "missing"
    assert "scan_receipt_missing" in codes(report)
    assert report["completeness"] == "unknown"
    assert not coverage.observations_path().exists()
    assert not coverage.report_path().exists()


def test_idle_receipt_does_not_claim_capability_or_complete_work(cfg):
    native_file(cfg)
    coverage.save_scan_observations([row()])
    report = coverage.build_report(cfg, now=NOW)
    assert report["collectors"][0]["observation"]["activity_state"] == "idle"
    assert report["collectors"][0]["capabilities"]["usage"]["observed"] is False
    assert source(report, "native:codex")["capabilities"]["usage"]["observed"] is None
    assert source(report, "native:codex")["capabilities"]["commands"]["support"] == "native_parser"
    assert "scan_receipt_missing" not in codes(report)


def test_scan_receipts_merge_per_collector_and_strip_content(cfg):
    coverage.save_scan_observations([row(usage_events=3, prompt="secret")])
    coverage.save_scan_observations([row("pi", activity={"turn": 2}, diagnostics={
        "skipped": {"malformed_records": 1}, "clients": ["codex_cli_rs"],
        "client_overflow": False, "prompt": "secret"})])
    data = json.loads(coverage.observations_path().read_text())
    assert set(data["collectors"]) == {"codex", "pi"}
    assert data["collectors"]["codex"]["usage_events"] == 3
    assert "secret" not in coverage.observations_path().read_text()
    if os.name != "nt":
        assert coverage.observations_path().stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("error,expected", [("ValueError", "ValueError"),
    ("PermissionError", "PermissionError"), ("private request text", "collector_error")])
def test_degraded_receipt_preserves_safe_error_class_without_messages(cfg, error, expected):
    coverage.save_scan_observations([row(status="degraded", error=error)])
    stored = json.loads(coverage.observations_path().read_text())["collectors"]["codex"]
    assert stored["status"] == "degraded"
    assert stored["error"] == expected
    report = coverage.build_report(cfg, now=NOW)
    assert "collector_failed" in codes(report)
    assert report["collectors"][0]["observation"]["activity_state"] == "unknown"


def test_custom_harness_relocated_engine_and_links_do_not_change_counts(cfg):
    root = cfg.home / "profiles/engine"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions/rollout-one.jsonl").write_text("not parsed\n")
    alias = cfg.home / "alias"
    alias.symlink_to(root, target_is_directory=True)
    sources = [{"id": "custom", "harness": "my-own-ade", "engine": "codex",
                "location": "local", "home": str(root)},
               {"id": "alias", "harness": "my-own-ade", "engine": "codex",
                "location": "local", "home": str(alias)},
               {"id": "remote", "harness": "my-own-ade", "engine": "codex",
                "location": "remote"},
               {"id": "unknown", "harness": "future", "engine": "future-engine",
                "location": "local"}]
    links = [{"source_id": "custom", "native_session_id": "n", "harness_thread_id": t}
             for t in ("a", "b")]
    registry(cfg, sources=sources, links=links)
    coverage.save_scan_observations([row(usage_events=7)])
    report = coverage.build_report(cfg, now=NOW)
    assert source(report, "custom")["availability"] == "available"
    assert source(report, "remote")["availability"] == "unknown"
    assert source(report, "unknown")["capabilities"]["usage"]["support"] == "unsupported"
    assert source(report, "custom")["session_associations"] == 2
    assert report["collectors"][0]["observation"]["usage_events"] == 7
    roots = report["collectors"][0]["roots"]
    assert len(roots) == 2
    assert "registered_source_unsupported" in codes(report)


def test_malformed_state_and_receipts_report_unknown_without_copying(cfg):
    path = Path(os.environ["AUT_STATE"])
    path.parent.mkdir(parents=True)
    path.write_text("broken JSON")
    coverage.observations_path().write_text("[]")
    registry(cfg, version=99)
    before = sorted(p.name for p in path.parent.iterdir())
    report = coverage.build_report(cfg, now=NOW)
    assert {"state_unreadable", "scan_receipts_unreadable", "registry_unreadable"} <= codes(report)
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_parser_loss_and_unread_byte_backlog_are_explicit(cfg):
    path = native_file(cfg)
    state = Path(os.environ["AUT_STATE"])
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"collectors": {"codex": {"files": {
        str(path): {"off": 2, "ino": path.stat().st_ino}}}}}))
    coverage.save_scan_observations([row(diagnostics={"skipped": {
        "malformed_records": 2, "activity_errors": 1}, "clients": [], "client_overflow": False})])
    report = coverage.build_report(cfg, now=NOW)
    assert {"parser_loss", "source_byte_backlog"} <= codes(report)
    assert report["collectors"][0]["roots"][0]["unread_bytes"] == path.stat().st_size - 2


def test_metadata_discovery_budget_is_explicit(cfg, monkeypatch):
    for i in range(5):
        native_file(cfg).with_name(f"rollout-{i}.jsonl").write_text("secret\n")
    monkeypatch.setattr(coverage, "MAX_ENTRIES", 2)
    report = coverage.build_report(cfg, now=NOW)
    assert "discovery_limited" in codes(report)
    assert report["read_limits"]["metadata_entries"] == 2


def test_large_first_root_cannot_hide_other_native_homes(cfg, monkeypatch):
    cfg.agents = ("claude_code", "codex", "pi")
    large = cfg.home / ".claude/projects/project"
    large.mkdir(parents=True)
    for index in range(30):
        (large / f"{index}.jsonl").write_text("not read\n")
    native_file(cfg)
    profile = cfg.home / "other-codex"
    (profile / "sessions").mkdir(parents=True)
    (profile / "sessions/rollout-profile.jsonl").write_text("not read\n")
    (cfg.home / ".pi/agent/sessions").mkdir(parents=True)
    registry(cfg, sources=[{"id": "profile", "harness": "wrapper", "engine": "codex",
                           "location": "local", "home": str(profile)}])
    monkeypatch.setattr(coverage, "MAX_ENTRIES", 16)
    report = coverage.build_report(cfg, now=NOW)
    by_engine = {c["collector"]: c for c in report["collectors"]}
    assert by_engine["claude_code"]["roots"][0]["limited"]
    assert all(r["status"] == "available" for r in by_engine["codex"]["roots"])
    assert source(report, "native:codex")["availability"] == "available"
    assert source(report, "profile")["availability"] == "available"
    assert source(report, "native:pi")["availability"] == "available"
    assert report["read_limits"]["metadata_entries_visited"] <= 16
    assert report["completeness"] == "unknown"


@pytest.mark.parametrize("future", [False, True])
def test_empty_native_home_is_available_without_inventing_complete_work(cfg, future):
    (cfg.home / ".codex/sessions").mkdir(parents=True)
    stamp = NOW + timedelta(hours=1) if future else NOW
    coverage.save_scan_observations([row(started_at=stamp.isoformat(),
                                        finished_at=stamp.isoformat())])
    report = coverage.build_report(cfg, now=NOW)
    assert source(report, "native:codex")["availability"] == "available"
    observation = report["collectors"][0]["observation"]
    assert observation["activity_state"] == ("unknown" if future else "idle")
    assert report["collectors"][0]["capabilities"]["requests"]["observed"] is (
        None if future else False)
    assert report["completeness"] == "unknown"


def test_each_root_stops_enumerating_after_its_share(cfg, monkeypatch):
    from contextlib import contextmanager

    root = cfg.home / ".codex/sessions"
    root.mkdir(parents=True)
    for index in range(30):
        (root / f"rollout-{index}.jsonl").write_text("not read\n")
    original = coverage.os.scandir
    seen = []

    @contextmanager
    def counted(path):
        with original(path) as entries:
            def bounded():
                for entry in entries:
                    seen.append(entry.name)
                    assert len(seen) <= 3, "root quota must also stop directory enumeration"
                    yield entry
            yield bounded()

    monkeypatch.setattr(coverage.os, "scandir", counted)
    budget = [0]
    info = coverage._metadata(root, {}, budget, "codex", 3)
    assert info["limited"] and info["files"] == 2
    assert budget[0] == 3


def test_outbox_report_is_readonly_and_separate(cfg):
    from agent_usage_tracker.push import outbox_path
    path = outbox_path()
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    try:
        with con:
            con.execute("CREATE TABLE pending(kind TEXT)")
            con.execute("INSERT INTO pending VALUES ('activity')")
    finally:
        con.close()
    path.chmod(0o640)
    before = (path.read_bytes(), path.stat().st_mode)
    report = coverage.build_report(cfg, now=NOW)
    assert report["delivery"] == {"status": "available", "pending": {"activity": 1}}
    assert "delivery_backlog" in codes(report)
    assert before == (path.read_bytes(), path.stat().st_mode)


@pytest.mark.parametrize("change,status", [({}, "fresh"),
    ({"published_at": "2026-11-13T21:00:00+00:00",
      "verified_at": "2026-11-13T22:00:00+00:00"}, "stale"),
    ({"verified_at": "2026-11-14T13:00:00+00:00"}, "unknown"),
    ({"published_at": "2026-11-14T13:00:00+00:00"}, "unknown"),
    ({"published_revision": "other"}, "stale"),
    ({"completed_through": "2026-11-12"}, "stale"),
    ({"completed_through": "2026-11-14"}, "unknown"),
    ({"verified_at": "2026-11-14T11:00:00"}, "unknown")])
def test_publication_receipts_validate_freshness_revision_and_completed_day(cfg, change, status):
    path = cfg.home / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"completed_through": "2026-11-13",
        "published_at": "2026-11-14T10:00:00+00:00",
        "verified_at": "2026-11-14T11:00:00+00:00",
        "published_revision": "one", "live_revision": "one", **change}))
    registry(cfg, publications=[{"id": "public", "path": str(path), "timezone": "Europe/Amsterdam"}])
    report = coverage.build_report(cfg, now=NOW)
    assert report["publications"][0]["status"] == status
    assert report["publications"][0]["required_completed_through"] == "2026-11-13"


def test_save_report_tracks_issue_signature_only(cfg):
    report = coverage.build_report(cfg, now=NOW)
    assert coverage.save_report(report)
    report["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert not coverage.save_report(report)
    report["issues"].append({"code": "new", "source_id": "x", "severity": "warning"})
    assert coverage.save_report(report)
    assert json.loads(coverage.report_path().read_text())["issues"][-1]["code"] == "new"


def test_doctor_does_not_create_state_salt_or_outbox(cfg):
    before = set(cfg.home.parent.rglob("*"))
    coverage.build_report(cfg, now=NOW)
    assert set(cfg.home.parent.rglob("*")) == before


def test_registered_nonfile_route_is_not_covered_by_engine_receipts(cfg):
    path = native_file(cfg)
    registry(cfg, sources=[{"id": "ui", "harness": "custom", "engine": "codex",
                           "location": "local", "home": str(path.parents[4]),
                           "transport": "acp"}])
    coverage.save_scan_observations([row(usage_events=9)])
    report = coverage.build_report(cfg, now=NOW)
    assert source(report, "ui")["availability"] == "unknown"
    assert source(report, "ui")["capabilities"]["usage"]["support"] == "unsupported"
    assert source(report, "ui")["capabilities"]["usage"]["observed"] is None


def test_discovery_ignores_unparsed_files_and_child_symlinks(cfg):
    path = native_file(cfg)
    path.with_name("arbitrary.jsonl").write_text("not a rollout\n")
    path.with_name("rollout-alias.jsonl").symlink_to(path)
    report = coverage.build_report(cfg, now=NOW)
    root = report["collectors"][0]["roots"][0]
    assert root["files"] == 1
    assert root["unread_bytes"] == path.stat().st_size


def test_unreadable_registered_source_is_explicit(cfg, monkeypatch):
    path = native_file(cfg)
    registry(cfg, sources=[{"id": "local", "harness": "custom", "engine": "codex",
                           "location": "local", "home": str(path.parents[4])}])
    original = coverage.os.scandir

    def denied(root):
        if Path(root) == cfg.home / ".codex/sessions":
            raise PermissionError("AUT_PRIVATE_ERROR_SENTINEL_794")
        return original(root)

    monkeypatch.setattr(coverage.os, "scandir", denied)
    report = coverage.build_report(cfg, now=NOW)
    assert source(report, "local")["availability"] == "unreadable"
    assert "source_unreadable" in codes(report)
    assert "AUT_PRIVATE_ERROR_SENTINEL_794" not in json.dumps(report)


@pytest.mark.parametrize("bad", [{"version": 99, "collectors": {}},
                                 {"version": 1, "collectors": {"codex": {
                                     "collector": "codex", "finished_at": NOW.isoformat(),
                                     "status": "ok", "usage_events": "invalid"}}}])
def test_bad_receipt_never_becomes_a_fresh_idle_run(cfg, bad):
    path = coverage.observations_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bad))
    report = coverage.build_report(cfg, now=NOW)
    assert report["collectors"][0]["observation"]["activity_state"] == "unknown"


def test_stale_and_future_scan_receipts_are_unknown_work(cfg):
    for stamp in ("2026-11-13T01:00:00+00:00", "2026-11-15T01:00:00+00:00"):
        coverage.save_scan_observations([row(started_at=stamp, finished_at=stamp)])
        report = coverage.build_report(cfg, now=NOW)
        assert report["collectors"][0]["observation"]["activity_state"] == "unknown"


def test_invalid_saved_counts_do_not_become_idle_evidence(cfg):
    coverage.save_scan_observations([row(usage_events="invalid")])
    report = coverage.build_report(cfg, now=NOW)
    assert report["collectors"][0]["observation"]["activity_state"] == "unknown"
    assert report["collectors"][0]["capabilities"]["usage"]["observed"] is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
def test_reading_fifo_is_rejected_without_waiting(cfg, tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    assert coverage._read_json(path)[0] == "invalid"


def test_json_reads_are_bounded(cfg, tmp_path):
    path = tmp_path / "large"
    path.write_bytes(b"x" * 100)
    assert coverage._read_json(path, 16) == ("limited", {})


def test_publication_uses_registered_timezone_at_day_boundary(cfg):
    path = cfg.home / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"completed_through": "2026-11-14",
        "published_at": "2026-11-14T11:00:00+00:00",
        "verified_at": "2026-11-14T11:30:00+00:00",
        "published_revision": "a", "live_revision": "a"}))
    registry(cfg, publications=[{"id": "public", "path": str(path), "timezone": "Pacific/Auckland"}])
    assert coverage.build_report(cfg, now=NOW)["publications"][0]["status"] == "fresh"


def test_catalog_is_research_not_parser_coverage(cfg):
    report = coverage.build_report(cfg, now=NOW)
    assert len(report["catalog"]) >= 17
    assert all(c["runtime_verified"] is False for c in report["catalog"])
    assert all(c["parser_coverage"] == "not_qualified_by_catalog" for c in report["catalog"])


@pytest.mark.parametrize("human_count,activity_state,requests_observed", [
    (0, "unknown", None), (1, "observed", True)])
def test_missing_payload_receipt_keeps_source_gap_visible(
        cfg, human_count, activity_state, requests_observed):
    cfg.agents = ("cursor",)
    coverage.save_scan_observations([row("cursor", human_classifications=human_count, diagnostics={
        "skipped": {"missing_payloads": 2}, "clients": [], "client_overflow": False})])
    report = coverage.build_report(cfg, now=NOW)
    collector = next(c for c in report["collectors"] if c["collector"] == "cursor")
    assert collector["observation"]["status"] == "ok"
    assert collector["observation"]["diagnostics"]["skipped"] == {"missing_payloads": 2}
    assert collector["observation"]["activity_state"] == activity_state
    assert collector["capabilities"]["requests"]["observed"] is requests_observed
    assert {"code": "source_payload_missing", "source_id": "collector:cursor",
            "severity": "warning"} in report["issues"]
    assert "parser_loss" not in codes(report)
    assert report["completeness"] == "unknown"
