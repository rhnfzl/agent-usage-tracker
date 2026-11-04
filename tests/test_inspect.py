"""Hermetic tests for `aut inspect` (the evidence inspector) and the retired `aut report`.

Run: `python tests/test_inspect.py`  (or `pytest`).
Locks the rules:
  - anchors come from seed sessions only and from their own repo's unit; a key two sessions
    share is shown as shared; commit outcomes are the latest per 7-digit prefix;
  - the outcome rule shown is the linker's, manual first;
  - the current label per unit is picked as agents.label_latest picks it, below tau is unknown;
  - local text is escaped, the page is served only under its token, read-only, and its
    correction link opens the existing review form;
  - `aut report` writes nothing and says it is retired.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import cli, inspector, review  # noqa: E402
from agent_usage_tracker.config import AutError  # noqa: E402

K1, K2 = "a1" * 32, "b2" * 32
T0 = datetime(2001, 1, 5, 9, tzinfo=timezone.utc)
EP = "0f0f0f0f-0000-4000-8000-000000000001"


def _raw(outcome="shipped", source="git") -> dict:
    ep = {"id": EP, "first_ts": T0, "last_ts": T0 + timedelta(days=1), "n_sessions": 3,
          "n_seed_sessions": 2, "n_steers": 4, "n_commits": 1, "n_prs": 0, "outcome": outcome,
          "outcome_source": source, "confirmed": False,
          "members": [{"session_id": "s1", "role": "seed", "repo_keys": [K1]},
                      {"session_id": "s2", "role": "seed", "repo_keys": [K1]},
                      {"session_id": "c1", "role": "child", "repo_keys": [K1]}]}

    def lab(eid, unit, uid, label, p, below, ver=1, at="2001-01-06T00:00:00", task="steer_intent"):
        return (eid, T0, {"task": task, "task_ver": ver, "unit": unit, "unit_id": uid,
                          "label": label, "p": p, "below_tau": below, "model_id": "m-small",
                          "labelled_at": at})
    return {
        "episodes": [ep],
        "acts": [("s1", "link", {"repo_key": K1, "branch_key": "bk1", "commit_keys": ["ck1"]}),
                 ("s2", "link", {"repo_key": K1, "branch_key": "bk1", "sha_refs": ["ck1"],
                                 "ticket_keys": ["tk1"]}),
                 ("s2", "link", {"repo_key": K2, "branch_key": "other-repo"}),  # other unit
                 ("c1", "link", {"repo_key": K1, "pr_keys": ["child-pr"]}),      # child: no
                 ("s1", "commit", {"repo_key": K1, "sha": "ABCDEF1234"}),
                 ("s1", "commit", {"repo_key": K1, "sha": "not-a-sha"})],
        "sessions": [("s1", "claude_code", 3, T0, T0 + timedelta(hours=1)),
                     ("s2", "codex", 1, T0 + timedelta(days=1), T0 + timedelta(days=1))],
        "steers": [("st1", "s1"), ("st2", "s1"), ("st3", "s2")],
        "outcomes": [("abcdef1", "merged", 0.8)],
        "manual": [("s2", "shipped", T0 + timedelta(days=2)), ("zz", "open", T0)],
        "corrections": [(T0, "confirm", "s1", None), (T0, "merge", "x9", "s2"),
                        (T0, "reject", "x8", None)],
        "labels": [lab("e1", "steer", "st1", "framing", 0.9, False),
                   lab("e2", "steer", "st2", "correction", 0.4, True),
                   lab("e3", "steer", "st3", "instruction", 0.7, False, ver=1, at="2001-01-06"),
                   lab("e4", "steer", "st3", "review", 0.8, False, ver=2, at=None),  # wins: ver
                   lab("e5", "episode_day", f"{EP}:2001-01-05", "building", 0.95, False,
                       task="work_category"),
                   lab("e6", "aw_block", "blk", "learning", 0.6, False, task="work_category")],
    }


def test_build_collects_evidence():
    view = inspector.build(_raw())
    ep = view["episodes"][0]
    a = ep["anchors"]
    assert a["commit key"] == {"ck1": {"s1", "s2"}}, a
    assert a["branch key"] == {"bk1": {"s1", "s2"}}, a          # other repo's unit left out
    assert a["pr key"] == {}, a                                 # children never join
    assert a["ticket key (a hint, never an edge)"] == {"tk1": {"s2"}}, a
    assert ep["commits"] == [("abcdef1", "merged", 0.8, ["s1"])], ep["commits"]
    assert ep["rule"] == inspector.RULES["shipped"]
    assert [x["label"] for x in ep["labels"]] == ["building"]
    assert ep["steer_labels"] == [(("steer_intent", "framing"), 1),
                                  (("steer_intent", "review"), 1),
                                  (("steer_intent", "unknown"), 1)], ep["steer_labels"]
    assert [c[1] for c in ep["corrections"]] == ["confirm", "merge"]
    assert [s.get("agent") for s in ep["sessions"]] == ["claude_code", "codex", None]
    assert [x["unit_id"] for x in view["labels"]] == ["blk"]
    print("ok build_collects_evidence")


def test_outcome_rule_manual_first_and_no_outcome_yet():
    raw = _raw("shipped", "manual")
    raw["outcomes"] = []
    ep = inspector.build(raw)["episodes"][0]
    assert ep["rule"].startswith(inspector.RULES["manual"]) and "(shipped, " in ep["rule"]
    assert ep["commits"][0][1] == "no outcome yet"
    for out in ("reworked", "open", "ruled_out", "undecided", "no_code"):
        assert inspector.build(_raw(out))["episodes"][0]["rule"] == inspector.RULES[out]
    print("ok outcome_rule_manual_first_and_no_outcome_yet")


def test_latest_labels_matches_label_latest():
    rows = [("a", T0, {"task": "t", "unit": "u", "unit_id": "1", "task_ver": 1,
                       "labelled_at": "2001-01-02", "label": "old"}),
            ("b", T0, {"task": "t", "unit": "u", "unit_id": "1", "task_ver": 1,
                       "labelled_at": "2001-01-03", "label": "new"}),
            ("c", T0, {"task": "t", "unit": "u", "unit_id": "1", "task_ver": 1, "label": "none"}),
            ("d", T0, {"task": "t", "unit": "u", "unit_id": "2", "label": "x"})]
    got = {x["unit_id"]: x for x in inspector.latest_labels(rows)}
    assert got["1"]["label"] == "new" and got["2"]["below_tau"] is True, got
    print("ok latest_labels_matches_label_latest")


def test_page_escapes_and_serves_read_only_with_correction_link():
    view = inspector.build(_raw())
    info = {"s1": {"titles": ["<script>x</script>"], "branches": ["a&b"]}}
    view["episodes"][0]["anchors"]["branch key"]["bk9"] = {"<b>x", "s1"}   # sink-set ids
    page = inspector.render(view, info, "tok", "Label st3", label="st3")
    assert "<script>x" not in page and "&lt;script&gt;x" in page and "a&amp;b" in page
    assert "<b>x" not in page and "&lt;b&gt;x" in page
    assert 'href="/tok/correct"' in page and "Every label row of" in page
    assert "<form" not in page                                    # the inspector writes nothing
    written: list = []
    ready, box = threading.Event(), {}

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: box.update(s=review.serve(
        view["episodes"], written.extend, inspector.pages(view, info, "E"), open_browser=False,
        on_ready=on_ready, now=T0)))
    t.start()
    assert ready.wait(10)
    url = box["url"]
    assert url.startswith("http://127.0.0.1:"), url
    with urllib.request.urlopen(url, timeout=5) as r:
        assert b"Rule that fired" in r.read()
    with urllib.request.urlopen(url + "/correct", timeout=5) as r:
        assert b'action="/' in r.read()
    try:
        urllib.request.urlopen(url.rsplit("/", 1)[0] + "/other/correct", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("served without the token")
    assert written == []
    body = b"a0=right"
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5) as r:
        assert r.status == 200
    t.join(10)
    assert [w["action"] for w in written] == ["confirm"] and box["s"]["reviewed"] == 1
    print("ok page_escapes_and_serves_read_only_with_correction_link")


def test_run_inspect_passes_mode_and_stops_on_empty():
    from agent_usage_tracker.config import Config
    seen = {}

    def fetch(dsn, **kw):
        seen.update(kw)
        return _raw()
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(machine="test", agents=(), webhook_url=None, webhook_secret=None,
                     pg_dsn="postgresql://ignored", home=Path(d))
        out = inspector.run_inspect(cfg, day="2001-01-05", fetch_fn=fetch,
                                    serve_fn=lambda *a, **k: {"reviewed": 0, "right": 0})
        assert out["reviewed"] == 0 and seen == {"episode": None, "day": "2001-01-05",
                                                 "label": None}
        empty = {k: [] for k in _raw()}
        try:
            inspector.run_inspect(cfg, episode="0f0f", fetch_fn=lambda dsn, **kw: empty)
        except AutError as exc:
            assert "nothing found" in str(exc)
        else:
            raise AssertionError("no AutError")
    print("ok run_inspect_passes_mode_and_stops_on_empty")


def test_exactly_one_selector():
    for kw in ({}, {"episode": "0f0f", "day": "2001-01-05"}):
        for call in (inspector.fetch, inspector.run_inspect):
            try:
                call("not reached", **kw)
            except ValueError as exc:
                assert "exactly one" in str(exc)
            else:
                raise AssertionError(f"accepted {kw}")
    print("ok exactly_one_selector")


def test_fetch_checks_episode_and_day_uses_activity_rows():
    import types
    sqls: list = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            sqls.append((sql, params))
            return types.SimpleNamespace(fetchall=lambda: [])
    fake = types.ModuleType("psycopg")
    fake.connect = lambda *a, **k: Conn()
    old = sys.modules.get("psycopg")
    sys.modules["psycopg"] = fake
    try:
        for bad in (" ", "", "0f_f", "0f%", "nope", "0f0"):
            try:
                inspector.fetch("dsn", episode=bad)
            except AutError as exc:
                assert "episode id or prefix" in str(exc)
            else:
                raise AssertionError(f"accepted --episode {bad!r}")
        assert sqls == []
        inspector.fetch("dsn", episode=" 0F0F ")
        assert sqls[0][1] == ("0f0f%",)
        sqls.clear()
        inspector.fetch("dsn", day="2001-01-05")
        assert sqls[0][1] == inspector._day_bounds("2001-01-05")
    finally:
        if old is None:
            sys.modules.pop("psycopg")
        else:
            sys.modules["psycopg"] = old
    print("ok fetch_checks_episode_and_day_uses_activity_rows")


def test_day_bounds_are_local_midnights():
    s, e = inspector._day_bounds("2001-03-25")
    assert (s.hour, e.hour, (e.date() - s.date()).days) == (0, 0, 1)
    try:
        inspector._day_bounds("25-03-2001")
    except AutError:
        pass
    else:
        raise AssertionError("bad day accepted")
    print("ok day_bounds_are_local_midnights")


def test_report_is_retired_and_writes_nothing(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "metrics.json"
        err = io.StringIO()
        monkeypatch.setenv("AUT_CONFIG", str(Path(d) / "missing.toml"))
        with contextlib.redirect_stderr(err):
            assert cli.main(["report", "--out", str(out), "--idle-gap", "60"]) == 0
        assert not out.exists() and "retired" in err.getvalue()
    print("ok report_is_retired_and_writes_nothing")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
