"""Hermetic tests for `aut episodes review` (the hand check of one month's episodes).

Run: `python tests/test_review.py`  (or `pytest`).
Locks the rules:
  - titles, branches, prompts, commit subjects, PR titles and files come from this machine's own
    logs, prompts trimmed and redacted, and are escaped on the page;
  - "don't know" is the default answer and writes nothing;
  - the page shows a sample of 30 spread over outcome and size, and links to all;
  - the page answers only on 127.0.0.1 and only under its random token;
  - each answer becomes one episode_correction row of keys only (confirm, split, merge, reject),
    no local text ever reaches a row, and an answer naming a session or episode the page did
    not show is dropped;
  - the summary is the share of reviewed episodes marked right.
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import local_page, review  # noqa: E402
from agent_usage_tracker.collectors import session_digest  # noqa: E402

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
        info = session_digest.local_info(home, {"s1", "cx1", "gone"})
        tb = ("titles", "branches")
        assert {k: info["s1"][k] for k in tb} == {"titles": ['Fix the "thing"', "named <b>"],
                                                  "branches": ["feat/one", "wt/two"]}, info
        assert {k: info["cx1"][k] for k in tb} == {"titles": ["codex thread"],
                                                   "branches": ["fix/three"]}, info
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
    # one odd merge answer drops only itself: isdigit() takes these, int() does not
    odd = {"a0": ["merge"], "merge0": ["\u00b2"], "a1": ["merge"], "merge1": ["9" * 5000],
           "a2": ["right"]}
    rows, summary = review.corrections(odd, _episodes(), T0)
    assert [r["action"] for r in rows] == ["confirm"] and summary["reviewed"] == 1, rows
    print("ok answers_become_key_only_corrections")


def test_server_serves_under_token_and_stops_on_submit():
    written: list = []
    ready = threading.Event()
    box: dict = {}

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: box.update(summary=review.serve(
        _episodes(), written.extend, review.month_pages(_episodes(), {}, "2001-01"),
        open_browser=False, on_ready=on_ready, now=T0)))
    t.start()
    assert ready.wait(10)
    url = box["url"]
    assert url.startswith("http://127.0.0.1:"), url
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.status == 200 and b"Episodes of 2001-01" in r.read()
    with urllib.request.urlopen(url + "?all=1", timeout=5) as r:
        assert r.status == 200 and b"All 4 open episodes" in r.read()
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


def test_post_with_a_bad_content_length_is_a_404_not_a_crash():
    ready = threading.Event()
    box: dict = {}

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: local_page.serve(
        "tok", lambda tail: b"page" if tail == "" else None, lambda form: b"done", "{url}",
        open_browser=False, on_ready=on_ready))
    t.start()
    assert ready.wait(10)
    parsed = urllib.parse.urlsplit(box["url"])

    def post(path, length):
        c = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        c.putrequest("POST", path)
        c.putheader("Content-Length", length)
        c.endheaders()
        code = c.getresponse().status
        c.close()
        return code
    assert post("/other", "nope") == 404
    assert post("/tok", "nope") == 404
    assert post("/tok", "-1") == 404
    with urllib.request.urlopen(box["url"], timeout=5) as r:
        assert r.read() == b"page"
    with urllib.request.urlopen(urllib.request.Request(box["url"], data=b"a=1"), timeout=5) as r:
        assert r.read() == b"done"
    t.join(10)
    assert not t.is_alive()
    print("ok post_with_a_bad_content_length_is_a_404_not_a_crash")


def test_ctrl_c_ends_the_page_quietly():
    with patch("http.server.HTTPServer.serve_forever", side_effect=KeyboardInterrupt):
        got = review.serve(_episodes(), [].extend, review.month_pages(_episodes(), {}, "2001-01"),
                           open_browser=False)
    assert got == {"reviewed": 0, "right": 0}
    print("ok ctrl_c_ends_the_page_quietly")


def test_month_bounds():
    label, start, end = review._month_bounds("2001-12")
    assert label == "2001-12" and (start.month, end.year, end.month) == (12, 2002, 1)
    label, start, end = review._month_bounds(None)
    assert end <= datetime.now().astimezone() and (end - start).days in (28, 29, 30, 31)
    print("ok month_bounds")


def _jsonl(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


SECRET = "ghp_" + "A1b2C3d4" * 5


def test_claude_session_fields():
    cwd = "/work/acme/widget"

    def rec(ts, **kw):
        return {"timestamp": f"2001-01-05T{ts}Z", "cwd": cwd, "sessionId": "s1", **kw}

    def user(ts, content, **kw):
        return rec(ts, type="user", entrypoint="cli", message={"role": "user", "content": content},
                   **kw)

    def bash(ts, tid, cmd):
        return rec(ts, type="assistant", message={"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]})

    def result(ts, tid, text, err=False):
        return user(ts, [{"type": "tool_result", "tool_use_id": tid, "content": text,
                          "is_error": err}])

    def edit(ts, tid, path):
        return rec(ts, type="assistant", message={"content": [
            {"type": "tool_use", "id": tid, "name": "Edit", "input": {"file_path": path}}]})
    long_prompt = ("<system-reminder>ignore me</system-reminder>Please fix the login bug, token="
                   + SECRET + " " + "word " * 80)
    heredoc = "git add -A && git commit -m \"$(cat <<'EOF'\nAdd the widget\n\nBody\nEOF\n)\""
    recs = [user("09:00:00", long_prompt),
            user("09:01:00", "<task-notification>done</task-notification>"),
            bash("09:02:00", "b1", heredoc), result("09:02:01", "b1", "ok abc1234"),
            bash("09:03:00", "b2", "python -m pytest && git commit -m 'Fix two'"),
            result("09:03:01", "b2", "[feat/x 1234abc] Fix two, from git"),
            bash("09:04:00", "b3", "git commit -m 'never made'"),
            result("09:04:01", "b3", "hook failed", err=True),
            bash("09:05:00", "b4", 'gh pr create --title "Widget PR" --body x'),
            result("09:05:01", "b4", "https://github.com/o/r/pull/1"),
            edit("09:06:00", "e1", cwd + "/src/a.py"), edit("09:06:01", "e2", cwd + "/src/a.py"),
            edit("09:06:01", "e2", cwd + "/src/a.py"),     # a re-logged block counts once
            edit("09:06:02", "e3", cwd + "/b.py"), edit("09:06:03", "e4", "/elsewhere/x/c.py"),
            edit("09:06:04", "e5", cwd + "/d.py"), edit("09:06:05", "e5b", cwd + "/b.py"),
            user("11:15:00", "now ship it")]
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _jsonl(home / ".claude" / "projects" / "-work-acme-widget" / "s1.jsonl", recs)
        v = session_digest.local_info(home, {"s1"})["s1"]
    assert (v["agent"], v["folder"], v["minutes"], v["n_prompts"]) == \
        ("Claude Code", "acme/widget", 135, 2), v
    assert v["start"] == datetime(2001, 1, 5, 9, tzinfo=timezone.utc)
    assert v["first"].startswith("Please fix the login bug, token=[redacted] word"), v["first"]
    assert len(v["first"]) <= 200 and SECRET not in v["first"] and "ignore me" not in v["first"]
    assert v["last"] == "now ship it"
    assert v["commits"] == ["Add the widget", "Fix two, from git"], v["commits"]
    assert v["prs"] == ["Widget PR"], v["prs"]
    assert v["files"] == ["src/a.py", "b.py", "x/c.py"], v["files"]
    print("ok claude_session_fields")


def test_codex_session_fields():
    def ev(ts, payload, typ="event_msg"):
        return {"timestamp": f"2001-01-05T{ts}Z", "type": typ, "payload": payload}

    def item(ts, it):
        return ev(ts, {"type": "item_completed", "item": it})
    meta = {"id": "cx1", "cwd": "/work/acme/gadget", "source": "cli",
            "git": {"branch": "feat/g"}}
    recs = [ev("08:00:00", meta, "session_meta"),
            ev("08:00:01", {"type": "user_message", "message": "first codex ask"}),
            item("08:10:00", {"type": "CommandExecution", "command": ["bash", "-lc",
                              "git commit -m 'Gadget works'"], "exit_code": 0,
                              "aggregated_output": "[main 7654321] Gadget works\n"}),
            item("08:11:00", {"type": "FileChange", "changes": {"/work/acme/gadget/g.py":
                                                                  {"type": "update"}}}),
            ev("08:20:00", {"type": "user_message", "message": "second ask " + SECRET})]
    auto = [ev("08:00:00", {**meta, "id": "cx2", "originator": "Claude Code"}, "session_meta"),
            ev("08:00:01", {"type": "user_message", "message": "a harness talking"})]
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        base = home / ".codex" / "sessions" / "2001" / "01" / "05"
        _jsonl(base / "rollout-2001-01-05T08-00-00-cx1.jsonl", recs)
        _jsonl(base / "rollout-2001-01-05T08-00-00-cx2.jsonl", auto)
        info = session_digest.local_info(home, {"cx1", "cx2"})
    v = info["cx1"]
    assert (v["agent"], v["folder"], v["minutes"], v["n_prompts"], v["first"]) == \
        ("Codex", "acme/gadget", 20, 2, "first codex ask"), v
    assert v["last"] == "second ask [redacted]" and v["branches"] == ["feat/g"], v
    assert (v["commits"], v["files"]) == (["Gadget works"], ["g.py"]), v
    assert info["cx2"]["n_prompts"] == 0 and info["cx2"]["first"] == ""
    print("ok codex_session_fields")


def test_dont_know_is_the_default_and_writes_nothing():
    page = review.render(_episodes(), {}, "tok", "2001-01")
    assert page.count('value="skip" checked') == 4 and page.count(" checked") == 4
    assert "not building work (admin, personal, a quick question)" in page
    rows, summary = review.corrections({f"a{i}": ["skip"] for i in range(4)}, _episodes(), T0)
    assert rows == [] and summary == {"reviewed": 0, "right": 0}
    print("ok dont_know_is_the_default_and_writes_nothing")


def test_sample_is_30_spread_over_outcome_and_size():
    outcomes = ["shipped"] * 60 + ["reworked"] * 20 + ["open"] * 15 + ["no_code"] * 5
    eps = [{"id": f"e{i}", "first_ts": T0, "last_ts": T0, "n_sessions": 1 + i % 5,
            "n_commits": 0, "n_prs": 0, "outcome": o,
            "members": [{"session_id": f"s{i}", "role": "seed", "repo_keys": []}]}
           for i, o in enumerate(outcomes)]
    got = review.sample(eps)
    assert len(got) == 30 and got == sorted(set(got)) and got == review.sample(eps)

    def size(k):
        return 0 if k <= 1 else 1 if k <= 3 else 2
    groups = {(ep["outcome"], size(ep["n_sessions"])) for ep in eps}
    assert {(eps[i]["outcome"], size(eps[i]["n_sessions"])) for i in got} == groups
    assert sum(eps[i]["outcome"] == "no_code" for i in got) == 5     # a rare outcome all shows
    assert review.sample(eps[:7]) == list(range(7))
    page = review.render(eps, {}, "tok", "2001-01", got, answered=3)
    assert page.count("<fieldset>") == 30 and 'href="/tok?all=1"' in page
    assert "3 already answered" in page
    print("ok sample_is_30_spread_over_outcome_and_size")


def test_no_local_text_reaches_the_rows():
    text = "first prompt words"
    info = {s: {"agent": "Codex", "folder": "acme/w", "start": T0, "minutes": 5, "n_prompts": 2,
                "first": text, "last": text, "commits": [text], "prs": [text], "files": [text],
                "titles": [text], "branches": [text]} for s in ("s1", "s2", "s3", "s4", "s5")}
    page = review.render(_episodes(), info, "tok", "2001-01")
    assert text in page and "Codex" in page and "1 commit" not in page
    form = {"a0": ["split"], "split0": ["s2", text], "a1": ["merge"], "merge1": ["2"],
            "a2": ["right"], "a3": ["reject"], "note": [text]}
    rows, _ = review.corrections(form, _episodes(), T0)
    assert len(rows) == 4 and text not in json.dumps(rows, default=str)
    assert all(set(r["attrs"]) <= {"repo_keys", "b_repo_keys"} for r in rows)
    print("ok no_local_text_reaches_the_rows")


def test_review_findings_edge_cases():
    d = session_digest.Digest("x")
    d.shell("gh pr create --fill && docker run -t myimage", "", False)
    d.shell("git commit -q --no-edit && python -m pytest", "", False)
    d.shell("git commit -m 'a; b' && echo -m nope", "", False)
    d.titles.append("fix token=" + SECRET)
    d.branches.append("b-" + SECRET)
    v = d.view()
    assert (v["prs"], v["commits"]) == ([], ["a; b"]), v
    assert SECRET not in json.dumps(v), v
    assert session_digest.clip(session_digest._BLOCK.sub(" ", "<user_info>OS Version: darwin 25</user_info> fix it")) \
        == "fix it"
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        sid = "0b1c2d3e-0000-4000-8000-000000000001"
        _jsonl(home / ".pi" / "agent" / "sessions" / "-p" / f"2001_{sid}.jsonl", [
            {"type": "session", "id": sid, "timestamp": "2001-01-05T09:00:00Z", "cwd": "/w/p"},
            {"type": "message", "timestamp": "2001-01-05T09:01:00Z", "message": {
                "role": "user", "content": '<skill name="tidy">long SKILL.md body</skill> now'}}])
        _jsonl(home / ".codex" / "sessions" / "2001" / "rollout-2001-cx2.jsonl", [
            {"type": "session_meta", "payload": {"id": "cx2"}}, {"payload": {}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}])
        info = session_digest.local_info(home, {sid, "cx2"})
    assert info[sid]["first"] == "/tidy now", info
    assert info["cx2"]["first"] == "hello", info
    print("ok review_findings_edge_cases")


def test_bad_values_in_one_log_cost_only_that_session():
    meta = {"type": "session_meta", "payload": {"id": "cx1", "git": {"branch": ["not", "text"]}}}
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        _jsonl(home / ".codex" / "sessions" / "2001" / "rollout-2001-cx1.jsonl", [meta])
        (home / ".codex" / "session_index.jsonl").write_text(
            '[1]\n{"id": ["x"], "thread_name": "a"}\nnot json\n'
            '{"id": "cx1", "thread_name": "named"}\n')
        _jsonl(home / ".claude" / "projects" / "-p" / "s1.jsonl",
               [{"type": "user", "gitBranch": "b1"}])
        real = session_digest.Digest.view

        def view(d):
            if d.agent == "Claude Code":
                raise TypeError("local text")
            return real(d)
        with patch.object(session_digest.Digest, "view", view):
            info = session_digest.local_info(home, {"cx1", "s1"})
    assert list(info) == ["cx1"], info
    assert (info["cx1"]["branches"], info["cx1"]["titles"]) == ([], ["named"]), info
    print("ok bad_values_in_one_log_cost_only_that_session")


def test_codex_fork_skips_the_copied_history():
    def um(text):
        return {"type": "event_msg", "payload": {"type": "user_message", "message": text}}
    recs = [{"type": "session_meta", "payload": {"id": "cx1", "forked_from_id": "p",
                                                 "subagent_history_start_ordinal": 3}},
            um("parent's"), um("parent's too"), um("mine")]
    with tempfile.TemporaryDirectory() as t:
        _jsonl(Path(t) / ".codex" / "sessions" / "rollout-2001-cx1.jsonl", recs)
        v = session_digest.local_info(Path(t), {"cx1"})["cx1"]
    assert (v["n_prompts"], v["first"]) == (1, "mine"), v
    print("ok codex_fork_skips_the_copied_history")


def test_digest_lists_hold_only_text():
    d = session_digest.Digest("x")
    d.shell("git commit --no-edit", "", False)
    d.shell("gh pr create --fill", "", False)
    assert d.commits == [] and d.prs == [], (d.commits, d.prs)
    print("ok digest_lists_hold_only_text")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
