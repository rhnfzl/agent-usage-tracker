"""Hermetic tests for session_digest: the local-only titles and text read from each agent's logs.

Run: `python tests/test_session_digest.py`  (or `pytest`).
Locks the rule: titles, branches, prompts, commit subjects, PR titles and files come from this
machine's own logs, prompts trimmed and redacted, and one bad log costs only its own session.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.collectors import session_digest  # noqa: E402


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


def test_codex_digest_keeps_a_record_with_an_invalid_byte():
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / ".codex" / "sessions" / "rollout-2001-cx1.jsonl"
        path.parent.mkdir(parents=True)
        lines = [json.dumps(r).encode() for r in (
            {"type": "session_meta", "payload": {"id": "cx1"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "first @@"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "second"}})]
        path.write_bytes(b"\n".join(lines).replace(b"@@", b"\xff") + b"\n")
        v = session_digest.local_info(Path(t), {"cx1"})["cx1"]
    assert (v["n_prompts"], v["last"]) == (2, "second"), v
    assert v["first"].startswith("first "), v
    print("ok codex_digest_keeps_a_record_with_an_invalid_byte")


def test_digest_lists_hold_only_text():
    d = session_digest.Digest("x")
    d.shell("git commit --no-edit", "", False)
    d.shell("gh pr create --fill", "", False)
    assert d.commits == [] and d.prs == [], (d.commits, d.prs)
    print("ok digest_lists_hold_only_text")


def test_local_info_says_a_log_failed_without_its_text(capsys):
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        log = home / ".claude" / "projects" / "p" / "s1.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{}\n")
        with patch.object(session_digest, "_digest_claude_log",
                          side_effect=KeyError("private prompt text")):
            assert session_digest.local_info(home, {"s1"}) == {}
    err = capsys.readouterr().err
    assert "1 local session log(s) not shown (KeyError)" in err and "private" not in err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
