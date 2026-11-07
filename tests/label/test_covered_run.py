"""Exercise the real runner/source/journal seam with synthetic local model responses."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from agent_usage_tracker.label import covered_run, local, run, tasks
from agent_usage_tracker.label.backends import BackendUnavailable
from label_helpers import NOW, FakeSource, _cfg, _env, _steer
from test_label import sink  # noqa: F401
from test_label_local import recipe


@pytest.fixture
def scenario(tmp_path, monkeypatch, sink):  # noqa: F811
    cfg = _cfg(tmp_path, label_mature_outcomes=True, label_budget_s=10)
    episodes, steers = [], []
    for uid, age, reply in [("old", 40, True), ("recent", 1, True), ("missing", 70, False)]:
        at = NOW-timedelta(days=age)
        directory = tmp_path / ".claude/projects/p"
        directory.mkdir(parents=True, exist_ok=True)
        records = [{"type": "user", "uuid": uid, "sessionId": uid,
                    "timestamp": at.isoformat(), "message": {"content": f"question {uid}"}}]
        if reply:
            records.append({"type": "assistant", "uuid": uid+"-reply", "sessionId": uid,
                            "timestamp": (at+timedelta(minutes=1)).isoformat(),
                            "message": {"content": "answer complete"}})
        (directory/f"{uid}.jsonl").write_text("\n".join(map(json.dumps, records)))
        steers.append(_steer(uid, uid, at))
        episodes.append({"id": uid, "first_ts": at, "last_ts": at+timedelta(minutes=1),
                         "outcome": "no_code", "members": [{"session_id": uid}]})
    src = FakeSource(cfg, steers, episodes, local_context=True)
    state = {"clock": 0, "step": 0, "score": .8, "policy": "b"*64,
             "on_score": lambda req: None, "scored": []}
    monkeypatch.setattr(covered_run.time, "monotonic", lambda: state["clock"])
    monkeypatch.setattr(run, "load_policy", lambda: ({}, state["policy"]))

    class Model(local.LocalBackend):
        def __init__(self):
            super().__init__("soft_outcome", recipe(), state["policy"], cfg)

        def prepare_requests(self, requests, deadline):
            pass

        def distribution(self, req):
            state["clock"] += state["step"]
            state["scored"].append(req.unit_id)
            state["on_score"](req)
            raw = {c["id"]: (1-state["score"])/3 for c in req.candidates}
            raw["resolved"] = state["score"]
            return raw

    def execute(now=NOW, **kw):
        state["clock"] = 0
        return run.run_label(cfg, ["soft_outcome"], src=src, now=now,
            local_estimates=True, backend_for=lambda task, choice: Model(), **kw)["soft_outcome"]

    return cfg, src, state, execute, sink


def progress():
    return covered_run.data_dir()/"label/outcome-coverage.json"


def test_default_off_and_mature_source_only_when_local(tmp_path):
    cfg = _cfg(tmp_path)
    assert not cfg.label_mature_outcomes
    assert not tasks.Source(cfg, local_context=True).mature_outcomes
    enabled = replace(cfg, label_mature_outcomes=True)
    assert not tasks.Source(enabled).mature_outcomes
    assert tasks.Source(enabled, local_context=True).mature_outcomes


def test_config_loads_explicit_mature_opt_in_without_changing_default(tmp_path):
    from agent_usage_tracker.config import load_config
    with _env(tmp_path):
        assert load_config().label_mature_outcomes is False
        (tmp_path/"config.toml").write_text("label_mature_outcomes = true\n")
        assert load_config().label_mature_outcomes is True


def test_real_source_selects_old_work_journal_skips_age_only_reruns(scenario, tmp_path):
    cfg, src, state, execute, captured = scenario
    with _env(tmp_path):
        report = execute()
        assert report["rows"] == 2 and report["skipped_incomplete_outcome"] == 1
        assert state["scored"] == ["recent", "old"]
        saved = json.loads(progress().read_text())
        assert len(saved["acknowledged"]) == 2
        assert "question" not in progress().read_text()
        assert progress().stat().st_mode & 0o077 == 0
        # Age changes alone do not re-run mature work; recent work is reassessed.
        state["scored"].clear()
        execute(now=NOW+timedelta(days=1))
        assert state["scored"] == ["recent"]


def test_failed_delivery_restarts_without_acknowledging_failed_batch(scenario, tmp_path, monkeypatch):
    cfg, src, state, execute, captured = scenario
    calls = []

    def fail(dsn, rows):
        calls.append(rows)
        raise OSError("synthetic delivery failure")

    with _env(tmp_path):
        monkeypatch.setattr(run, "send_activity_rows", fail)
        with pytest.raises(OSError):
            execute()
        assert not progress().exists()
        monkeypatch.setattr(run, "send_activity_rows", captured.activity)
        assert execute()["rows"] == 2
        assert [ev.event_id for ev in calls[0]] == [ev.event_id for ev in captured.rows[0]]


def test_one_request_budget_resumes_mature_phase_after_restart(scenario, tmp_path, monkeypatch):
    cfg, src, state, execute, captured = scenario
    state["step"] = 11
    with _env(tmp_path):
        first = execute()
        assert first["rows"] == 1 and first["cut"] == 1
        assert json.loads(progress().read_text())["next_mature"] is True
        state["scored"].clear()
        # A fresh recent request appears the next day, but the saved phase starts mature.
        second = execute(now=NOW+timedelta(days=1))
        assert second["rows"] == 1 and state["scored"] == ["old"]


def test_unknown_is_retained_and_acknowledged(scenario, tmp_path):
    cfg, src, state, execute, captured = scenario
    state["score"] = .4
    with _env(tmp_path):
        assert execute()["below_tau"] == 2
        assert {ev.attrs["label"] for batch in captured.rows for ev in batch} == {"unknown"}
        state["scored"].clear()
        assert execute()["rows"] == 0 and state["scored"] == []


def test_changed_revision_context_or_policy_reclassifies(scenario, tmp_path):
    cfg, src, state, execute, captured = scenario
    with _env(tmp_path):
        execute()
        old = src._e[0]
        old["last_ts"] += timedelta(minutes=1)
        state["scored"].clear()
        execute()
        assert state["scored"] == ["old"]
        logfile = tmp_path / ".claude/projects/p/old.jsonl"
        logfile.write_text(logfile.read_text().replace("answer complete", "changed answer"))
        state["scored"].clear()
        execute()
        assert state["scored"] == ["old"]
        state["policy"] = "c"*64
        state["scored"].clear()
        execute()
        assert set(state["scored"]) == {"recent", "old"}


def test_reopened_episode_is_rechecked_before_delivery(scenario, tmp_path):
    cfg, src, state, execute, captured = scenario

    def reopen(req):
        if req.unit_id == "old":
            src._e[0]["outcome"] = "shipped"

    state["on_score"] = reopen
    with _env(tmp_path):
        result = execute()
        assert result["rows"] == 1 and result["skipped_stale_outcome"] == 1
        assert [ev.attrs["unit_id"] for batch in captured.rows for ev in batch] == ["recent"]


def test_dry_run_does_not_create_coverage_progress(scenario, tmp_path):
    cfg, src, state, execute, captured = scenario
    with _env(tmp_path):
        assert execute(dry_run=True)["rows"] == 2
        assert not progress().exists() and captured.rows == []
        assert not progress().with_suffix(".lock").exists()


def test_later_model_budget_failure_preserves_completed_pending_rows(scenario, tmp_path):
    cfg, src, state, execute, captured = scenario

    def exhaust(req):
        if req.unit_id == "old":
            raise BackendUnavailable("synthetic model budget")

    state["on_score"] = exhaust
    with _env(tmp_path):
        assert execute()["failed"] == 1
        assert len(json.loads(progress().read_text())["acknowledged"]) == 1
        assert [ev.attrs["unit_id"] for batch in captured.rows for ev in batch] == ["recent"]
        state["on_score"] = lambda req: None
        state["scored"].clear()
        assert execute()["rows"] == 1 and state["scored"] == ["old"]


def test_checkpoint_failure_after_delivery_retries_same_events(scenario, tmp_path, monkeypatch):
    cfg, src, state, execute, captured = scenario
    write = covered_run.atomic_write_text

    def fail(*args, **kw):
        raise OSError("synthetic checkpoint failure")

    with _env(tmp_path):
        monkeypatch.setattr(covered_run, "atomic_write_text", fail)
        with pytest.raises(OSError):
            execute()
        assert not progress().exists() and len(captured.rows) == 1
        monkeypatch.setattr(covered_run, "atomic_write_text", write)
        assert execute()["rows"] == 2
        assert [ev.event_id for ev in captured.rows[0]] == [ev.event_id for ev in captured.rows[1]]


def test_invalid_progress_is_not_silently_reset(scenario, tmp_path):
    from agent_usage_tracker.config import AutError
    cfg, src, state, execute, captured = scenario
    with _env(tmp_path):
        progress().parent.mkdir(parents=True)
        progress().write_text('{"version":999}')
        with pytest.raises(AutError, match="not reset"):
            execute()
        assert progress().read_text() == '{"version":999}' and captured.rows == []
