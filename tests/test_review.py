"""Hermetic tests for `aut episodes review` (the hand check of one month's episodes).

Run: `python tests/test_review.py`  (or `pytest`).
Locks the rules:
  - titles and branch names come from this machine's own logs and are escaped on the page;
  - the page answers only on 127.0.0.1 and only under its random token;
  - each answer becomes one episode_correction row of keys only (confirm, split, merge, reject),
    and an answer naming a session or episode the page did not show is dropped;
  - the summary is the share of reviewed episodes marked right.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import review  # noqa: E402

K1, K2 = "a1" * 32, "b2" * 32
T0 = datetime(2001, 1, 5, 9, tzinfo=timezone.utc)


def _episodes() -> list[dict]:
    def ep(eid, members):
        return {"id": eid, "first_ts": T0, "last_ts": T0, "n_sessions": len(members),
                "n_seed_sessions": 1, "n_commits": 2, "n_prs": 0, "outcome": "shipped",
                "members": [{"session_id": s, "role": r, "repo_keys": k} for s, r, k in members]}
    return [ep("e1", [("s1", "seed", [K1]), ("s2", "seed", [K1, K2]), ("c1", "child", [K1])]),
            ep("e2", [("s3", "seed", [K2])]),
            ep("e3", [("s4", "seed", [K1])]),
            ep("e4", [("s5", "seed", [K2])])]


def test_local_info_reads_claude_and_codex_logs():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        proj = home / ".claude" / "projects" / "-p"
        proj.mkdir(parents=True)
        recs = [{"type": "user", "gitBranch": "feat/one", "sessionId": "s1"},
                {"type": "ai-title", "aiTitle": "Fix the \"thing\""},
                {"type": "worktree-state", "worktreeSession": {"worktreeBranch": "wt/two"}},
                {"type": "custom-title", "customTitle": "named <b>"},
                {"type": "user", "gitBranch": "feat/one"}]
        (proj / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        sub = proj / "s1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-x.jsonl").write_text(json.dumps({"aiTitle": "not me"}) + "\n")
        cx = home / ".codex" / "sessions" / "2001" / "01" / "05"
        cx.mkdir(parents=True)
        (cx / "rollout-2001-01-05T09-00-00-cx1.jsonl").write_text(json.dumps(
            {"type": "session_meta", "payload": {"id": "cx1", "git": {"branch": "fix/three"}}})
            + "\n")
        (home / ".codex" / "session_index.jsonl").write_text(
            json.dumps({"id": "cx1", "thread_name": "codex thread"}) + "\n")
        info = review.local_info(home, {"s1", "cx1", "gone"})
        assert info["s1"] == {"titles": ['Fix the "thing"', "named <b>"],
                              "branches": ["feat/one", "wt/two"]}, info
        assert info["cx1"] == {"titles": ["codex thread"], "branches": ["fix/three"]}, info
        assert "gone" not in info
        print("ok local_info_reads_claude_and_codex_logs")


def test_page_escapes_local_text():
    page = review.render(_episodes(), {"s1": {"titles": ["<script>x</script>"],
                                              "branches": ["a&b"]}}, "tok", "2001-01")
    assert "<script>x" not in page and "&lt;script&gt;x" in page and "a&amp;b" in page
    assert 'action="/tok"' in page
    print("ok page_escapes_local_text")


def test_answers_become_key_only_corrections():
    form = {"a0": ["split"], "split0": ["s2", "nope"], "a1": ["merge"], "merge1": ["2"],
            "a2": ["right"], "a3": ["reject"]}
    rows, summary = review.corrections(form, _episodes(), T0)
    got = [(r["action"], r["a_session"], r["b_session"], r["attrs"]) for r in rows]
    assert got == [("split", "s2", None, {"repo_keys": [K1, K2]}),
                   ("merge", "s3", "s4", {"repo_keys": [K2], "b_repo_keys": [K1]}),
                   ("confirm", "s4", None, {"repo_keys": [K1]}),
                   ("reject", "s5", None, {"repo_keys": [K2]})], got
    assert all(len(r["id"]) == 36 and r["ts"] == T0 for r in rows)
    assert summary == {"reviewed": 4, "right": 1}, summary
    bad = {"a0": ["merge"], "merge0": ["0"], "a1": ["merge"], "merge1": ["99"],
           "a2": ["split"], "a3": ["bogus"]}
    rows, summary = review.corrections(bad, _episodes(), T0)
    assert rows == [] and summary == {"reviewed": 0, "right": 0}, (rows, summary)
    print("ok answers_become_key_only_corrections")


def test_server_serves_under_token_and_stops_on_submit():
    written: list = []
    ready = threading.Event()
    box: dict = {}

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: box.update(summary=review.serve(
        _episodes(), {}, "2001-01", written.extend, open_browser=False, on_ready=on_ready,
        now=T0)))
    t.start()
    assert ready.wait(10)
    url = box["url"]
    assert url.startswith("http://127.0.0.1:"), url
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.status == 200 and b"Episodes of 2001-01" in r.read()
    base = url.rsplit("/", 1)[0]
    for wrong in (base + "/", base + "/other"):
        try:
            urllib.request.urlopen(wrong, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("served without the token")
    body = urllib.parse.urlencode({"a2": "right", "a3": "reject"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/other", data=body), timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5) as r:
        assert r.status == 200 and b"1 of 2" in r.read()
    t.join(10)
    assert not t.is_alive()
    assert [w["action"] for w in written] == ["confirm", "reject"], written
    assert box["summary"] == {"reviewed": 2, "right": 1}
    print("ok server_serves_under_token_and_stops_on_submit")


def test_month_bounds():
    label, start, end = review._month_bounds("2001-12")
    assert label == "2001-12" and (start.month, end.year, end.month) == (12, 2002, 1)
    label, start, end = review._month_bounds(None)
    assert end <= datetime.now().astimezone() and (end - start).days in (28, 29, 30, 31)
    print("ok month_bounds")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
