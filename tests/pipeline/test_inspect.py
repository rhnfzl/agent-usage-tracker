"""Hermetic tests for `aut inspect` (the evidence inspector).

Locks the rules:
  - anchors come from seed sessions only and from their own repo's unit; a key two sessions
    share is shown as shared; commit outcomes are the latest per 7-digit prefix;
  - the outcome rule shown is the linker's, manual first;
  - the current label per unit is picked as agents.label_latest picks it, below tau is unknown;
  - local text is escaped, the page is served only under its token, and its one write is its
    correction link, which opens the existing review form.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import inspector
from agent_usage_tracker import cli, review
from agent_usage_tracker.config import AutError

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
    assert [s.get("agent") for s in ep["member_activity"]] == ["claude_code", "codex", None]
    assert [x["unit_id"] for x in view["labels"]] == ["blk"]


def test_outcome_rule_manual_first_and_no_outcome_yet():
    raw = _raw("shipped", "manual")
    raw["outcomes"] = []
    ep = inspector.build(raw)["episodes"][0]
    assert ep["rule"].startswith(inspector.RULES["manual"]) and "(shipped, " in ep["rule"]
    assert ep["commits"][0][1] == "no outcome yet"
    for out in ("reworked", "open", "ruled_out", "undecided", "no_code"):
        assert inspector.build(_raw(out))["episodes"][0]["rule"] == inspector.RULES[out]


def test_latest_labels_matches_label_latest():
    rows = [("a", T0, {"task": "t", "unit": "u", "unit_id": "1", "task_ver": 1,
                       "labelled_at": "2001-01-02", "label": "old"}),
            ("b", T0, {"task": "t", "unit": "u", "unit_id": "1", "task_ver": 1,
                       "labelled_at": "2001-01-03", "label": "new"}),
            ("c", T0, {"task": "t", "unit": "u", "unit_id": "1", "task_ver": 1, "label": "none"}),
            ("d", T0, {"task": "t", "unit": "u", "unit_id": "2", "label": "x"})]
    got = {x["unit_id"]: x for x in inspector.latest_labels(rows)}
    assert got["1"]["label"] == "new" and got["2"]["below_tau"] is True, got


def test_page_escapes_and_serves_read_only_with_correction_link():
    view = inspector.build(_raw())
    info = {"s1": {"titles": ["<script>x</script>"], "branches": ["a&b"]}}
    view["episodes"][0]["anchors"]["branch key"]["bk9"] = {"<b>x", "s1"}   # sink-set ids
    page = inspector.render(view, info, "tok", "Label st3", label="st3")
    assert "<script>x" not in page and "&lt;script&gt;x" in page and "a&amp;b" in page
    assert "<b>x" not in page and "&lt;b&gt;x" in page
    assert 'href="/tok/correct"' in page and "Every label row of" in page
    assert "read-only" not in page
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


def test_run_inspect_passes_mode_and_stops_on_empty(monkeypatch):
    from agent_usage_tracker.config import Config
    seen = []

    def fetch(dsn, kind, value):
        seen.append((kind, value))
        return _raw()
    monkeypatch.setattr(inspector, "fetch", fetch)
    monkeypatch.setattr(review, "serve", lambda *a, **k: {"reviewed": 0, "right": 0})
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(machine="test", agents=(), webhook_url=None, webhook_secret=None,
                     pg_dsn="postgresql://ignored", home=Path(d))
        out = inspector.run_inspect(cfg, "day", "2001-01-05")
        assert out["reviewed"] == 0 and seen == [("day", "2001-01-05")]
        empty = {k: [] for k in _raw()}
        monkeypatch.setattr(inspector, "fetch", lambda dsn, kind, value: empty)
        try:
            inspector.run_inspect(cfg, "episode", "0f0f")
        except AutError as exc:
            assert "nothing found" in str(exc)
        else:
            raise AssertionError("no AutError")


def test_exactly_one_selector():
    for args in ((), ("episode",), ("episode", "0f0f", "2001-01-05")):
        for call in (inspector.fetch, inspector.run_inspect):
            with pytest.raises(TypeError):
                call("not reached", *args)


def test_fetch_checks_episode_and_day_uses_activity_rows(monkeypatch):
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
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    for bad in (" ", "", "0f_f", "0f%", "nope", "0f0"):
        try:
            inspector.fetch("dsn", "episode", bad)
        except AutError as exc:
            assert "episode id or prefix" in str(exc)
        else:
            raise AssertionError(f"accepted --episode {bad!r}")
    assert sqls == []
    inspector.fetch("dsn", "episode", " 0F0F ")
    assert sqls[0][1] == ("0f0f%",)
    sqls.clear()
    inspector.fetch("dsn", "day", "2001-01-05")
    assert sqls[0][1] == inspector._day_bounds("2001-01-05")


def test_day_bounds_are_local_midnights():
    s, e = inspector._day_bounds("2001-03-25")
    assert (s.hour, e.hour, (e.date() - s.date()).days) == (0, 0, 1)
    try:
        inspector._day_bounds("25-03-2001")
    except AutError:
        pass
    else:
        raise AssertionError("bad day accepted")


def test_report_is_gone():
    with pytest.raises(SystemExit) as exc:
        cli.main(["report"])
    assert exc.value.code == 2


def test_day_bounds_are_local_midnights_and_refuse_another_format():
    start, end = inspector._day_bounds("2001-01-05")
    assert start.tzinfo is not None and (start.day, start.hour) == (5, 0) and (end.day, end.hour) == (6, 0)
    for bad in ("2001-1", "05-01-2001", ""):
        with pytest.raises(AutError, match="--day is YYYY-MM-DD"):
            inspector._day_bounds(bad)


def test_episode_prefix_lets_no_like_wildcard_through():
    assert inspector._episode_prefix("  0F0F-AB ") == "0f0f-ab"
    for bad in ("abc", "abcd%", "ab_cd", "xyz1", "a" * 37):
        with pytest.raises(AutError, match="--episode"):
            inspector._episode_prefix(bad)


def test_fetch_wants_exactly_one_selector_before_it_connects():
    for args in ((), ("episode",), ("episode", "abcd", "2001-01-05")):
        with pytest.raises(TypeError):
            inspector.fetch("postgresql://never-used", *args)


def test_add_anchors_files_lists_and_scalars_under_their_type():
    anchors = {name: {} for name, _f in inspector.ANCHORS}
    inspector._add_anchors(anchors, "s1", {"commit_keys": ["k"], "sha_refs": ["k", "m"],
                                           "branch_key": "b", "pr_keys": [], "ticket_keys": None})
    assert anchors["commit key"] == {"k": {"s1"}, "m": {"s1"}}       # one type for both fields
    assert anchors["branch key"] == {"b": {"s1"}}
    assert anchors["pr key"] == {} and anchors["ticket key (a hint, never an edge)"] == {}


def test_link_keys_read_only_members_of_this_repo():
    members = {"s1": {"repo_keys": ["K"]}, "c1": {"repo_keys": ["K"]}, "n": {"repo_keys": [None]}}
    acts = [("s1", "link", {"repo_key": "K", "branch_key": "here"}),
            ("s1", "link", {"repo_key": "other", "branch_key": "there"}),
            ("c1", "link", {"repo_key": "K", "branch_key": "child"}),
            ("zz", "link", {"branch_key": "stranger"}),
            ("n", "link", None),
            ("s1", "commit", {"repo_key": "K", "sha": "ABCDEF12"}),
            ("c1", "commit", {"sha": "abcdef1ff"}),
            ("s1", "commit", {"sha": "short"}),
            ("s1", "commit", {"repo_key": "other", "sha": "1234567"})]
    anchors, commits = inspector._link_keys(members, {"s1"}, acts)
    assert anchors["branch key"] == {"here": {"s1"}}
    assert commits == {"abcdef1": {"s1", "c1"}}


def test_rule_of_names_the_rule_and_the_seed_sessions_manual_outcome():
    t = T0
    assert inspector._rule_of({"outcome_source": "git", "outcome": "new-rule"}, [], set()) == "new-rule"
    manual = {"outcome_source": "manual", "outcome": "shipped"}
    assert inspector._rule_of(manual, [("other", "open", t)], {"s1"}) == inspector.RULES["manual"]
    rows = [("s1", "shipped", t), ("s1", "open", t)]      # newest first, as fetch orders them
    assert f"(shipped, {t.astimezone():%Y-%m-%d %H:%M})" in inspector._rule_of(manual, rows, {"s1"})


def test_episode_labels_take_their_own_and_mark_what_they_took():
    labs = [{"unit": "episode", "unit_id": EP, "task": "t", "below_tau": False, "label": "a"},
            {"unit": "episode_day", "unit_id": f"{EP}:2001-01-05", "task": "t", "below_tau": False,
             "label": "b"},
            {"unit": "steer", "unit_id": "st", "task": "t", "below_tau": True, "label": "c"},
            {"unit": "steer", "unit_id": "elsewhere", "task": "t", "below_tau": False, "label": "d"},
            {"unit": "aw_block", "unit_id": None, "task": "t", "below_tau": False, "label": "e"}]
    used: set = set()
    own, mix = inspector._episode_labels({"id": EP}, {"s1": {}}, labs, {"st": "s1", "elsewhere": "zz"},
                                         used)
    assert [x["label"] for x in own] == ["a", "b"] and mix == {("t", "unknown"): 1}
    assert used == {0, 1, 2}


def test_table_escapes_each_cell():
    assert inspector._table(["<h>"], [["<c>", 1]]) == (
        "<table><tr><th>&lt;h&gt;</th></tr><tr><td>&lt;c&gt;</td><td>1</td></tr></table>")


def test_labels_table_says_when_a_label_is_below_tau():
    row = {"task": "t", "unit": "u", "unit_id": "i", "label": "a", "p": None, "model_id": "m",
           "task_ver": 2, "below_tau": True}
    page = inspector._labels_table([row, {**row, "below_tau": False, "p": 0.456}])
    assert "unknown (below tau: a)" in page and "<td>0.46</td>" in page and "<td></td>" in page


def test_member_table_caps_the_rows_and_says_so():
    s = {"role": "seed", "session_id": "s" * 20, "repo_keys": [K1, None]}
    page = inspector._member_table({"member_activity": [s] * 101}, {})
    assert page.count("<tr>") == 101 and "First 100 of 101." in page and "(no local title)" in page
    info = {s["session_id"]: {"titles": ["t1", "t2", "t3", "t4"], "branches": ["b"]}}
    page = inspector._member_table({"member_activity": [{**s, "first": T0, "last": T0}]}, info)
    assert "t2; t3; t4" in page and "t1" not in page and K1[:12] in page and K1[:13] not in page


def test_anchor_items_count_every_key_and_list_only_the_shared():
    page = inspector._anchor_items({"pr key": {"shared": {"s1", "s2"}, "alone": {"s1"}}})
    assert "pr key: 2 keys, 1 shared" in page and "<code>shared</code> in s1, s2" in page
    assert "alone" not in page


def test_labels_part_without_labels_says_none_yet():
    assert "No labels yet." in inspector._labels_part({"labels": [], "steer_labels": []})
    page = inspector._labels_part({"labels": [], "steer_labels": [(("t", "<x>"), 2)]})
    assert "Steer labels: t &lt;x&gt; 2" in page and "No labels yet." not in page


def test_label_history_lists_that_unit_oldest_first():
    def row(eid, ts, unit, label):
        return (eid, ts, {"unit_id": unit, "task": "t", "label": label, "task_ver": 1})
    view = {"history": [row("b", T0 + timedelta(days=1), "u<1>", "later"), row("a", T0, "u<1>", "first"),
                        row("c", T0, "other", "not-this"), ("d", T0, None)]}
    page = inspector._label_history(view, "u<1>")
    assert "u&lt;1&gt;" in page and "not-this" not in page
    assert page.index("first") < page.index("later")


def test_pages_serve_the_evidence_and_the_correction_form():
    view = inspector.build(_raw())
    got = inspector.pages(view, {}, "Episode X")("tok")
    assert set(got) == {"", "/correct"}
    assert "<h1>Episode X</h1>" in got[""] and 'action="/tok' in got["/correct"]


def test_print_inspect_is_quiet_when_nothing_was_saved(capsys):
    inspector.print_inspect({"reviewed": 0})
    assert capsys.readouterr().out == ""
    inspector.print_inspect({"reviewed": 2})
    assert capsys.readouterr().out == "inspect: 2 corrections saved\n"
