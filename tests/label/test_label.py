"""Hermetic tests for the `aut label` run and its wiring. No model and no mlx: every backend here
is a fake that returns fixed letter logits.

Locks the rules:
  - a run sends rows in batches, advances its watermark only when not a dry run, writes one
    heartbeat row (collector `label`), stops at the time budget, and never re-asks a cached title;
  - the nightly note's prompt is built from counts only;
  - aut-scan.sh runs `aut label run` once a day, only where AUT_LABEL=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import cli, push
from agent_usage_tracker.collectors import activitywatch as aw
from agent_usage_tracker.config import LABEL_DAYS, AutError
from agent_usage_tracker.label import backends, core, run, tasks
from agent_usage_tracker.label import bakeoff, gold, teach
from label_helpers import ById, DAY1, FakeSource, NOW, TASK, _cfg, _claude_log, _env, _steer, _win

REPO = Path(__file__).resolve().parents[2]


class Sink:
    def __init__(self):
        self.rows, self.scans, self.notes = [], [], []

    def activity(self, dsn, rows):
        self.rows.append(list(rows))

    def scan(self, dsn, rows):
        self.scans += rows


@pytest.fixture
def sink(monkeypatch):
    s = Sink()
    monkeypatch.setattr(run, "send_activity_rows", s.activity)
    monkeypatch.setattr(push, "send_scan_rows", s.scan)
    monkeypatch.setattr(run, "_insert_note", lambda dsn, day, text, m: s.notes.append(
        (day, text, m)))
    monkeypatch.setattr(run, "BATCH", 2)
    return s


def test_run_batches_watermark_dry_run_and_heartbeat(tmp_path, sink):
    recs = [(f"u{i}", f"step {i}") for i in range(5)]
    _claude_log(tmp_path, "s1", recs)
    steers = [_steer(u, "s1", DAY1 + timedelta(minutes=i)) for i, (u, _t) in enumerate(recs)]
    cfg = _cfg(tmp_path)
    b = ById("instruction")
    with _env(tmp_path):
        rep = run.run_label(cfg, ["steer_intent"], dry_run=True, src=FakeSource(cfg, steers),
                            backend_for=lambda t, c: b, now=NOW)
        assert rep["steer_intent"]["rows"] == 5 and sink.rows == [] and sink.scans == []
        assert not (tmp_path / "state.json").exists()

        rep = run.run_label(cfg, ["steer_intent"], src=FakeSource(cfg, steers),
                            backend_for=lambda t, c: b, now=NOW)
        assert [len(x) for x in sink.rows] == [2, 2, 1]
        assert rep["steer_intent"]["labels"]["instruction"] == 5
        assert rep["steer_intent"]["below_tau"] == 5          # uncalibrated: nothing passes
        sent = [e for batch in sink.rows for e in batch]
        assert all(e.machine == "test" and e.kind == "label" for e in sent)
        wm = json.loads((tmp_path / "state.json").read_text())["collectors"]["label"]
        assert wm["steer_intent"] == steers[-1]["ts"].isoformat()
        assert sink.scans[-1]["collector"] == "label" and sink.scans[-1]["events"] == 5
        assert sink.scans[-1]["status"] == "ok"

        # the next night: only the boundary unit again, with the same id (a no-op at the sink)
        sink.rows.clear()
        run.run_label(cfg, ["steer_intent"], src=FakeSource(cfg, steers),
                      backend_for=lambda t, c: b, now=NOW)
        again = [e for batch in sink.rows for e in batch]
        assert [e.event_id for e in again] == [sent[-1].event_id]


def test_time_budget_and_error_heartbeat(tmp_path, sink):
    _claude_log(tmp_path, "s1", [("u1", "go")])
    cfg = _cfg(tmp_path, label_budget_s=0)
    src = FakeSource(cfg, [_steer("u1", "s1", DAY1)])
    with _env(tmp_path):
        rep = run.run_label(cfg, ["steer_intent"], src=src, backend_for=lambda t, c: ById(),
                            now=NOW)
        assert rep["steer_intent"]["rows"] == 0 and rep["steer_intent"]["cut"] == 1

        class Boom(ById):
            def letter_logits(self, prompt, letters):
                raise RuntimeError("model died")
        with pytest.raises(RuntimeError):
            run.run_label(_cfg(tmp_path), ["steer_intent"], src=src,
                          backend_for=lambda t, c: Boom(), now=NOW)
        assert sink.scans[-1]["status"] == "error" and sink.scans[-1]["error"] == "RuntimeError"
    with pytest.raises(AutError):
        run.run_label(_cfg(tmp_path, pg_dsn=None), ["steer_intent"])


def test_per_task_entry_that_is_not_a_table_warns(tmp_path, capsys):
    with _env(tmp_path):
        core.choice_path().parent.mkdir(parents=True, exist_ok=True)
        core.choice_path().write_text(json.dumps({"tasks": {"steer_intent": 5}}))
        assert core.load_choice("steer_intent") == core.Choice()
        assert "steer_intent" in capsys.readouterr().err
        assert core.load_choice("work_category") == core.Choice()     # no entry yet: quiet
        assert capsys.readouterr().err == ""


def test_ctrl_c_sends_no_heartbeat_but_another_exit_does(tmp_path, sink):
    cfg = _cfg(tmp_path)
    src = FakeSource(cfg, [])
    for exc, sent in ((KeyboardInterrupt, 0), (SystemExit("stopped"), 1)):
        def stop(task, choice, exc=exc):
            raise exc
        sink.scans.clear()
        with _env(tmp_path), pytest.raises((KeyboardInterrupt, SystemExit)):
            run.run_label(cfg, ["steer_intent"], src=src, backend_for=stop, now=NOW)
        assert len(sink.scans) == sent, exc


def test_a_task_whose_backend_cannot_run_leaves_the_others_running(tmp_path, sink, capsys):
    _claude_log(tmp_path, "s1", [("u1", "go")])
    cfg = _cfg(tmp_path)
    src = FakeSource(cfg, [_steer("u1", "s1", DAY1)])

    class NotLoaded(ById):           # the way a lazy model load fails: on the first decision
        def distribution(self, req):
            raise backends.BackendUnavailable("label: not ready")

    def not_trained(task, choice):
        if task == "work_category":
            raise backends.BackendUnavailable("label: no encoder is trained")
        return ById()

    with _env(tmp_path):
        rep = run.run_label(cfg, ["steer_intent"], src=src, backend_for=lambda t, c: NotLoaded(),
                            now=NOW)
        assert rep["steer_intent"]["failed"] == 1
        assert not (tmp_path / "state.json").exists() or "steer_intent" not in json.loads(
            (tmp_path / "state.json").read_text()).get("collectors", {}).get("label", {})
        rep = run.run_label(cfg, ["work_category", "steer_intent"], src=src,
                            backend_for=not_trained, now=NOW)
        assert rep["work_category"]["failed"] == 1 and rep["steer_intent"]["rows"] == 1
        wm = json.loads((tmp_path / "state.json").read_text())["collectors"]["label"]
        assert "steer_intent" in wm and "work_category" not in wm
    assert sink.scans[-1]["status"] == "error"
    assert sink.scans[-1]["error"] == "BackendUnavailable"
    assert "no encoder is trained" in capsys.readouterr().err


def test_main_exits_1_with_the_message_for_an_operator_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(run, "run_label", lambda *a, **k: {"steer_intent": Counter(failed=1)})
    assert cli.main(["label", "run"]) == 1
    assert "could not run steer_intent" in capsys.readouterr().err

    def refuse():
        raise AutError("config: nope")
    monkeypatch.setattr(cli, "load_config", refuse)
    assert cli.main(["status"]) == 1
    assert capsys.readouterr().err == "config: nope\n"


def test_missing_mlx_gives_the_install_hint(monkeypatch):
    for mod in ("mlx", "mlx.core", "mlx_lm"):
        monkeypatch.setitem(sys.modules, mod, None)     # makes the import raise ImportError
    b = backends.MlxLetter("some/model")
    with pytest.raises(backends.BackendUnavailable, match=r"agent-usage-tracker\[label\]"):
        b.letter_logits("p", ["A"])
    with pytest.raises(backends.BackendUnavailable, match=r"agent-usage-tracker\[label\]"):
        b.generate("p")


def test_title_units_are_browser_only_salted_and_cached(tmp_path, sink):
    t0 = DAY1.replace(minute=0)
    wins = [_win(t0, 3), _win(t0 + timedelta(minutes=10), 2),
            _win(t0 + timedelta(minutes=20), 5, app="Code", title="main.py", category="editor",
                 rule="default_app"),
            _win(t0 + timedelta(minutes=30), 1, title=None)]
    cfg = _cfg(tmp_path)
    reqs = tasks.units("aw_title_relevance", FakeSource(cfg, wins=wins), t0 - timedelta(1),
                       NOW)
    assert len(reqs) == 1 and reqs[0].facts["minutes"] == 5.0
    plain = hashlib.sha256(b"aw_title|safari|docs - a page").hexdigest()
    assert reqs[0].unit_id != plain and len(reqs[0].unit_id) == 64       # an HMAC, not a hash
    assert [c["id"] for c in reqs[0].candidates] == ["relevant", "not_relevant"]
    with _env(tmp_path):
        b = ById("relevant")
        run.run_label(cfg, ["aw_title_relevance"], src=FakeSource(cfg, wins=wins),
                      backend_for=lambda t, c: b, now=NOW)
        assert sum(len(x) for x in sink.rows) == 1
        assert sink.rows[0][0].attrs["labelled_at"] == sink.scans[-1]["started_at"].isoformat(
            timespec="milliseconds")
        sink.rows.clear()
        # a backfill over the same days: the title is cached, so it is not asked again
        run.run_label(cfg, ["aw_title_relevance"], since=t0 - timedelta(days=2),
                      src=FakeSource(cfg, wins=wins), backend_for=lambda t, c: b, now=NOW)
        assert sink.rows == []
        cache = json.loads((tmp_path / "data" / "aut" / "label" / "cache.json").read_text())
        assert "Docs" not in json.dumps(cache)


def test_notes_prompt_counts_only(tmp_path, sink):
    labels = [("e1", DAY1, {"task": "steer_intent", "unit_id": "a", "label": "correction",
                            "below_tau": False}),
              ("e2", DAY1, {"task": "steer_intent", "unit_id": "a", "label": "correction",
                            "below_tau": False}),
              # same unit and ts: the newer calibrated run wins over the uncalibrated one
              ("e3", DAY1, {"task": "steer_intent", "unit_id": "b", "label": "stop",
                            "below_tau": False, "task_ver": 2,
                            "labelled_at": "2026-11-05T02:00:00.000+00:00"}),
              ("e4", DAY1, {"task": "steer_intent", "unit_id": "b", "label": "stop",
                            "below_tau": True, "task_ver": 2,
                            "labelled_at": "2026-11-03T02:00:00.000+00:00"})]
    eps = [{"id": "e1", "first_ts": DAY1, "last_ts": DAY1, "n_sessions": 2,
            "n_steers": 4, "n_commits": 3, "outcome": "shipped",
            "members": [{"session_id": "s1", "role": "seed", "repo_keys": []}]}]
    cfg = _cfg(tmp_path)
    b = ById()
    with _env(tmp_path):
        rep = run.run_label(cfg, ["daily_note"], src=FakeSource(cfg, episodes=eps, labels=labels),
                            backend_for=lambda t, c: b, now=NOW)
    assert rep["daily_note"]["notes"] == 1 and sink.notes[0][1] == "A quiet day."
    assert sink.notes[0][0] == DAY1.astimezone().date()
    p = b.prompts[0]
    assert "steer_intent: correction 1" in p and "steer_intent: stop 1" in p and "commits 3" in p
    assert run._note_prompt(DAY1.date(), labels[3:] + labels[2:3], []) == \
        run._note_prompt(DAY1.date(), labels[2:4], [])


def test_note_prompt_reads_the_one_latest_label_rule():
    a = {"task": "steer_intent", "label": "stop"}
    rows = [("e1", DAY1, {**a, "unit": "steer", "unit_id": "x"}),           # no below_tau: unknown
            # the same unit_id under two units is two labels
            ("e2", DAY1, {**a, "unit": "steer", "unit_id": "y", "below_tau": False}),
            ("e3", DAY1, {**a, "unit": "episode", "unit_id": "y", "below_tau": False}),
            # a tie on task_ver and labelled_at goes to the larger event_id, in any order
            ("e5", DAY1, {**a, "unit": "steer", "unit_id": "z", "below_tau": False}),
            ("e4", DAY1, {**a, "unit": "steer", "unit_id": "z", "below_tau": True})]
    assert "steer_intent: stop 3" in run._note_prompt(DAY1.date(), rows, [])


def test_a_note_task_on_the_encoder_fails_alone(tmp_path, sink, capsys):
    cfg = _cfg(tmp_path)
    with _env(tmp_path):
        rep = run.run_label(cfg, ["daily_note"], src=FakeSource(cfg),
                            backend_for=lambda t, c: backends.Encoder(t), now=NOW)
    assert rep["daily_note"]["failed"] == 1 and sink.notes == []
    assert "daily_note needs a backend that can write text" in capsys.readouterr().err


def test_windows_go_through_the_collector(tmp_path, monkeypatch):
    t0 = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    evs = [{"timestamp": (t0 + timedelta(minutes=i)).isoformat(), "duration": 60,
            "data": {"app": "Safari", "title": f"page {i} password=hunter2x"}} for i in range(3)]
    evs.append({"timestamp": t0.isoformat(), "duration": 60, "data": {"app": "1Password",
                                                                       "title": "vault"}})

    def http(cfg, method, path, body=None):
        if path == "/api/0/info":
            return {"hostname": "h"}
        if path == "/api/0/buckets/":
            return {"aw-watcher-window_h": {"type": "currentwindow", "hostname": "h",
                                            "created": "2026-10-01T00:00:00+00:00"},
                    "aw-watcher-afk_h": {"type": "afkstatus", "hostname": "h",
                                         "created": "2026-10-01T00:00:00+00:00"}}
        s, e = (aw._parse_aw_ts(x) for x in body["timeperiods"][0].split("/"))
        return [[x for x in evs if s <= aw._parse_aw_ts(x["timestamp"]) < e]]
    monkeypatch.setattr(aw, "_http", http)
    monkeypatch.setattr(aw, "_now", lambda: NOW)
    monkeypatch.setattr(aw, "_MAC", lambda app: None)
    cfg = _cfg(tmp_path, agents=("activitywatch",))
    w = tasks.Source(cfg).windows(t0 - timedelta(hours=1), t0 + timedelta(minutes=2))
    assert [x["title"] for x in w] == ["page 0 password=[redacted]", "page 1 password=[redacted]"]
    assert all(x["rule"].startswith("browser_") for x in w)
    assert tasks.Source(_cfg(tmp_path)).windows(t0, NOW) == []       # activitywatch not enabled
    # a range of several days reads day by day and stops once it is covered
    far = t0 - timedelta(days=3)
    assert len(aw.intervals(cfg, far, t0 + timedelta(minutes=2))) == 3
    assert aw.intervals(cfg, t0, t0) == []

    def down(cfg, method, path, body=None):
        raise OSError("connection refused")
    monkeypatch.setattr(aw, "_http", down)
    with pytest.raises(tasks.SourceUnavailable):
        tasks.Source(cfg).windows(t0, NOW)


def test_unreadable_windows_keep_the_watermark(tmp_path, sink):
    _claude_log(tmp_path, "s1", [("u1", "go")])
    cfg = _cfg(tmp_path)

    class Down(FakeSource):
        def windows(self, since, until):
            raise tasks.SourceUnavailable("aw-server down")
    src = Down(cfg, [_steer("u1", "s1", DAY1)])
    with _env(tmp_path):
        rep = run.run_label(cfg, ["work_category", "steer_intent"], src=src,
                            backend_for=lambda t, c: ById(), now=NOW)
        assert rep["work_category"]["unavailable"] == 1 and rep["steer_intent"]["rows"] == 1
        wm = json.loads((tmp_path / "state.json").read_text())["collectors"]["label"]
        assert "work_category" not in wm and "steer_intent" in wm
        assert sink.scans[-1]["status"] == "error"
        assert sink.scans[-1]["error"] == "SourceUnavailable"


def test_config_and_evalset_stay_below_their_callers():
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).parents[1])}

    def loaded(mod: str) -> set[str]:
        code = f"import sys, {mod}; print(*sys.modules)"
        return set(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                  check=True, env=env).stdout.split())

    assert not {m for m in loaded("agent_usage_tracker.config")
                if ".collectors" in m or ".label" in m}
    assert "agent_usage_tracker.push" not in loaded("agent_usage_tracker.label.evalset")
    # building the parser loads no label module: every `aut` command would pay for them
    assert not {m for m in loaded("agent_usage_tracker.cli") if ".label" in m}


def test_cli_and_config(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(run, "run_label", lambda cfg, t, since, dry_run, local_estimates=False: seen.update(
        t=t, since=since, dry=dry_run) or {})
    assert cli.main(["label", "run", "--task", "steer_intent", "--since", "2026-10-01",
                     "--dry-run"]) == 0
    assert seen["t"] == ["steer_intent"] and seen["dry"] is True
    assert seen["since"].date() == date(2026, 10, 1) and seen["since"].tzinfo is not None
    assert cli.main(["label", "backfill", "--task", "work_category", "--days", "3"]) == 0
    assert seen["since"].date() == date.today() - timedelta(days=3) and seen["dry"] is False
    assert cli.main(["label", "run"]) == 0 and seen["t"] is None
    from agent_usage_tracker import config
    p = tmp_path / "c.toml"
    monkeypatch.setenv("AUT_CONFIG", str(p))
    p.write_text('label_tasks = ["steer_intent", "daily_note"]\nlabel_precision = 0.95\n')
    c = config.load_config()
    assert c.label_tasks == ("steer_intent", "daily_note") and c.label_precision == 0.95
    assert c.label_recall == 0.60 and c.label_llama_server == "llama-server"
    p.write_text('label_recall = 0.8\nlabel_llama_server = "/opt/llama-server"\n')
    c = config.load_config()
    assert c.label_recall == 0.8 and c.label_llama_server == "/opt/llama-server"
    p.write_text("label_recall = 1.5\n")
    with pytest.raises(AutError):
        config.load_config()
    p.write_text('label_tasks = ["vibes"]\n')
    with pytest.raises(AutError):
        config.load_config()


@pytest.mark.skipif(os.name == "nt", reason="aut-scan.sh is a POSIX shell script")
def test_scan_script_runs_label_once_a_day_where_enabled():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        conf = home / ".config" / "agent-usage-tracker"
        conf.mkdir(parents=True)
        calls = home / "calls"
        fake = home / "aut"
        fake.write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\n')
        fake.chmod(0o755)
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        (conf / "scan.env").write_text(f"AUT_BIN={fake}\n")
        script = str(REPO / "packaging" / "launchd" / "aut-scan.sh")
        subprocess.run(["bash", script], env=env, check=True)
        assert "label run" not in calls.read_text()
        (conf / "scan.env").write_text(f"AUT_BIN={fake}\nAUT_LABEL=1\n")
        subprocess.run(["bash", script], env=env, check=True)
        subprocess.run(["bash", script], env=env, check=True)
        lines = calls.read_text().splitlines()
        assert lines.count("label run") == 1 and lines.count("outcomes") == 1
        assert lines.index("outcomes") < lines.index("label run")
        # a failed step logs its own exit status, not the one of the timestamp
        fake.write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\n[ "$1" = scan ] || exit 3\n')
        for mark in ("label-day", "outcomes-day"):
            (conf / mark).unlink()
        subprocess.run(["bash", script], env=env, check=True)
        log = "\n".join(p.read_text() for p in home.rglob("*.log"))
        assert "outcomes done (exit 3)" in log and "label done (exit 3)" in log


def test_cached_snapshot_needs_config_and_every_weight():
    from agent_usage_tracker.label.backends import cached_snapshot
    with tempfile.TemporaryDirectory() as d:
        snap = Path(d) / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"a": "w-1.safetensors", "b": "w-2.safetensors"}}))
        (snap / "w-1.safetensors").write_text("x")
        look = lambda repo, f, revision=None: str(snap / f)          # noqa: E731
        assert cached_snapshot(look, "r", None) is None               # a weight file is missing
        (snap / "w-2.safetensors").write_text("x")
        assert cached_snapshot(look, "r", None) == snap               # no LICENSE needed
        assert cached_snapshot(lambda *a, **k: None, "r", None) is None


def test_cli_wiring(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(teach, "run_teach", lambda cfg, t, n, budget_usd, days, sonnet_only: seen.append(
        ("teach", t, n, budget_usd, days)) or Counter())
    monkeypatch.setattr(gold, "run_gold_review", lambda cfg, t, d: seen.append(("review", t, d)) or {
        "task": TASK, "answered": 0, "skipped": 0, "gold": {}})
    monkeypatch.setattr(bakeoff, "run_eval", lambda cfg, t, d: seen.append(("eval", t, d)) or [])
    monkeypatch.setattr(bakeoff, "run_final", lambda cfg, t, m, d: seen.append(("final", t, m, d))
                        or {"task": t, "model_id": m, "n": 1, "macro_f1_tuned": 1.0,
                            "macro_f1_default": 1.0, "brier": 0.0, "nll": 0.0, "coverage": 1.0,
                            "fails": []})
    monkeypatch.setattr(bakeoff, "choose", lambda cfg, t, m: seen.append(("choose", t, m)) or (
        {"backend": "encoder", "model_id": m, "temperature": 1.0, "platt": None, "tau": {}}
        if m else None))
    monkeypatch.setattr(bakeoff, "train_encoder", lambda cfg, t, d: seen.append(("train", t))
                        or {})
    with _env(tmp_path):
        assert cli.main(["label", "teach", "--task", TASK, "--n", "5"]) == 0
        assert cli.main(["label", "review"]) == 0
        assert cli.main(["label", "eval", "--task", TASK, "--days", "30"]) == 0
        assert cli.main(["label", "eval", "--task", TASK, "--final", "--model", "M"]) == 0
        for half in (["--final"], ["--model", "M"]):       # one without the other is refused
            assert cli.main(["label", "eval", "--task", TASK, *half]) == 1
        assert cli.main(["label", "choose", "--task", TASK]) == 0
        assert cli.main(["label", "choose", "--task", TASK, "--model", "M"]) == 0
        assert cli.main(["label", "train-encoder", "--task", TASK]) == 0
    assert seen == [("teach", TASK, 5, 5.0, LABEL_DAYS), ("review", None, LABEL_DAYS),
                    ("eval", TASK, 30), ("final", TASK, "M", LABEL_DAYS),
                    ("choose", TASK, None), ("choose", TASK, "M"), ("train", TASK)]
    with pytest.raises(SystemExit):
        cli.main(["label", "teach", "--task", "daily_note", "--n", "1"])


def test_a_recent_task_says_since_does_not_apply(capsys):
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    start, until = run._window("soft_outcome", {}, now - timedelta(days=90), now)
    assert start == now - timedelta(days=14) and until is None
    assert "do not apply" in capsys.readouterr().err


def test_two_encoder_tasks_do_not_share_a_backend(tmp_path, monkeypatch):
    built = []

    class Fake:
        model_id, model_rev = "encoder", None

        def distribution(self, req):
            return {}
    monkeypatch.setattr(run, "make_backend", lambda task, c, m, llama: built.append(task) or Fake())
    monkeypatch.setattr(run, "load_choice", lambda name, version: core.Choice(backend="encoder"))
    with _env(tmp_path):
        run.run_label(_cfg(tmp_path), ["steer_intent", "work_category"], dry_run=True,
                      src=FakeSource(_cfg(tmp_path)), now=NOW)
    assert built == ["steer_intent", "work_category"]


def test_a_run_closes_the_backends_it_made(tmp_path, monkeypatch):
    closed = []

    class Closing(ById):
        def close(self):
            closed.append(self.model_id)

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(run, "make_backend", lambda *a: Closing())
    with _env(tmp_path):
        run.run_label(cfg, ["steer_intent"], dry_run=True, src=FakeSource(cfg), now=NOW)
    assert closed == ["fake/byid"]
