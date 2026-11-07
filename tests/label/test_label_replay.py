"""Historical replay uses synthetic facts, separate progress and immutable generations."""

import json
import os
import errno
import sys
from types import SimpleNamespace
from contextlib import nullcontext
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest

from agent_usage_tracker import cli, state
from agent_usage_tracker.config import AutError
from agent_usage_tracker.label import local, replay, run, tasks
from label_helpers import FakeSource, _cfg, _claude_log, _steer, _win
from test_label_local import recipe

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SINCE, UNTIL = date(2026, 11, 1), date(2026, 11, 7)


def request(uid="u", text="A", day=3, task="steer_intent", unit="steer"):
    return tasks.TASKS[task].request(unit, uid, NOW.replace(day=day), {"text": text})


@pytest.fixture
def context(tmp_path, monkeypatch):
    cfg = replace(_cfg(tmp_path), repo_key_salt="s" * 32)
    state = {"requests": [request()], "sent": [], "loads": 0,
             "policy": {"tasks": {"steer_intent": recipe()}}, "policy_id": "a" * 64}
    monkeypatch.setattr(replay, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(replay, "load_policy", lambda: (state["policy"], state["policy_id"]))
    monkeypatch.setattr(replay, "units", lambda *args: state["requests"])
    monkeypatch.setattr(replay, "send_activity_rows", lambda dsn, events: state["sent"].extend(events))

    class Backend:
        def __init__(self, task, row, policy_id, cfg):
            state["loads"] += 1
            self.row, self.recipe, self.policy_id = row, row["recipe"], policy_id
            self.model_id, self.model_rev = "local/test", policy_id

        def prepare_requests(self, requests, deadline):
            pass

        def distribution(self, req):
            if state.get("on_score"):
                state["on_score"]()
            score = .4 if req.facts["text"] == "B" else .8
            raw = {c["id"]: (1 - score) / (len(req.candidates) - 1) for c in req.candidates}
            raw["instruction"] = score
            return raw

        def close(self):
            pass

    state["backend"] = Backend
    state["cfg"], state["root"] = cfg, tmp_path / "data/label/replay"
    return state


def prepare(ctx, now=NOW):
    return replay.prepare(ctx["cfg"], "steer_intent", SINCE, UNTIL, src=object(), now=now)


def apply(ctx, job, **kw):
    return replay.resume(ctx["cfg"], job, apply=True, backend_for=ctx["backend"], **kw)


def test_preview_filters_range_and_never_loads_models_or_sends(context):
    c = context
    c["requests"] = [request("early", day=1), request("late", day=7), request("middle", day=3)]
    p = prepare(c)
    assert p["total"] == 2
    assert p["remaining"] == 2
    assert not c["loads"] and not c["sent"]
    assert replay.resume(c["cfg"], p["job_id"])["remaining"] == 2
    if os.name != "nt":
        directory = c["root"] / p["job_id"]
        assert directory.stat().st_mode & 0o077 == 0
        assert (directory / "manifest.json").stat().st_mode & 0o077 == 0


def test_successful_resume_is_idempotent_and_keeps_unit_time(context):
    p = prepare(context)
    result = apply(context, p["job_id"])
    assert result["complete"] and result["remaining"] == 0
    assert context["sent"][0].ts == request().ts
    assert context["sent"][0].attrs["replay_job_id"] == p["job_id"]
    apply(context, p["job_id"])
    assert len(context["sent"]) == 1


def test_delivery_failure_does_not_advance_progress(context, monkeypatch):
    p = prepare(context)
    def fail(*args):
        raise RuntimeError("synthetic delivery failure")
    monkeypatch.setattr(replay, "send_activity_rows", fail)
    with pytest.raises(RuntimeError):
        apply(context, p["job_id"])
    progress = json.loads((context["root"] / p["job_id"] / "progress.json").read_text())
    assert progress["next"] == 0
    monkeypatch.setattr(replay, "send_activity_rows", lambda dsn, events: context["sent"].extend(events))
    assert apply(context, p["job_id"])["complete"]


def test_budget_resumes_after_acknowledged_batch(context, monkeypatch):
    context["requests"] = [request(f"u{i:02}") for i in range(25)]
    clock = [0]
    monkeypatch.setattr(replay.time, "monotonic", lambda: clock[0])
    context["on_score"] = lambda: clock.__setitem__(0, clock[0] + 1)
    p = prepare(context)
    first = apply(context, p["job_id"], budget_s=10)
    assert first["remaining"] == 15 and not first["complete"]
    second = apply(context, p["job_id"], budget_s=20)
    assert second["complete"] and len(context["sent"]) == 25
    assert len({e.event_id for e in context["sent"]}) == 25


def test_newer_unknown_and_a_to_b_to_a_are_distinct_generations(context):
    jobs = []
    for i, text in enumerate(["A", "B", "A"]):
        context["requests"] = [request(text=text)]
        jobs.append(prepare(context, NOW + timedelta(seconds=i))["job_id"])
        apply(context, jobs[-1])
    a, b, restored = context["sent"]
    assert [e.attrs["label"] for e in (a, b, restored)] == ["instruction", "unknown", "instruction"]
    assert a.attrs["input_hash"] == restored.attrs["input_hash"]
    assert len({e.event_id for e in context["sent"]}) == 3
    assert a.ts == b.ts == restored.ts
    assert max(context["sent"], key=lambda e: e.attrs["labelled_at"]) is restored


def test_delayed_older_job_does_not_beat_newer_result(context):
    old = prepare(context, NOW)["job_id"]
    context["requests"] = [request(text="B")]
    new = prepare(context, NOW + timedelta(seconds=1))["job_id"]
    apply(context, new)
    apply(context, old)
    latest = max(context["sent"], key=lambda e: e.attrs["labelled_at"])
    assert latest.attrs["label"] == "unknown" and latest.attrs["replay_job_id"] == new


@pytest.mark.parametrize("delta_us", [0, 500])
def test_equal_or_submillisecond_previews_have_strict_generation_order(context, delta_us):
    now = NOW.replace(microsecond=123000)
    old = prepare(context, now)["job_id"]
    context["requests"] = [request(text="B")]
    new = prepare(context, now + timedelta(microseconds=delta_us))["job_id"]
    apply(context, new)
    apply(context, old)
    newest, delayed = context["sent"]
    assert newest.attrs["labelled_at"] > delayed.attrs["labelled_at"]
    assert max(context["sent"], key=lambda e: (e.attrs["labelled_at"], e.event_id)) is newest
    assert newest.attrs["label"] == "unknown"


@pytest.mark.parametrize("raw", ["{bad", "{}", '{"last_ms":true}',
                                 '{"last_ms":-1}', '{"last_ms":"123"}',
                                 '{"last_ms":999999999999999999999999999999999}'])
def test_corrupt_generation_state_is_not_reset(context, raw):
    prepare(context)
    path = context["root"] / "generation.json"
    path.write_text(raw)
    with pytest.raises(AutError):
        prepare(context)
    assert path.read_text() == raw
    assert not context["loads"] and not context["sent"]


def test_missing_generation_state_is_not_reset_with_existing_jobs(context):
    prepare(context)
    (context["root"] / "generation.json").unlink()
    with pytest.raises(AutError, match="missing generation state"):
        prepare(context)


def test_concurrent_generation_allocation_is_unique(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        stamps = list(pool.map(lambda _: replay.reserve_label_time(tmp_path, NOW), range(12)))
    assert len(set(stamps)) == 12
    assert max(stamps) == (NOW + timedelta(milliseconds=11)).isoformat(timespec="milliseconds")


@pytest.fixture
def normal_run(context, monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(microsecond=123000)

    class Backend(context["backend"], local.LocalBackend):
        pass

    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(run, "datetime", Clock)
    monkeypatch.setattr(run, "data_dir", lambda: context["root"].parent.parent)
    monkeypatch.setattr(run, "load_policy", lambda: (context["policy"], context["policy_id"]))
    monkeypatch.setattr(run, "units", lambda *args: context["requests"])
    monkeypatch.setattr(run, "send_activity_rows", lambda dsn, rows: context["sent"].extend(rows))
    monkeypatch.setattr(run, "job_heartbeat", lambda *a, **kw: nullcontext(SimpleNamespace(events=0)))
    source = FakeSource(context["cfg"])

    def invoke(dry_run=False):
        return run.run_label(context["cfg"], ["steer_intent"], src=source,
                             backend_for=lambda *a: Backend("steer_intent", recipe(),
                                                            context["policy_id"], context["cfg"]),
                             local_estimates=True, dry_run=dry_run, now=NOW)
    return invoke


@pytest.mark.parametrize("older_path", ["normal", "replay"])
def test_normal_and_replay_share_fixed_clock_ordering(context, normal_run, older_path):
    fixed = NOW.replace(microsecond=123000)
    if older_path == "normal":
        normal_run()
        old = context["sent"][-1]
        normal_run()
        assert context["sent"][-1].event_id == old.event_id
        context["requests"] = [request(text="B")]
        new_job = prepare(context, fixed)["job_id"]
        apply(context, new_job)
        new = context["sent"][-1]
    else:
        old_job = prepare(context, fixed)["job_id"]
        context["requests"] = [request(text="B")]
        normal_run()
        new = context["sent"][-1]
        apply(context, old_job)
        old = context["sent"][-1]
    assert old.attrs["label"] == "instruction" and new.attrs["label"] == "unknown"
    assert new.attrs["labelled_at"] > old.attrs["labelled_at"]
    assert new.ts == old.ts == request().ts
    assert new.attrs["task"] == old.attrs["task"] == "working.local.steer_intent"
    assert new.attrs["policy_id"] == old.attrs["policy_id"] == context["policy_id"]
    assert max(context["sent"], key=lambda e: (e.attrs["labelled_at"], e.event_id)) is new


def test_normal_local_dry_run_does_not_allocate_generation(context, normal_run, monkeypatch):
    monkeypatch.setattr(run, "reserve_label_time", lambda *a: pytest.fail("dry run allocated"))
    normal_run(dry_run=True)
    assert not context["sent"] and not (context["root"] / "generation.json").exists()


def test_unrelated_policy_change_does_not_invalidate_resume(context):
    p = prepare(context)
    context["policy"]["tasks"]["work_category"] = {"changed": True}
    context["policy_id"] = "b" * 64
    assert apply(context, p["job_id"])["complete"]
    assert context["sent"][0].attrs["policy_id"] == "a" * 64


def test_current_task_change_and_manifest_tampering_fail_closed(context):
    p = prepare(context)
    context["policy"]["tasks"]["steer_intent"]["minimum_score"] = .8
    with pytest.raises(AutError):
        apply(context, p["job_id"])
    context["policy"]["tasks"]["steer_intent"]["minimum_score"] = .5
    path = context["root"] / p["job_id"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["requests"][0]["facts"]["text"] = "tampered"
    path.write_text(json.dumps(manifest))
    with pytest.raises(AutError):
        apply(context, p["job_id"])
    assert not context["loads"] and not context["sent"]


@pytest.mark.parametrize("job", ["../outside", "bad", "A" * 32])
def test_invalid_job_identifier_is_rejected(context, job):
    with pytest.raises(AutError):
        replay.resume(context["cfg"], job)


@pytest.mark.parametrize("budget", [0, -1, 3601])
def test_invalid_budgets_are_rejected(context, budget):
    p = prepare(context)
    with pytest.raises(AutError):
        apply(context, p["job_id"], budget_s=budget)


def test_invalid_or_future_range_is_rejected(context):
    with pytest.raises(AutError):
        replay.prepare(context["cfg"], "steer_intent", UNTIL, SINCE, now=NOW)
    with pytest.raises(AutError):
        replay.prepare(context["cfg"], "steer_intent", SINCE, date(2026,11,8), now=NOW)


def test_same_job_contention_rejects_before_scoring(context):
    p = prepare(context)
    with state._locked(context["root"] / p["job_id"] / "progress.json"):
        with pytest.raises(AutError, match="already running"):
            apply(context, p["job_id"])
    assert not context["loads"] and not context["sent"]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EAGAIN, errno.EDEADLK])
def test_windows_nonblocking_contention_is_normalized(tmp_path, monkeypatch, code):
    def locking(*args):
        raise OSError(code, "synthetic lock conflict")
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32")
        m.setitem(sys.modules, "msvcrt", SimpleNamespace(
            locking=locking, LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3))
        with pytest.raises(state.LockBusy):
            with state._locked(tmp_path / "progress.json", blocking=False):
                pytest.fail("lock acquired")


def test_windows_body_errors_are_not_lock_contention(tmp_path, monkeypatch):
    calls = []
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32")
        m.setitem(sys.modules, "msvcrt", SimpleNamespace(
            locking=lambda fd, mode, size: calls.append(mode), LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3))
        with pytest.raises(OSError) as error:
            with state._locked(tmp_path / "progress.json", blocking=False):
                raise OSError(errno.EACCES, "synthetic body error")
        assert not isinstance(error.value, state.LockBusy)
    assert calls == [2, 3]


def test_checkpoint_failure_retries_same_ids(context, monkeypatch):
    p = prepare(context)
    write = replay._write
    def fail(path, value):
        raise OSError("synthetic checkpoint failure")
    monkeypatch.setattr(replay, "_write", fail)
    with pytest.raises(OSError):
        apply(context, p["job_id"])
    first = context["sent"][0]
    monkeypatch.setattr(replay, "_write", write)
    assert apply(context, p["job_id"])["complete"]
    assert context["sent"][1] == first


def test_expired_budget_keeps_unfinished_batch_for_retry(context, monkeypatch):
    context["requests"] = [request("first"), request("second")]
    clock = [0]
    monkeypatch.setattr(replay.time, "monotonic", lambda: clock[0])
    context["on_score"] = lambda: clock.__setitem__(0, 10)
    p = prepare(context)
    result = apply(context, p["job_id"], budget_s=1)
    assert result["reason"] == "budget" and result["remaining"] == 2
    assert not context["sent"]
    context.pop("on_score")
    assert apply(context, p["job_id"])["complete"]


def test_delivery_blocking_error_is_not_reported_as_contention(context, monkeypatch):
    p = prepare(context)
    def fail(*args):
        raise BlockingIOError("synthetic delivery error")
    monkeypatch.setattr(replay, "send_activity_rows", fail)
    with pytest.raises(BlockingIOError, match="delivery error"):
        apply(context, p["job_id"])


@pytest.mark.parametrize("filename", ["manifest.json", "progress.json"])
def test_corrupt_json_fails_closed(context, filename):
    p = prepare(context)
    (context["root"] / p["job_id"] / filename).write_text("{bad json")
    with pytest.raises(AutError, match="missing or invalid"):
        apply(context, p["job_id"])
    assert not context["loads"] and not context["sent"]


def test_watermark_bytes_unchanged(context, tmp_path, monkeypatch):
    watermark = tmp_path / "state.json"
    original = b'{"collectors":{"label":{"since":"old"},"claude_code":{"offset":123}}}\n'
    watermark.write_bytes(original)
    monkeypatch.setenv("AUT_STATE", str(watermark))
    p = prepare(context)
    replay.resume(context["cfg"], p["job_id"])
    apply(context, p["job_id"])
    assert watermark.read_bytes() == original


@pytest.mark.parametrize("cursor", [-1, 2, True, "0", None])
def test_corrupt_cursor_fails_closed(context, cursor):
    p = prepare(context)
    path = context["root"] / p["job_id"] / "progress.json"
    value = json.loads(path.read_text())
    value["next"] = cursor
    path.write_text(json.dumps(value))
    with pytest.raises(AutError, match="cursor changed"):
        apply(context, p["job_id"])
    assert not context["loads"] and not context["sent"]


@pytest.mark.parametrize("change", [{"machine": "another"}, {"repo_key_salt": "x" * 32}])
def test_changed_machine_or_salt_fails_closed(context, change):
    p = prepare(context)
    context["cfg"] = replace(context["cfg"], **change)
    with pytest.raises(AutError, match="identity or repository salt changed"):
        apply(context, p["job_id"])
    assert not context["loads"] and not context["sent"]


def test_missing_and_corrupt_salt_fail_closed(context, tmp_path, monkeypatch):
    p = prepare(context)
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    context["cfg"] = replace(context["cfg"], repo_key_salt=None)
    for raw in (None, b"short", b"\xff" * 40):
        if raw is not None:
            (tmp_path / "repo_key.salt").write_bytes(raw)
        with pytest.raises(AutError, match="restore the existing repository salt"):
            apply(context, p["job_id"])
    assert not context["loads"] and not context["sent"]


@pytest.mark.parametrize("argv", [[], ["--apply"], ["--task", "steer_intent"],
    ["--job", "a" * 32, "--task", "steer_intent"],
    ["--job", "a" * 32, "--since", "2026-11-01"], ["--budget-s", "3601"]])
def test_cli_invalid_combinations_do_not_prepare_or_resume(context, monkeypatch, argv):
    monkeypatch.setattr(cli, "load_config", lambda: context["cfg"])
    monkeypatch.setattr(replay, "prepare", lambda *a, **kw: pytest.fail("prepared"))
    monkeypatch.setattr(replay, "resume", lambda *a, **kw: pytest.fail("resumed"))
    assert cli.main(["label", "replay", *argv]) == 1


def test_cli_preview_and_apply_dispatch(context, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: context["cfg"])
    calls = []
    monkeypatch.setattr(replay, "prepare", lambda *a: calls.append(a) or {"total": 1})
    monkeypatch.setattr(replay, "resume", lambda *a, **kw: calls.append((a, kw)) or {"complete": True})
    assert cli.main(["label", "replay", "--task", "steer_intent", "--since", "2026-11-01",
                     "--until", "2026-11-07"]) == 0
    assert calls[0] == (context["cfg"], "steer_intent", SINCE, UNTIL)
    assert cli.main(["label", "replay", "--job", "a" * 32, "--apply", "--budget-s", "30"]) == 0
    assert calls[1] == ((context["cfg"], "a" * 32), {"apply": True, "budget_s": 30})
    assert '"complete": true' in capsys.readouterr().out


def test_work_snapshot_includes_episode_day_and_aw_and_counts_missing(context, monkeypatch):
    cfg = context["cfg"]
    t = NOW.replace(day=3)
    _claude_log(cfg.home, "s", [("u", "synthetic planning text")])
    steers = [_steer("u", "s", t), _steer("part_missing", "s", t),
              _steer("missing", "gone", t),
              _steer("today", "today", NOW)]
    def episode(uid, sid):
        return {"id": uid, "first_ts": t, "last_ts": t, "members": [{"session_id": sid}]}
    src = FakeSource(cfg, steers, [episode("e", "s"), episode("lost", "gone")],
                     wins=[_win(t + timedelta(hours=1), 6), _win(NOW, 6)])
    monkeypatch.setattr(replay, "units", tasks.units)
    context["policy"]["tasks"]["work_category"] = recipe()
    p = replay.prepare(cfg, "work_category", SINCE, UNTIL, src=src, now=NOW)
    manifest = json.loads((context["root"] / p["job_id"] / "manifest.json").read_text())
    assert {r["unit"] for r in manifest["requests"]} == {"episode_day", "aw_block"}
    assert p["total"] == 2 and p["missing_local_text"] == 1
    assert p["partial_local_text"] == 1
    assert "synthetic planning text" not in json.dumps(p)


def test_steer_preview_counts_missing_text_only_inside_range(context, monkeypatch):
    cfg = context["cfg"]
    t = NOW.replace(day=3)
    _claude_log(cfg.home, "s", [("u", "synthetic instruction")])
    src = FakeSource(cfg, [_steer("u", "s", t), _steer("missing", "s", t),
                           _steer("today", "s", NOW),
                           _steer("unsupported", "c", t, agent="codex")])
    monkeypatch.setattr(replay, "units", tasks.units)
    p = replay.prepare(cfg, "steer_intent", SINCE, UNTIL, src=src, now=NOW)
    assert p["total"] == 1 and p["missing_local_text"] == 1
    assert p["partial_local_text"] == 0


def test_old_outcome_snapshot_uses_current_facts_and_counts_missing(context, monkeypatch):
    cfg = context["cfg"]
    old = NOW.replace(month=10, day=3)
    _claude_log(cfg.home, "s", [("recent", "synthetic current facts")])
    eps = [{"id": uid, "outcome": outcome, "first_ts": old, "last_ts": NOW,
            "members": [{"session_id": sid}]} for uid, sid, outcome in
           [("eligible", "s", "no_code"), ("missing", "gone", "no_code"),
            ("committed", "s", "shipped")]]
    src = FakeSource(cfg, [_steer("recent", "s", NOW - timedelta(hours=1))], eps)
    context["policy"]["tasks"]["soft_outcome"] = recipe()
    monkeypatch.setattr(replay, "units", tasks.units)
    p = replay.prepare(cfg, "soft_outcome", date(2026,10,1), date(2026,10,8), src=src, now=NOW)
    manifest = json.loads((context["root"] / p["job_id"] / "manifest.json").read_text())
    assert p["total"] == 1 and p["missing_local_text"] == 1
    assert manifest["requests"][0]["facts"]["last_steers"] == ["synthetic current facts"]
    assert manifest["requests"][0]["unit_id"] == "eligible"
