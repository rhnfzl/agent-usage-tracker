"""Hermetic tests of what Codex and Pi rows must share, on synthetic fixtures.

Locks the rules:
  - attrs never carry free text; backfill works for both and never touches the watermark;
  - both tailers reset on truncation or replacement, upgrade old state once at load, and count a
    malformed usage record.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path

from test_activity import _git_repo, _plain_value

from agent_usage_tracker import scan
from agent_usage_tracker.collectors import claude_code, codex, pi
from codex_pi_helpers import (AGAIN, SID, T, TYPED, _acts, _append, _cfg, _collect, _hdr,
                              _meta, _pa, _pi_file, _pr, _pu, _run, _tok, _usage, _write_jsonl)

LEAKS = (TYPED, AGAIN, "tidy", "clientname", "acmecorp", "ABC-1", "feature", "src/pkg",
         "hello there", "standup")


# ---------------------------------------------------------------- codex fixtures

# ---------------------------------------------------------------- pi fixtures

# ---------------------------------------------------------------- both


def test_attrs_carry_no_text() -> None:
    def check(home, repo):
        kinds = {"codex": {"steer", "interrupt", "turn", "commit", "compaction", "link"},
                 "pi": {"steer", "interrupt", "turn", "commit", "compaction", "invoke", "link"}}
        for mod in (codex, pi):
            evs = _acts(_collect(mod, home))
            assert {e.kind for e in evs} == kinds[mod.NAME], (mod.NAME, {e.kind for e in evs})
            for e in evs:
                for k, v in e.attrs.items():
                    assert _plain_value(k, v), (mod.NAME, e.kind, k, v)
                blob = json.dumps(e.attrs)
                for leak in (*LEAKS, str(repo)):
                    assert leak not in blob, (mod.NAME, e.kind, leak)
    _run(check)


def test_backfill_codex_pi(monkeypatch) -> None:
    def check(home, repo):
        with tempfile.TemporaryDirectory() as sd:
            state = Path(sd) / "state.json"
            state.write_text('{"collectors": {"codex": {"files": {"keep": {"off": 7}}}}}')
            monkeypatch.setenv("AUT_STATE", str(state))
            before = (state.read_text(), state.stat().st_mtime_ns)
            with contextlib.redirect_stderr(io.StringIO()):
                c = scan.run_history_backfill(_cfg(home), ["codex", "pi"], dry_run=True,
                                             sessions=True, activity=True)
            assert (state.read_text(), state.stat().st_mtime_ns) == before, "state untouched"
        assert c["by_origin"] == {"interactive": 6, "automated": 2, "subagent": 1,
                                  "unknown": 11}, c
        assert c["subagent_no_parent"] == 0, c
        # a link row per closed turn; Codex also closes one on turn_aborted
        assert c["by_kind"] == {"steer": 10, "interrupt": 2, "turn": 8, "commit": 2,
                                "compaction": 2, "invoke": 1, "link": 9}, c
        assert sum(r["rows"] for r in c["link"].values()) == 9 and c["link"]["seed"]["repo_key"], c
    _run(check)


def test_tailers_share_the_truncation_and_replacement_reset() -> None:
    for mod, f, recs in (
            (pi, "pi", [_hdr(SID, "/x/repo"), _pu("u1", T(1), "go"), _pa("a1", T(2), "stop", tokens=7)]),
            (codex, "codex", [_meta("C1"), _tok(T(1))])):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            path = _pi_file(home) if f == "pi" else home / ".codex/sessions/2026/07/18/rollout-t.jsonl"
            _write_jsonl(path, recs)
            wm: dict = {}
            assert len(_usage(_collect(mod, home, wm))) == 1
            wm["files"][str(path)]["off"] += 1000       # the file is now shorter than the offset
            assert len(_usage(_collect(mod, home, wm))) == 1, f
            wm["files"][str(path)]["ino"] += 1          # a different file now sits at this path
            assert len(_usage(_collect(mod, home, wm))) == 1, f
            assert _collect(mod, home, wm) == [], f     # and it resumes normally after


def test_state_upgrades_run_once_at_load() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        # Codex state from before um_ord: dropped, so the file is re-read from 0
        cf = home / ".codex/sessions/2026/07/18/rollout-u.jsonl"
        _write_jsonl(cf, [_meta("C1"), _tok(T(1))])
        wm: dict = {}
        _collect(codex, home, wm)
        del wm["files"][str(cf)]["um_ord"], wm["state_v"]
        assert len(_usage(_collect(codex, home, wm))) == 1
        assert wm["files"][str(cf)]["um_ord"] == 0 and wm["state_v"] == codex._STATE_V

        # Pi: a commit call pending in state from before its target was kept still resolves
        repo = _git_repo(home, "https://Example.com/Owner/Repo.git")
        f = _pi_file(home)
        _write_jsonl(f, [_hdr(SID, str(repo)), _pu("u1", T(1), "go"),
                         _pa("a1", T(2), "toolUse", ("k1", "bash", {"command": "git commit -m x"}),
                             tokens=1)])
        wm = {}
        _collect(pi, home, wm)
        pend = wm["files"][str(f)]["act"]["pend"]
        pend["k1"] = pend["k1"][:2]
        del wm["state_v"]
        err = io.StringIO()
        _append(f, [_pr("r1", T(3), "k1", "ok 0a1b2c3"), _pa("a2", T(4), "stop", tokens=1)])
        with contextlib.redirect_stderr(err):
            second = _collect(pi, home, wm)
        [c] = _acts(second, "commit")
        assert c.attrs["sha"] == "0a1b2c3" and c.attrs["repo_key"]
        assert "skipped" not in err.getvalue(), err.getvalue()


def test_a_version_1_watermark_upgrades_without_reading_anything_again() -> None:
    """The state on disk when _STATE_V went to 2: entries with a `dev`, link SHAs maybe raw."""
    key = "a" * 64
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        logs = (
            (claude_code, home / ".claude/projects/-x/S1.jsonl",
             [{"type": "user", "uuid": "h1", "timestamp": T(1), "sessionId": "S1",
               "message": {"role": "user", "content": "go"}}]),
            (codex, home / ".codex/sessions/2026/07/18/rollout-a.jsonl",
             [_meta("C1"), _tok(T(1))]),
            (pi, _pi_file(home), [_hdr(SID, "/x/repo"), _pu("u1", T(1), "go"),
                                  _pa("a1", T(2), "stop", tokens=7)]),
        )
        for mod, f, recs in logs:
            _write_jsonl(f, recs)
            wm: dict = {}
            assert _collect(mod, home, wm), mod.NAME
            entry = wm["files"][str(f)]
            off, ino = entry["off"], entry["ino"]
            entry["dev"] = 4
            entry.setdefault("act", {}).setdefault("link", {})["shas"] = ["abc1234def", key]
            wm["state_v"] = 1
            assert _collect(mod, home, wm) == [], mod.NAME
            entry = wm["files"][str(f)]
            assert (entry["off"], entry["ino"]) == (off, ino), mod.NAME
            assert entry["act"]["link"]["shas"] == [key], mod.NAME
            assert wm["state_v"] == mod._STATE_V == 2, mod.NAME


def test_malformed_usage_records_are_counted() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        pf = _pi_file(home)
        bad_pi = _pa("a2", T(3), "stop", tokens=1)
        bad_pi["message"]["usage"]["input"] = "many"
        _write_jsonl(pf, [_hdr(SID, "/x/repo"), _pa("a1", T(2), "stop", tokens=7), bad_pi])
        with pf.open("a") as fh:
            fh.write("{not json\n[1, 2]\n")
        cf = home / ".codex/sessions/2026/07/18/rollout-m.jsonl"
        bad_tok = _tok(T(2))
        bad_tok["payload"]["info"]["last_token_usage"]["input_tokens"] = "many"
        _write_jsonl(cf, [_meta("C1"), _tok(T(1)), bad_tok])
        with cf.open("a") as fh:
            fh.write("{not json\n")
        for mod, want in ((pi, 3), (codex, 2)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                evs = _collect(mod, home)
            assert len(_usage(evs)) == 1, mod.NAME          # the good row is untouched
            assert f"{want} malformed record(s) skipped" in err.getvalue(), err.getvalue()
