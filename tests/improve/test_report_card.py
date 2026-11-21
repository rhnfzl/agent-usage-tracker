"""Report fixtures never read production stores or call a model or sink."""

import json
import os
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from agent_usage_tracker import cli
from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import judge, qualify, report_card as card
from agent_usage_tracker.improve.store import Store, digest

from .test_qualify import RUBRIC, case as label_case, judgment

TODAY = date(2026, 11, 21)
SECRET = '<script>alert("PRIVATE_SENTINEL")</script>|[link](javascript:bad)'


@pytest.fixture(autouse=True)
def local_paths(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('ledger_timezone = "Asia/Tokyo"\n[transport]\npg_dsn = 123\n')
    monkeypatch.setenv("AUT_CONFIG", str(config))
    monkeypatch.setattr(card.coverage, "report_path", lambda: tmp_path / "coverage.json")


def put_case(store, stamp, name, trigger="control", verdict="fail", version=1, **extra):
    events = [{"id": name, "timestamp": stamp, "text": SECRET, "kind": "prompt"}]
    session = store.put("sessions", {"agent": "codex", "session_id": name, "events": events})
    key = store.put("cases", {"agent": "codex", "trigger": trigger, "events": events,
                               "session_snapshot": session})
    store.put("judgments", {"case_id": key, "rubric": "test_rubric", "rubric_version": version,
        "job_id": digest(name), "verdict": verdict, "review": {"rationale": SECRET}, **extra})
    return key


def snapshot(store):
    return {p.relative_to(store.root).as_posix(): p.read_bytes() for p in store.root.rglob("*")
            if p.is_file()}


def test_empty_store_read_only_build_and_render(tmp_path):
    store = Store(tmp_path / "store")
    before = snapshot(store)
    result = card.build(Store(store.root, read_only=True), today=TODAY)
    assert all(n == 0 for n in result["records"].values())
    assert result["cycle"]["status"] == "missing"
    assert "0 of 2" in card.to_markdown(result)
    assert "too few cases" in card.to_html(result)
    assert not result["coverage"]["audit_available"]
    assert result["coverage"]["open_issues"] is None
    assert snapshot(store) == before


def test_two_windows_versions_triggers_harnesses_and_ledger_zone(tmp_path):
    store = Store(tmp_path / "store")
    put_case(store, "2026-11-19T23:30:00Z", "current", verdict="pass")
    put_case(store, "2026-11-17T23:30:00Z", "previous")
    put_case(store, "2026-11-20T02:00:00Z", "error", trigger="error_result")
    put_case(store, "2026-11-20T02:00:00Z", "merge", trigger="merge", version=2)
    put_case(store, "2026-11-20T03:00:00Z", "unknown", reason="incomplete_evidence")
    put_case(store, "2026-11-22T00:00:00Z", "future")
    rows = [r for r in card.build(store, days=2, today=TODAY)["rubrics"]
            if r["rubric_id"] == "test_rubric"]
    assert len(rows) == 3
    control = next(r for r in rows if r["trigger"] == "control")
    assert control["current"] == {"passed": 1, "failed": 0, "unknown": 1}
    assert control["previous"] == {"passed": 0, "failed": 1, "unknown": 0}
    assert next(r for r in rows if r["trigger"] == "merge")["rubric_version"] == 2
    text = card.to_markdown(card.build(store, days=2, today=TODAY))
    assert "-100.0 percentage points" in text


@pytest.mark.parametrize("before,after,enough", [(12, 12, True), (12, 5, False), (10, 10, True)])
def test_adoption_minimum_known_cases_and_versions(tmp_path, before, after, enough):
    store = Store(tmp_path / "store")
    for side, count, stamp in [("before", before, "2026-11-02T12:00:00Z"),
                               ("after", after, "2026-11-15T12:00:00Z")]:
        for i in range(count):
            put_case(store, stamp, f"{side}{i}", verdict="fail" if i < 3 else "pass")
    for i in range(20):
        put_case(store, "2026-11-15T12:00:00Z", f"unknown{i}", verdict="unknown")
        put_case(store, "2026-11-15T12:00:00Z", f"error{i}", trigger="error_result")
    put_case(store, "2026-11-10T12:00:00Z", "adoption-day")
    store.put("adoptions", {"title": SECRET, "adopted_at": "2026-11-10",
                            "rubric_ids": ["test_rubric"]})
    result = card.build(store, today=TODAY)
    row = result["adoptions"][0]
    assert row["before"] == {"passed": before - 3, "failed": 3, "unknown": 0}
    assert row["after"] == {"passed": after - 3, "failed": 3, "unknown": 20}
    assert row["tasks_after"] is None
    section = next(rows for title, _, rows in card._sections(result) if title.startswith("Adopted"))
    assert ("too few cases" not in section[0][3]) == enough
    assert "not a causal estimate" in section[0][-1]


def test_commands_group_status_age_escaping_and_json_privacy(tmp_path):
    store = Store(tmp_path / "private-sentinel-path")
    group = digest("group")
    proposal = store.put("proposals", {"title": SECRET, "owner_component": SECRET,
                                      "group_key": group, "case_id": digest("case")})
    store.append("proposal_prepared", proposal_id=proposal, at="2026-11-19T23:30:00Z")
    store.append("proposal_staged", proposal_id=proposal, candidate="/private/sentinel")
    report = store.put("group_reports", {"window_days": 14, "day": "2026-11-21", "groups": [{
        "group_key": group, "signals": {"cause": SECRET}, "recurring": True,
        "distinct_tasks": 3, "projects": {SECRET: 3}, "audit_incidents": 9}]})
    store.append("groups", report_id=report)
    before = snapshot(store)
    result = card.build(store, today=TODAY)
    assert result["groups"][0]["status"] == "proposed"
    assert result["proposals"][0]["age_days"] == 1
    assert result["proposals"][0]["staged"]
    markdown, page, public = card.to_markdown(result), card.to_html(result), card.to_json(result)
    assert "inbox accept" in page and "inbox reject" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<script>" not in markdown and r"\[link\]" in markdown
    assert SECRET not in public and "PRIVATE_SENTINEL" not in public
    assert "private-sentinel" not in public and "javascript" not in public
    assert snapshot(store) == before


def test_fresh_qualification_after_queued_grading_and_write_footprint(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_rubrics", lambda: [RUBRIC])
    store = Store(tmp_path / "store")
    key = label_case(store, "queued")
    qualify.save(store, RUBRIC, key, "pass")
    qualify.run(store, RUBRIC.id)
    assert judge.queued(store) == [key]
    judgment(store, key)
    before = snapshot(store)
    result = json.loads(card.write(store, format="json"))
    assert result["qualifications"][0]["counts"]["supported_passes"] == 1
    assert judge.queued(store) == []
    after = snapshot(store)
    for path, data in before.items():
        if path != "events.jsonl" and path != "judge_queue.json":
            assert after[path] == data
    added = set(after) - set(before)
    assert all(p.startswith("qualifications/") or p in {
        "report-card.html", "report-card.md"} or p.endswith(".lock") for p in added)
    assert "report-card.html" in after and "report-card.md" in after
    assert not any(p.endswith(".json") and p.startswith("report-card") for p in after)
    assert all(json.loads(line)["kind"] == "qualification" for line in
               after["events.jsonl"][len(before["events.jsonl"]):].splitlines())


def test_original_job_bound_judgments_remain_historical_in_report(tmp_path):
    store = Store(tmp_path / "store")
    events = [{"id": "p", "kind": "prompt", "text": "Check the example",
               "timestamp": "2026-11-21T10:00:00Z"},
              {"id": "a", "kind": "reply", "text": "Checked the example"}]
    key = label_case(store, "original", agent="codex", events=events)
    policy = {"version": 1, "cases": [key], "audit_id": "a", "models": ["fixture"],
              "rubrics": {RUBRIC.id: ["Historic question", "Historic definition"]},
              "explain_model": None, "explain_revision": None, "minimum_raw_score": .7}
    job = store.put("diagnoses", policy)
    identity = store.put("judgments", {"case_id": key, "rubric": RUBRIC.id,
        "job_id": job, "verdict": "pass",
        "evaluation_key": digest({"job": job, "case": key, "rubric": RUBRIC.id})})
    original = store.get("judgments", identity)
    result = json.loads(card.write(store, format="json"))
    assert any(r["rubric_id"] == RUBRIC.id and r["rubric_version"] == 0
               for r in result["rubrics"])
    assert not any(r["qualified"] for r in result["qualifications"])
    assert store.get("judgments", identity) == original


@pytest.mark.parametrize("format", ["md", "html", "json"])
def test_cli_formats_always_write_private_files(tmp_path, capsys, format):
    root = tmp_path / format
    cli.main(["improve", "--root", str(root), "report-card", "--format", format])
    out = capsys.readouterr().out
    assert (root / "report-card.md").is_file() and (root / "report-card.html").is_file()
    if format == "json":
        assert json.loads(out)["days"] == 28
    else:
        assert out.strip() == (root / f"report-card.{format}").read_text().strip()
    if os.name != "nt":
        assert (root / "report-card.html").stat().st_mode & 0o077 == 0
        assert (root / "report-card.md").stat().st_mode & 0o077 == 0


def test_invalid_days_has_no_qualification_effects(tmp_path):
    store = Store(tmp_path / "store")
    before = snapshot(store)
    with pytest.raises(AutError, match="days"):
        card.write(store, days=0)
    assert snapshot(store) == before


def test_coverage_cost_and_stopped_cycle_use_actual_records(tmp_path):
    store = Store(tmp_path / "store")
    audit = store.put("audits", {"coverage": {"complete_sessions": 2, "unread_sources": 7,
        "unreadable_sources": 3, "truncated_events": 4, "automated_sources": 5}})
    store.append("audit", audit_id=audit)
    (tmp_path / "coverage.json").write_text(json.dumps({"issues": [{"title": SECRET}],
        "collectors": [{"collector": "pi", "observation": {"usage_events": 1}},
                       {"collector": "cursor", "observation": {"usage_events": 0}}]}))
    for kind, values in [("model_call", {"load_s": 2, "infer_s": None}),
                         ("model_call", {"infer_s": 3, "started_at": "2026-11-21T01:00:00Z"}),
                         ("diagnosis", {"job_id": "same", "calls": 7}),
                         ("diagnosis", {"job_id": "same", "calls": 0}),
                         ("label", {}), ("decision", {})]:
        store.append(kind, at="2026-11-21T01:00:00Z", **values)
    store.append("model_call", at="2026-10-01T01:00:00Z", infer_s=999)
    proposal = store.put("proposals", {"drafter": {"cost_usd": 0.25}, "title": SECRET})
    store.append("proposal_prepared", proposal_id=proposal, at="2026-11-21T01:00:00Z")
    (store.root / "last_cycle.json").write_text(json.dumps({"status": "timeout", "error": SECRET,
        "progress": {"diagnosis": {"finished": 3, "total": 5, "optional_finished": 1,
                                   "optional_total": 2, "calls": 4, "rationale": SECRET}}}))
    result = card.build(store, days=2, today=TODAY)
    assert result["coverage"]["audit_counts"]["unread_sources"] == 7
    assert result["coverage"]["usage_without_evidence"] == ["pi"]
    assert result["coverage"]["open_issues"] == 1
    assert result["cost"]["model_calls"] == 1 and result["cost"]["load_records"] == 1
    assert result["cost"]["diagnosis_reported_calls"] == 7
    assert result["cost"]["labels"] == result["cost"]["decisions"] == 1
    assert result["private"]["model_seconds"] == 5
    assert result["private"]["drafter_cost_usd"] == 0.25
    assert result["cycle"]["finished"] == 3 and result["cycle"]["status"] == "timeout"
    assert "PRIVATE_SENTINEL" not in card.to_json(result)


def test_group_task_counts_use_group_members_and_deduplicate_sessions(tmp_path):
    store = Store(tmp_path / "store")
    before = put_case(store, "2026-11-05T12:00:00Z", "before")
    after = put_case(store, "2026-11-15T12:00:00Z", "after")
    duplicate = store.put("cases", {**store.get("cases", after), "copy": True})
    put_case(store, "2026-11-15T12:00:00Z", "not-in-group")
    group = digest("group")
    store.put("group_reports", {"groups": [{"group_key": group, "case_ids": [before]}]})
    store.put("group_reports", {"groups": [{"group_key": group, "case_ids": [after, duplicate]}]})
    store.put("adoptions", {"title": "Adopted", "adopted_at": "2026-11-10", "group_key": group,
                            "rubric_ids": ["test_rubric"]})
    row = card.build(store, today=TODAY)["adoptions"][0]
    assert row["tasks_before"] == 1 and row["tasks_after"] == 1
    assert card._tasks(store, {}, TODAY, TODAY, TODAY, ZoneInfo("UTC")) is None


def test_json_refuses_text_in_numeric_progress(tmp_path):
    store = Store(tmp_path / "store")
    (store.root / "last_cycle.json").write_text(json.dumps({"diagnosis": {"calls": SECRET}}))
    with pytest.raises(AutError, match="non-count"):
        card.to_json(card.build(store, today=TODAY))


def test_incomplete_share_uses_current_version_and_window(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_rubrics", lambda: [RUBRIC])
    store = Store(tmp_path / "store")
    for name, stamp, incomplete in [("current", "2026-11-20T01:00:00Z", True),
                                    ("known", "2026-11-20T02:00:00Z", False),
                                    ("old", "2026-10-01T01:00:00Z", True)]:
        key = label_case(store, name, agent="codex", events=[
            {"id": name, "kind": "prompt", "text": SECRET, "timestamp": stamp}])
        jid = judgment(store, key)
        if incomplete:
            row = store.get("judgments", jid)
            newer = store.put("judgments", {**row, "reason": "incomplete_evidence"})
            store.append("diagnosis", judgment_ids=[newer])
    result = card.build(store, days=2, today=TODAY)
    assert result["coverage"]["judgments"] == 2
    assert result["coverage"]["incomplete_evidence"] == 1
    assert "50.0% (1/2)" in card.to_html(result)


def test_cli_existing_store_leaves_support_files_untouched(tmp_path, capsys):
    store = Store(tmp_path / "store")
    support = [store.root / ".gitignore", store.root / ".aut-improve"]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in support]
    cli.main(["improve", "--root", str(store.root), "report-card", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["days"] == 28
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in support] == before
