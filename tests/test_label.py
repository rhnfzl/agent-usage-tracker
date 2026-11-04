"""Hermetic tests for `aut label` (the local-model labels). No model and no mlx: every backend
here is a fake that returns fixed letter logits.

Run: `python tests/test_label.py`  (or `pytest`).
Locks the rules:
  - a backend's distribution is normalized over the candidates, and the original and reversed
    candidate orders are averaged (letter position bias cancels);
  - temperature scaling keeps the top answer, a fitted T > 1 tempers an overconfident model, tau
    is the smallest p reaching the precision bar, and a label without tau never passes;
  - a label row carries enums, numbers and versions only, never the state text, and its event_id
    is stable for the same task, version, unit, model and calibration;
  - steer text is read from the local Claude Code log and redacted; a steer without local text is
    not labelled;
  - a run sends rows in batches, advances its watermark only when not a dry run, writes one
    heartbeat row (collector `label`), stops at the time budget, and never re-asks a cached title;
  - the nightly note's prompt is built from counts only;
  - aut-scan.sh runs `aut label run` once a day, only where AUT_LABEL=1.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import cli, push  # noqa: E402
from agent_usage_tracker.collectors import activitywatch as aw  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import make_event_id  # noqa: E402
from agent_usage_tracker.label import backends, core, names, run, tasks  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SALT = "test-salt-for-labels-0123456789abcdef"
NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
DAY1 = NOW - timedelta(days=1)          # inside yesterday in every zone tests run in
SECRET = "SECRET-PROMPT acme-corp merger"


def _cfg(home: Path, **kw) -> Config:
    base = dict(machine="test", agents=(), webhook_url=None, webhook_secret=None,
                pg_dsn="postgresql://x", home=home, repo_key_salt=SALT)
    return Config(**{**base, **kw})


class FixedLetters(backends.LetterBackend):
    """Logits per letter position, whatever the prompt says (pure position bias)."""
    model_id, model_rev = "fake/model", "rev1"

    def __init__(self, logits):
        self.logits, self.prompts = logits, []

    def letter_logits(self, prompt, letters):
        self.prompts.append(prompt)
        return self.logits[:len(letters)]


class ById(backends.LetterBackend):
    """Logit 5 for the letter showing `want`, 0 for the rest: reads the options in the prompt."""
    model_id, model_rev = "fake/byid", "rev2"

    def __init__(self, want="instruction"):
        self.want, self.prompts = want, []

    def letter_logits(self, prompt, letters):
        self.prompts.append(prompt)
        shown = dict(re.findall(r"^([A-J])\. (\w+):", prompt, re.M))
        return [5.0 if shown[x] == self.want else 0.0 for x in letters]

    def generate(self, prompt, max_tokens=300):
        self.prompts.append(prompt)
        return "A quiet day."


def _req(n=3) -> core.Request:
    t = tasks.TASKS["steer_intent"]
    r = t.request("steer", "u1", NOW, {"text": SECRET})
    r.candidates = r.candidates[:n]
    return r


def test_orders_are_averaged_and_normalized():
    b = FixedLetters([3.0, 0.0, 0.0])
    d = b.distribution(_req(3))
    assert abs(sum(d.values()) - 1) < 1e-9
    ids = [c["id"] for c in _req(3).candidates]
    # the first and last candidate each held letter A once: position bias cancels between them
    assert abs(d[ids[0]] - d[ids[2]]) < 1e-12 and d[ids[1]] < d[ids[0]]
    assert len(b.prompts) == 2 and b.prompts[0] != b.prompts[1]
    assert "A. framing:" in b.prompts[0] and "A. clarification:" in b.prompts[1]


def test_content_wins_over_position():
    d = ById("instruction").distribution(_req(9))
    assert max(d, key=d.get) == "instruction" and d["instruction"] > 0.9
    assert abs(sum(d.values()) - 1) < 1e-9


def test_temperature_and_tau():
    d = {"a": 0.9, "b": 0.08, "c": 0.02}
    hot = core.temper(d, 2.0)
    assert max(hot, key=hot.get) == "a" and hot["a"] < 0.9
    assert all(abs(core.temper(d, 1.0)[k] - v) < 1e-9 for k, v in d.items())
    # overconfident: always 0.99 on its answer, right 7 times in 10 -> T > 1
    dists = [{"a": 0.99, "b": 0.01}] * 10
    golds = ["a"] * 7 + ["b"] * 3
    t = core.fit_temperature(dists, golds)
    assert t > 1.5 and abs(core.temper(dists[0], t)["a"] - 0.7) < 0.02
    assert core.fit_temperature([], []) == 1.0
    items = [("a", 0.95, "a"), ("a", 0.9, "a"), ("a", 0.8, "a"), ("a", 0.7, "b"),
             ("a", 0.6, "a"), ("b", 0.9, "a"), ("c", 0.55, "c")]
    tau = core.fit_tau(items, precision=0.75)
    assert tau["a"] == 0.6          # 4 of 5 right at p >= 0.6
    assert tau["b"] is None         # never right
    assert tau["c"] == 0.55
    assert core.fit_tau([("c", 0.3, "c")])["c"] == core.TAU_FLOOR


def test_decide_below_tau():
    req = _req(3)
    raw = {"framing": 0.7, "instruction": 0.2, "clarification": 0.1}
    none = core.decide(req, raw, core.Choice(), "m", "r")
    assert none.chosen == "framing" and none.below_tau and none.calib_ver == "none"
    ch = core.Choice(temperature=1.0, tau={"framing": 0.6}, calib_ver="steer_intent@1/x")
    assert not core.decide(req, raw, ch, "m", "r").below_tau
    ch.tau["framing"] = 0.8
    assert core.decide(req, raw, ch, "m", "r").below_tau


def test_label_row_has_no_text_and_a_stable_id():
    req = _req(9)
    res = core.decide(req, ById().distribution(req), core.Choice(), "fake/byid", "rev2")
    ev = core.label_event(req, res)
    blob = json.dumps([ev.event_id, ev.attrs, ev.session_id, ev.turn_id])
    for word in SECRET.split():
        assert word not in blob
    assert set(ev.attrs) == {"task", "task_ver", "unit", "unit_id", "label", "p", "dist",
                             "below_tau", "model_id", "model_rev", "prompt_ver", "calib_ver",
                             "labelled_at"}
    assert datetime.fromisoformat(ev.attrs["labelled_at"]).tzinfo is not None
    assert core.label_event(req, res, "2026-11-04T02:13:00.000+00:00").event_id == ev.event_id
    assert ev.kind == "label" and ev.ts == req.ts and ev.attrs["label"] == "instruction"
    assert set(ev.attrs["dist"]) <= {c["id"] for c in req.candidates}
    again = core.label_event(req, core.decide(req, ById().distribution(req), core.Choice(),
                                              "fake/byid", "rev2"))
    assert again.event_id == ev.event_id
    other = core.label_event(req, core.decide(req, {"framing": 1.0}, core.Choice(), "m2", "r"))
    assert other.event_id != ev.event_id
    req.rev = "2026-11-02T10:00:00+00:00"           # a changed episode: a new row
    assert core.label_event(req, res).event_id != ev.event_id
    req.rev = None
    req.unit_id = "u2"
    assert core.label_event(req, res).event_id != ev.event_id


def test_choice_file_and_backend_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))   # no trained encoder here
    assert core.load_choice("steer_intent") == core.Choice()
    (tmp_path / "label.json").write_text(json.dumps({"tasks": {"steer_intent": {
        "backend": "mlx_letter", "model_id": "gemma-4-31b", "temperature": 1.4,
        "tau": {"stop": 0.7}, "calib_ver": "steer_intent@1/2026-11-20", "junk": 1}}}))
    c = core.load_choice("steer_intent")
    assert c.temperature == 1.4 and c.tau == {"stop": 0.7}
    b = backends.make_backend("steer_intent", c, "unused")
    assert isinstance(b, backends.MlxLetter) and b.model_id == backends.MODELS["gemma-4-31b"]
    with pytest.raises(SystemExit):
        backends.make_backend("x", core.Choice(model_id="pplx-decider-flash-v1.2"), "m")
    with pytest.raises(SystemExit):
        backends.Encoder("steer_intent").distribution(_req())


def _claude_log(home: Path, sid: str, recs: list[tuple[str, str]]) -> None:
    proj = home / ".claude" / "projects" / "-p"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "uuid": u, "sessionId": sid,
                         "message": {"role": "user", "content": t}}) for u, t in recs]
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def _steer(uid: str, sid: str, ts: datetime, agent="claude_code", **attrs) -> dict:
    return {"event_id": make_event_id(agent, "steer", uid), "ts": ts, "agent": agent,
            "session_id": sid, "attrs": {"via": "typed", **attrs}}


class FakeSource(tasks.Source):
    def __init__(self, cfg, steers=(), episodes=(), acts=(), wins=(), labels=()):
        super().__init__(cfg)
        self._s, self._e, self._a, self._w, self._l = (list(x) for x in
                                                        (steers, episodes, acts, wins, labels))

    def steers(self, since):
        sids = {s["session_id"] for s in self._s if s["ts"] >= since}
        return [s for s in self._s if s["session_id"] in sids]

    def episodes(self, since):
        return [e for e in self._e if e["last_ts"] >= since]

    def activity(self, sessions, since, until):
        return [a for a in self._a if a["session_id"] in sessions and since <= a["ts"] < until]

    def labels(self, since, until):
        return self._l

    def windows(self, since, until):
        return [w for w in self._w if since <= w["start"] < until]


def test_steer_without_uuid_finds_its_text_under_the_collectors_id(tmp_path):
    # the id the collector gives a record with no uuid: path:timestamp
    proj = tmp_path / ".claude" / "projects" / "-p"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    ts = "2001-01-05T09:00:00Z"
    f.write_text(json.dumps({"type": "user", "timestamp": ts, "sessionId": "s1",
                             "message": {"role": "user", "content": "old log prompt"}}) + "\n")
    src = FakeSource(_cfg(tmp_path))
    steer = _steer(f"{f}:{ts}", "s1", DAY1)
    assert src.texts([steer]) == {steer["event_id"]: "old log prompt"}


def test_steer_units_read_local_redacted_text(tmp_path):
    t0 = DAY1
    _claude_log(tmp_path, "s1", [
        ("u1", "build the export, key sk-abcdefghijklmnopqrstuvwx"),
        ("u2", "no, mail bob@example.com first"),
        ("u3", "x" * 5000)])
    src = FakeSource(_cfg(tmp_path), steers=[
        _steer("u1", "s1", t0), _steer("u2", "s1", t0 + timedelta(seconds=90),
                                       after_interrupt=True),
        _steer("u3", "s1", t0 + timedelta(seconds=120)),
        _steer("gone", "s1", t0 + timedelta(seconds=150)),          # not in the local log
        _steer("u1", "s1", t0 + timedelta(seconds=160), agent="codex")])
    reqs = tasks.units("steer_intent", src, t0 + timedelta(seconds=1), None)
    assert [r.unit_id for r in reqs] == [make_event_id("claude_code", "steer", u)
                                         for u in ("u2", "u3")]
    st = reqs[0].state
    assert st["text"] == "no, mail [redacted] first"
    assert "sk-" not in st["previous_text"] and "[redacted]" in st["previous_text"]
    assert st["index_in_session"] == 1 and st["seconds_since_previous"] == 90
    assert st["after_interrupt"] is True
    assert len(reqs[1].state["text"]) <= tasks.TEXT_MAX


@contextlib.contextmanager
def _env(tmp: Path):
    keys = ("AUT_STATE", "XDG_DATA_HOME", "AUT_CONFIG")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(AUT_STATE=str(tmp / "state.json"), XDG_DATA_HOME=str(tmp / "data"),
                      AUT_CONFIG=str(tmp / "config.toml"))
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


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
    with pytest.raises(SystemExit):
        run.run_label(_cfg(tmp_path, pg_dsn=None), ["steer_intent"])


def test_ctrl_c_sends_no_heartbeat_but_another_exit_does(tmp_path, sink):
    cfg = _cfg(tmp_path)
    src = FakeSource(cfg, [])
    for exc, sent in ((KeyboardInterrupt, 0), (SystemExit("no mlx"), 1)):
        def stop(task, choice, exc=exc):
            raise exc
        sink.scans.clear()
        with _env(tmp_path), pytest.raises((KeyboardInterrupt, SystemExit)):
            run.run_label(cfg, ["steer_intent"], src=src, backend_for=stop, now=NOW)
        assert len(sink.scans) == sent, exc


def _win(start, minutes, app="Safari", title="Docs - a page", category="web-work",
         rule="browser_work_title"):
    return {"start": start, "end": start + timedelta(minutes=minutes), "app": app,
            "title": title, "category": category, "rule": rule}


def test_title_units_are_browser_only_salted_and_cached(tmp_path, sink):
    t0 = DAY1.replace(minute=0)
    wins = [_win(t0, 3), _win(t0 + timedelta(minutes=10), 2),
            _win(t0 + timedelta(minutes=20), 5, app="Code", title="main.py", category="editor",
                 rule="default_app"),
            _win(t0 + timedelta(minutes=30), 1, title=None)]
    cfg = _cfg(tmp_path)
    reqs = tasks.units("aw_title_relevance", FakeSource(cfg, wins=wins), t0 - timedelta(1),
                       NOW)
    assert len(reqs) == 1 and reqs[0].state["minutes"] == 5.0
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


def test_work_category_units(tmp_path):
    t0 = DAY1.replace(minute=0)
    _claude_log(tmp_path, "s1", [("u1", "plan the export"), ("u2", "now write it")])
    steers = [_steer("u1", "s1", t0), _steer("u2", "s1", t0 + timedelta(minutes=5))]
    eps = [{"episode_id": "e1", "first_ts": t0, "last_ts": t0 + timedelta(minutes=5),
            "n_sessions": 1, "n_steers": 2, "n_commits": 1, "outcome": "open",
            "sessions": ["s1"]}]
    acts = [{"session_id": "s1", "ts": t0 + timedelta(minutes=1), "kind": "turn",
             "attrs": {"by_tool": {"Edit": 3, "Bash": 1}, "tool_errors": 1}},
            {"session_id": "s1", "ts": t0 + timedelta(minutes=2), "kind": "commit", "attrs": {}}]
    wins = [_win(t0 - timedelta(minutes=2), 4, app="Slack", category="comms", rule="default_app"),
            _win(t0 + timedelta(minutes=30), 4, app="Slack", category="comms",
                 rule="default_app"),
            _win(t0 + timedelta(minutes=34, seconds=30), 3, app="Slack", category="comms",
                 rule="default_app"),
            _win(t0 + timedelta(minutes=50), 2, app="Notes", category="docs")]
    # a Codex session in the same episode: its steer and turns stay out of the episode facts
    steers.append(_steer("c1", "s2", t0 + timedelta(minutes=6), agent="codex"))
    eps[0]["sessions"].append("s2")
    acts.append({"session_id": "s2", "ts": t0 + timedelta(minutes=7), "kind": "turn",
                 "attrs": {"by_tool": {"shell": 9}, "tool_errors": 4}})
    cfg = _cfg(tmp_path)
    reqs = tasks.units("work_category", FakeSource(cfg, steers, eps, acts, wins),
                       run.local_midnight(DAY1.astimezone().date()), run.local_midnight(
                           NOW.astimezone().date()))
    by = {r.unit: r for r in reqs}
    assert set(by) == {"episode_day", "aw_block"} and len(reqs) == 2
    st = by["episode_day"].state
    assert st["first_steer"] == "plan the export" and st["last_steers"] == ["now write it"]
    assert st["tool_calls_by_tool"] == {"Edit": 3, "Bash": 1} and st["commits"] == 1
    assert st["steers"] == 2 and st["sessions"] == 1 and st["tool_errors"] == 1
    assert by["episode_day"].unit_id == f"e1:{t0.astimezone().date().isoformat()}"
    # the 7-minute Slack block counts; the one with a steer inside and the 2-minute one do not
    assert by["aw_block"].state["minutes"] == 7 and by["aw_block"].state["apps"] == ["Slack"]


def test_soft_outcome_only_no_code(tmp_path):
    _claude_log(tmp_path, "s1", [("u1", "look into the queue"), ("u2", "park it")])
    start = DAY1 - timedelta(days=3)
    steers = [_steer("u1", "s1", DAY1), _steer("u2", "s2", DAY1),
              _steer("x1", "s3", DAY1, agent="codex")]
    ep = {"first_ts": start, "last_ts": DAY1, "n_sessions": 2, "n_steers": 2, "n_commits": 0}
    eps = [{**ep, "episode_id": "e1", "outcome": "no_code", "sessions": ["s1", "s3"]},
           {**ep, "episode_id": "e2", "outcome": "shipped", "sessions": ["s1"]}]
    reqs = tasks.units("soft_outcome", FakeSource(_cfg(tmp_path), steers, eps),
                       NOW - timedelta(days=14), NOW)
    assert [r.unit_id for r in reqs] == ["e1"]
    assert reqs[0].state["last_steers"] == ["look into the queue"]
    assert reqs[0].none_fits is None and reqs[0].state["days_idle"] == 1
    assert reqs[0].state["steers"] == 1 and reqs[0].state["sessions"] == 1     # Claude Code only
    # the row's ts is the episode start (stable); new activity re-asks with a new id
    assert reqs[0].ts == start.isoformat()
    res = core.decide(reqs[0], {"resolved": 1.0}, core.Choice(), "m", "r")
    first = core.label_event(reqs[0], res).event_id
    eps[0]["last_ts"] = NOW
    again = tasks.units("soft_outcome", FakeSource(_cfg(tmp_path), steers, eps),
                        NOW - timedelta(days=14), NOW)[0]
    assert again.ts == reqs[0].ts and core.label_event(again, res).event_id != first


def test_notes_prompt_counts_only(tmp_path, sink):
    labels = [{"task": "steer_intent", "unit_id": "a", "label": "correction", "below_tau": False},
              {"task": "steer_intent", "unit_id": "a", "label": "correction", "below_tau": False},
              # same unit and ts: the newer calibrated run wins over the uncalibrated one
              {"task": "steer_intent", "unit_id": "b", "label": "stop", "below_tau": False,
               "task_ver": 1, "labelled_at": "2026-11-05T02:00:00.000+00:00"},
              {"task": "steer_intent", "unit_id": "b", "label": "stop", "below_tau": True,
               "task_ver": 1, "labelled_at": "2026-11-03T02:00:00.000+00:00"}]
    eps = [{"episode_id": "e1", "first_ts": DAY1, "last_ts": DAY1, "n_sessions": 2,
            "n_steers": 4, "n_commits": 3, "outcome": "shipped", "sessions": ["s1"]}]
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
        s, e = (aw._parse(x) for x in body["timeperiods"][0].split("/"))
        return [[x for x in evs if s <= aw._parse(x["timestamp"]) < e]]
    monkeypatch.setattr(aw, "_http", http)
    monkeypatch.setattr(aw, "_now", lambda: NOW)
    monkeypatch.setattr(aw, "_MacCategories", lambda: lambda app: None)
    cfg = _cfg(tmp_path, agents=("activitywatch",))
    w = tasks.Source(cfg).windows(t0 - timedelta(hours=1), t0 + timedelta(minutes=2))
    assert [x["title"] for x in w] == ["page 0 password=[redacted]", "page 1 password=[redacted]"]
    assert all(x["rule"].startswith("browser_") for x in w)
    assert tasks.Source(_cfg(tmp_path)).windows(t0, NOW) == []       # activitywatch not enabled

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


def test_task_names_match_the_tasks():
    assert names.DECISION_TASKS == tuple(tasks.TASKS) and names.NOTE not in tasks.TASKS


def test_config_and_evalset_stay_below_their_callers():
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).parents[1])}

    def loaded(mod: str) -> set[str]:
        code = f"import sys, {mod}; print(*sys.modules)"
        return set(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                  check=True, env=env).stdout.split())

    assert not {m for m in loaded("agent_usage_tracker.config")
                if ".collectors" in m or m.endswith((".tasks", ".core"))}
    assert "agent_usage_tracker.push" not in loaded("agent_usage_tracker.label.evalset")
    # building the parser reads only the names: every `aut` command would pay for the rest
    assert not {m for m in loaded("agent_usage_tracker.cli")
                if ".label." in m and not m.endswith(".names")}


def test_cli_and_config(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(run, "run_label", lambda cfg, t, since, dry: seen.update(
        t=t, since=since, dry=dry) or {})
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
    p.write_text('label_tasks = ["vibes"]\n')
    with pytest.raises(SystemExit):
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


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
    print("ok cached_snapshot")
