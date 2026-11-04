"""Hermetic tests for Claude Code activity rows (agents.activity_event) on synthetic fixtures.

Run: `python tests/test_activity.py`  (or `pytest`).
Locks the rules:
  - one steer per human prompt (same uuid seed), with `via`, words, chars, after_interrupt;
  - the machine-message filters (system-reminder only, Stop hook feedback, IDE-only, /usage-only);
  - interrupt, rejection vs hook block, commit pairing, pr host + repo_key, turn aggregates with the
    retry and runaway flags, compaction, invoke;
  - attrs never carry free text;
  - Postgres delivery puts activity rows in the same transaction as usage + session rows;
  - the activity backfill sends zero usage rows and never touches the watermark.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import cli, push  # noqa: E402
from agent_usage_tracker.collectors import claude_code  # noqa: E402
from agent_usage_tracker.collectors import claude_code_activity as act  # noqa: E402
from agent_usage_tracker.collectors.activity_common import GIT_COMMIT  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import ActivityEvent, UsageEvent, make_event_id  # noqa: E402
from agent_usage_tracker.repo_keys import RepoKeys, repo_identity  # noqa: E402

TS = "2026-07-18T10:00:00Z"
NAME = claude_code.NAME
TYPED = "please tidy the parser module"
QUEUED = "and then run the checks"


SALT = "test-salt"


def _cfg(home: Path, pg_dsn: str | None = None) -> Config:
    return Config(machine="test", agents=("claude_code",), webhook_url=None,
                  webhook_secret=None, pg_dsn=pg_dsn, home=home, repo_key_salt=SALT)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _base(**kw) -> dict:
    return {"timestamp": TS, "sessionId": "S1", "entrypoint": "cli", **kw}


def _user(uid: str, content, **kw) -> dict:
    return _base(type="user", uuid=uid, promptId="P-" + uid,
                 message={"role": "user", "content": content}, **kw)


def _tools(mid: str, *blocks: dict, **kw) -> dict:
    return _base(type="assistant", uuid="a-" + mid, requestId="r-" + mid,
                 message={"id": mid, "model": "claude-x", "content": list(blocks), "usage": {
                     "input_tokens": 1, "output_tokens": 1,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}, **kw)


def _use(tid: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _result(uid: str, tid: str, text: str, err: bool = False, **kw) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if err:
        block["is_error"] = True
    return _user(uid, [block], **kw)


def _turn_end(uid: str, ms: int, n: int) -> dict:
    return _base(type="system", subtype="turn_duration", uuid=uid, durationMs=ms, messageCount=n)


def _git_repo(d: Path, remote: str) -> Path:
    repo = d / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    return repo


def _key(identity: str, salt: str = SALT) -> str:
    return hmac.new(salt.encode(), identity.encode(), hashlib.sha256).hexdigest()


def _fixture(home: Path, repo: Path) -> None:
    proj = home / ".claude/projects/-p"
    cwd = str(repo)
    _write_jsonl(proj / "S1.jsonl", [
        _base(type="queue-operation", operation="enqueue", content=QUEUED),
        _user("h1", TYPED, promptSource="typed", cwd=cwd),
        _tools("m1",
               _use("c1", "Bash", command='git commit -m "tidy"'),
               _use("c2", "Bash", command="git commit --dry-run -a"),
               _use("c3", "Bash", command="git commit --amend --no-edit"),
               _use("c4", "Bash", command='git commit -m "fails"'),
               _use("c5", "Bash", command='git commit -q -m "quiet"'),
               _use("x1", "Read", file_path="/a"),
               _use("x2", "Edit", file_path="/b"),
               _use("x3", "Skill", skill="my-skill", args="go"), cwd=cwd),
        _result("r1", "c1", "[feat/x 1a2b3c4] tidy\n 1 file changed", cwd=cwd,
                toolUseResult={"stdout": "[feat/x 1a2b3c4] tidy\n 1 file changed", "stderr": ""}),
        _result("r2", "c2", "[feat/x 9999999] would commit", cwd=cwd),
        _result("r3", "c3", "[feat/x (root-commit) def5678] tidy", cwd=cwd),
        _result("r4", "c4", "Exit code 1\nhook failed", err=True, cwd=cwd),
        _result("r5", "c5", "ok 0a1b2c3", cwd=cwd, gitBranch="feat/x"),
        _result("r6", "x1", "The user doesn't want to proceed with this tool use.", err=True),
        _result("r7", "x2", "PreToolUse:Edit hook error: blocked by policy", err=True),
        _result("r8", "x3", "Launching skill"),
        _turn_end("td1", 1234, 9),
        _user("i1", [{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
        _user("h2", "no, do it the other way", promptSource="typed"),
        # turn 2: 3 identical calls in a row, 10 Bash calls in all, 3 errors -> retry + runaway
        _tools("m2", *[_use(f"b{i}", "Bash", command="ls" if i < 3 else f"echo {i}")
                       for i in range(10)]),
        *[_result(f"rb{i}", f"b{i}", "boom" if i >= 7 else "fine", err=i >= 7)
          for i in range(10)],
        _turn_end("td2", 50, 4),
        _user("h3", "sure", promptSource="suggestion_accepted"),
        _user("h4", "<command-name>/review</command-name>\n<command-message>review</command-message>"),
        _user("h5", "<bash-input>ls</bash-input>"),
        _user("h6", "<skill>my-skill</skill>"),
        _user("h7", "typed while busy", promptSource="queued"),
        _user("h8", QUEUED),                     # no promptSource: matched to the enqueue above
        _base(type="pr-link", prNumber=12, prUrl="https://github.com/acme/widget/pull/12",
              prRepository="acme/widget"),
        _base(type="pr-link", prNumber=12, prUrl="https://github.com/acme/widget/pull/12",
              prRepository="acme/widget", timestamp="2026-07-18T11:00:00Z"),   # same PR again
        _base(type="pr-link", prNumber=3, prUrl="https://bitbucket.org/team/thing/pull-requests/3",
              prRepository="team/thing"),
        _base(type="pr-link", prNumber=4, prUrl="https://git.example.com/o/r/pull/4",
              prRepository="o/r"),
        _base(type="system", subtype="compact_boundary", uuid="cb1",
              compactMetadata={"trigger": "manual", "preTokens": 5}),
        # new machine-message filters: none of these is a human prompt
        _user("f1", "<system-reminder>be careful</system-reminder>\n"),
        _user("f2", "Stop hook feedback: tests failed"),
        _user("f3", "<ide_opened_file>The user opened a file</ide_opened_file>"),
        _user("f4", [{"type": "text", "text": "<ide_selection>x</ide_selection>"}]),
        # IDE context plus typed text IS a prompt
        _user("h9", "<ide_selection>x = 1</ide_selection> rename this"),
    ])
    # a session that only ran /usage: nobody prompted anything
    _write_jsonl(proj / "S2.jsonl", [
        _user("u1", "<command-name>/usage</command-name>\n<command-message>usage</command-message>"
                    "\n<command-args></command-args>", sessionId="S2"),
        _user("u2", "<local-command-stdout>usage table</local-command-stdout>", sessionId="S2"),
    ])
    # a subagent echo of the parent's interrupt is not a second keypress
    _write_jsonl(proj / "S1/subagents/agent-A1.jsonl", [
        _user("si1", [{"type": "text", "text": "[Request interrupted by user]"}],
              agentId="A1", isSidechain=True),
    ])


def _collect(home: Path, wm: dict | None = None) -> list:
    out = []
    for events, commit in claude_code.collect(_cfg(home), {} if wm is None else wm):
        out.extend(events)
        commit()
    return out


def _acts(evs: list, kind: str | None = None) -> list:
    return [e for e in evs if isinstance(e, ActivityEvent) and (kind is None or e.kind == kind)]


def _humans(evs: list) -> list:
    return [e for e in evs if isinstance(e, UsageEvent) and e.kind == "human"]


def _run(fn):
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        repo = _git_repo(home, "https://user:tok@Example.com/Owner/Repo.git")
        _fixture(home, repo)
        return fn(home, repo)


def test_steer_and_filters() -> None:
    def check(home, repo):
        evs = _collect(home)
        hum = _humans(evs)
        human_uids = {"h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8", "h9"}
        assert {e.event_id for e in hum} == {make_event_id(NAME, "human", u) for u in human_uids}, \
            sorted(e.event_id for e in hum)
        steers = {e.event_id: e for e in _acts(evs, "steer")}
        assert set(steers) == {make_event_id(NAME, "steer", u) for u in human_uids}   # 1:1
        via = {u: steers[make_event_id(NAME, "steer", u)].attrs["via"] for u in human_uids}
        assert via == {"h1": "typed", "h2": "typed", "h3": "suggestion_accepted",
                       "h4": "slash_command", "h5": "bash_input", "h6": "skill",
                       "h7": "queued", "h8": "queued", "h9": "typed"}, via
        s1 = steers[make_event_id(NAME, "steer", "h1")]
        assert s1.attrs["words"] == 5 and s1.attrs["chars"] == len(TYPED), s1.attrs
        assert s1.attrs["after_interrupt"] is False
        assert steers[make_event_id(NAME, "steer", "h2")].attrs["after_interrupt"] is True
        assert (s1.ts, s1.session_id, s1.turn_id) == (TS, "S1", "P-h1"), s1
    _run(check)
    print("ok steer_and_filters")


def test_interrupt_rejection_hook() -> None:
    def check(home, repo):
        evs = _collect(home)
        ints = _acts(evs, "interrupt")
        assert len(ints) == 1 and ints[0].attrs == {"during_tool": True}, ints   # subagent echo out
        rej = _acts(evs, "rejection")
        assert [e.event_id for e in rej] == [make_event_id(NAME, "rejection", "x1")], rej
        t1 = next(e for e in _acts(evs, "turn") if e.event_id == make_event_id(NAME, "turn", "td1"))
        assert t1.attrs["hook_blocks"] == 1, t1.attrs              # a hook denial, not a rejection
    _run(check)
    print("ok interrupt_rejection_hook")


def test_commits() -> None:
    def check(home, repo):
        evs = _collect(home)
        got = {(e.attrs["sha"], e.attrs.get("amend")) for e in _acts(evs, "commit")}
        # c2 is a --dry-run, c4 failed with no SHA; c5 is the condensed "ok <sha>" form
        assert got == {("1a2b3c4", False), ("def5678", True), ("0a1b2c3", False)}, got
        assert all("branch" not in e.attrs for e in _acts(evs, "commit"))  # branch names are text
        keys = {e.attrs["repo_key"] for e in _acts(evs, "commit")}
        assert keys == {_key("example.com/owner/repo")}, keys      # creds and .git stripped
    _run(check)
    print("ok commits")


def test_repo_key_normalisation() -> None:
    k = repo_identity
    assert k("https://user:secret@Example.com/Owner/Repo.git") == "example.com/owner/repo"
    assert k("git@example.com:Owner/Repo.git") == "example.com/owner/repo"
    assert k("ssh://git@example.com:22/Owner/Repo/") == "example.com/owner/repo"
    keys = RepoKeys(SALT)
    assert keys.of_cwd("/no/such/dir/anywhere") is None
    with tempfile.TemporaryDirectory() as d:
        plain = Path(d) / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        assert keys.of_cwd(str(plain)) is None                     # repo without a remote
    assert keys.of("example.com/owner/repo") == _key("example.com/owner/repo")
    assert RepoKeys("other").of("example.com/owner/repo") != _key("example.com/owner/repo")
    print("ok repo_key_normalisation")


def test_pr_rows() -> None:
    def check(home, repo):
        rows = _acts(_collect(home), "pr")
        # one row per pr-link record, so ts never depends on how much of the file a scan read;
        # distinct PRs are counted in SQL
        assert sorted(e.attrs["number"] for e in rows) == [3, 4, 12, 12], rows
        prs = {e.attrs["number"]: e for e in rows}
        assert {n: e.attrs["host"] for n, e in prs.items()} == {12: "github", 3: "bitbucket",
                                                                4: "other"}
        assert prs[12].attrs["repo_key"] == _key("github.com/acme/widget")
        twelve = sorted((e.ts, e.event_id) for e in rows if e.attrs["number"] == 12)
        assert [t for t, _ in twelve] == [TS, "2026-07-18T11:00:00Z"], twelve
        assert twelve[0][1] == make_event_id(NAME, "pr", f"S1|12|{TS}"), twelve
    _run(check)
    print("ok pr_rows")


def test_turn_aggregates() -> None:
    def check(home, repo):
        turns = {e.event_id: e.attrs for e in _acts(_collect(home), "turn")}
        t1, t2 = turns[make_event_id(NAME, "turn", "td1")], turns[make_event_id(NAME, "turn", "td2")]
        assert t1 == {"duration_ms": 1234, "message_count": 9, "tool_calls": 8, "tool_errors": 3,
                      "by_tool": {"Bash": 5, "Read": 1, "Edit": 1, "Skill": 1}, "hook_blocks": 1,
                      "max_identical_run": 1, "retry_flag": False, "runaway_flag": False}, t1
        assert (t2["tool_calls"], t2["tool_errors"], t2["max_identical_run"]) == (10, 3, 3), t2
        assert t2["retry_flag"] is True and t2["runaway_flag"] is True, t2
        assert len(turns) == 2
    _run(check)
    print("ok turn_aggregates")


def test_compaction_and_invoke() -> None:
    def check(home, repo):
        evs = _collect(home)
        assert [e.attrs for e in _acts(evs, "compaction")] == [{"trigger": "manual"}]
        inv = _acts(evs, "invoke")
        assert [(e.event_id, e.attrs) for e in inv] == [
            (make_event_id(NAME, "invoke", "x3"), {"skill": "my-skill"})], inv
    _run(check)
    print("ok compaction_and_invoke")


# attrs allowlist: every string value is a fixed enum, lowercase hex or a uuid; dicts map known
# tool names to ints. Nothing with a slash, an @ or a space can pass.
_ENUMS = {
    "via": {"typed", "suggestion_accepted", "queued", "slash_command", "bash_input", "skill"},
    "host": {"github", "bitbucket", "other"},
    "trigger": {"manual", "auto"},
    "skill": {"my-skill", "mcp"},              # the fixture's own skill names
    "status": {"finished", "cancelled", "error", "queued", "running"},
    "run_origin": {"interactive", "subagent", "automated"},
    "role": {"seed", "child"},
}
_TOOLS = {"Bash", "Read", "Edit", "Write", "Skill", "Agent", "Task", "WebSearch", "WebFetch",
          "Glob", "Grep", "mcp", "exec_command", "bash", "read", "Shell", "SwitchMode",
          "Subagent", "other"}
_HEX = re.compile(r"^[0-9a-f]{7,64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _plain_value(key: str, v) -> bool:
    if v is None or isinstance(v, (bool, int, float)):
        return True
    if isinstance(v, str):
        ok = v in _ENUMS.get(key, ()) or _HEX.match(v) or _UUID.match(v)
        return bool(ok) and not any(c in v for c in "/@ ")
    if isinstance(v, list):          # the link row's key lists: lowercase hex only
        return all(isinstance(x, str) and _HEX.match(x) for x in v)
    if isinstance(v, dict):
        return all(t in _TOOLS and isinstance(n, int) and not isinstance(n, bool)
                   for t, n in v.items())
    return False


def _adversarial(home: Path, repo: Path) -> None:
    """Text that must never reach attrs: a ticket-and-client branch, a relative path, an MCP server
    label, a commit subject."""
    cwd, br = str(repo), "feature/ABC-1-clientname-import"
    _write_jsonl(home / ".claude/projects/-p/S3.jsonl", [
        _user("v1", "fix it", sessionId="S3", cwd=cwd, gitBranch=br),
        _tools("mv",
               _use("v2", "Read", file_path="src/pkg/clientname_module.py"),
               _use("v3", "mcp__acmecorp_internal__authenticate", token="x"),
               _use("v4", "Bash", command='git commit -m "fix clientname import"'),
               _use("v5", "Skill", skill="mcp__acmecorp_internal__lookup"),
               sessionId="S3", cwd=cwd, gitBranch=br),
        _result("w2", "v2", "contents", sessionId="S3", cwd=cwd),
        _result("w3", "v3", "ok", sessionId="S3", cwd=cwd),
        _result("w4", "v4", f"[{br} abc1234] fix clientname import\n 1 file changed",
                sessionId="S3", cwd=cwd, gitBranch=br),
        _result("w5", "v5", "done", sessionId="S3", cwd=cwd),
        _turn_end("tv", 5, 4) | {"sessionId": "S3"},
    ])


def test_attrs_carry_no_text() -> None:
    def check(home, repo):
        _adversarial(home, repo)
        evs = _acts(_collect(home))
        assert {e.kind for e in evs} == {"steer", "interrupt", "rejection", "commit", "pr", "turn",
                                         "compaction", "invoke", "link"}
        for e in evs:
            for k, v in e.attrs.items():
                assert _plain_value(k, v), (e.kind, k, v)
            blob = json.dumps(e.attrs)
            for leak in (TYPED, QUEUED, "tidy", "acme", "widget", "Owner", str(repo), "blocked",
                         "ABC-1", "clientname", "acmecorp", "src/pkg", "feature", "feat/x"):
                assert leak not in blob, (e.kind, leak)
        [tv] = [e for e in evs if e.event_id == make_event_id(NAME, "turn", "tv")]
        assert tv.attrs["by_tool"] == {"Read": 1, "mcp": 1, "Bash": 1, "Skill": 1}, tv.attrs
        assert [e.attrs["sha"] for e in evs if e.kind == "commit" and e.session_id == "S3"] == \
            ["abc1234"]
        assert {e.attrs["skill"] for e in evs if e.kind == "invoke"} == {"my-skill", "mcp"}
        for bad in ("free text here", "/abs/path", "src/pkg/m.py", "user@corp.com", "a-b-c"):
            assert not _plain_value("skill", bad) and not _plain_value("sha", bad), bad
        assert not _plain_value("by_tool", {"mcp__corp__x": 1})
        assert not _plain_value("sha_refs", ["ABC-1"]) and not _plain_value("role", "lead")
        [lv] = [e for e in evs if e.kind == "link" and e.session_id == "S3"]
        assert lv.attrs["ticket_keys"] == [_key("ABC-1")] and lv.attrs["branch_key"], lv.attrs
        # a quoted or printed SHA reaches the link row only as a keyed hash, never as raw hex
        assert lv.attrs["commit_keys"] and lv.attrs["sha_refs"], lv.attrs
        for k in ("sha_refs", "commit_keys"):
            assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in lv.attrs[k]), (k, lv.attrs)
        assert "abc1234" not in json.dumps(lv.attrs), lv.attrs
    _run(check)
    print("ok attrs_carry_no_text")


def test_turn_and_commit_carry_across_scans() -> None:
    # The scan boundary falls mid-turn and between a commit's tool_use and its result.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/-p/S1.jsonl"
        _write_jsonl(f, [
            _user("h1", "go", promptSource="typed"),
            _tools("m1", _use("c1", "Bash", command="git commit -m x"), _use("x1", "Read")),
            _result("r0", "x1", "nope", err=True),
        ])
        wm: dict = {}
        first = _collect(home, wm)
        assert not _acts(first, "turn") and not _acts(first, "commit")
        with f.open("a") as fh:
            for r in (_result("r1", "c1", "[main 1a2b3c4] x"), _turn_end("td1", 9, 3)):
                fh.write(json.dumps(r) + "\n")
        second = _collect(home, wm)
        [t] = _acts(second, "turn")
        assert (t.attrs["tool_calls"], t.attrs["tool_errors"], t.attrs["by_tool"]) == \
            (2, 1, {"Bash": 1, "Read": 1}), t.attrs
        assert [e.attrs["sha"] for e in _acts(second, "commit")] == ["1a2b3c4"]
        assert "act" not in next(iter(wm["files"].values())), "nothing left to carry"
    print("ok turn_and_commit_carry_across_scans")


def test_commit_detection() -> None:
    cases = [   # (command, output, sha expected or None)
        ("git merge --no-ff --no-commit topic", "abc1234 subject", None),
        ("git cat-file commit HEAD", "2222222 tree", None),
        ("git rev-parse HEAD^{commit}", "bbbbbbb", None),
        ("git verify-commit HEAD", "ccccccc", None),
        ('git log --oneline -5\necho "since last commit"', "8888888 subject", None),
        ('git log --oneline -3\ngit commit -m "x"', "3333333 old subject\nok 4444444", "4444444"),
        ('git commit -m "x" && git push', "ok 5555555\nok main", "5555555"),
        ("git log --oneline -1 && git commit -m x", "6666666 old subject", None),
        ('git -C /some/dir -c user.name=x commit -m "y"', "ok 7777777", "7777777"),
        ("git commit --amend -m x && git log -1", "[main 9999999] x\nok aaaaaaa", "9999999"),
        ("git commit -m x", "ddddddd subject", "ddddddd"),       # single segment: first line
        # segments before the commit that print nothing: the first line is still the commit's own
        ("cd /some/dir && git add -A && git commit -m x && git push", "eeeeeee subject\nok main",
         "eeeeeee"),
        ('set -e\nexport X=1\ngit -C d add f\ngit commit -m "$(cat <<\'EOF\'\nx\nEOF\n)"',
         "fffffff subject", "fffffff"),
        # a wrapper before git, and a -c value with a space in quotes
        ('/usr/bin/env git -c user.name="A B" commit -m x', "[main 1234567] x", "1234567"),
        ("GIT_EDITOR=true git commit --amend", "[main 2345678] x", "2345678"),
        ("echo git commit", "3456789 subject", None),
        # a heredoc body is data, not commands: neither its lines nor a "git commit" inside count
        ('D="/a b/"\ncat > f.txt <<\'EOF\'\nhello\ngit commit -m y\nEOF\nmkdir -p d && git commit -m x',
         "4567890 subject", "4567890"),
        ("cat > f.sh <<'EOF'\ngit commit -m y\nEOF", "5678901 subject", None),
        ("uv run pytest -q && git commit -m x", "6789012 passed\n7890123 subject", None),
    ]
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        recs = [_user("h1", "go")]
        recs += [_tools(f"m{i}", _use(f"k{i}", "Bash", command=c)) for i, (c, _o, _s) in
                 enumerate(cases)]
        recs += [_result(f"r{i}", f"k{i}", o) for i, (_c, o, _s) in enumerate(cases)]
        _write_jsonl(home / ".claude/projects/-p/S1.jsonl", recs)
        got = {e.event_id: e.attrs["sha"] for e in _acts(_collect(home), "commit")}
        want = {make_event_id(NAME, "commit", f"k{i}|{s}"): s for i, (_c, _o, s) in
                enumerate(cases) if s}
        assert got == want, (got, want)
    print("ok commit_detection")


def test_failed_delivery_rescan_matches_clean_pass() -> None:
    # The sink is down for one scan; the state saved after it must not already hold that scan's
    # turn counts or popped commit calls.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/-p/S1.jsonl"
        _write_jsonl(f, [
            _user("h1", "go", promptSource="typed"),
            _tools("m1", _use("c1", "Bash", command="git commit -m x"), _use("x1", "Read")),
            _result("r0", "x1", "nope", err=True),
        ])
        sent: list = []
        prev = os.environ.get("AUT_STATE")
        os.environ["AUT_STATE"] = str(home / "state" / "state.json")

        def scan(fail: bool) -> None:
            def sender(batch):
                if fail:
                    raise RuntimeError("sink down")
                sent.extend(batch)
            saved = cli.make_sender
            cli.make_sender = lambda c: sender
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.run_scan(_cfg(home), ["claude_code"], dry_run=False)
                assert not fail
            except RuntimeError:
                assert fail
            finally:
                cli.make_sender = saved
        try:
            scan(False)
            with f.open("a") as fh:
                for r in (_result("r1", "c1", "[main 1a2b3c4] x"),
                          _tools("m2", _use("x2", "Bash", command="ls")),
                          _result("r2", "x2", "fine"), _turn_end("td1", 9, 3)):
                    fh.write(json.dumps(r) + "\n")
            scan(True)
            scan(False)
        finally:
            if prev is None:
                os.environ.pop("AUT_STATE", None)
            else:
                os.environ["AUT_STATE"] = prev
        clean = _collect(home)

        def view(evs):
            return sorted((e.kind, e.event_id, e.ts, json.dumps(e.attrs, sort_keys=True))
                          for e in _acts(evs) if e.kind in ("turn", "commit"))
        assert view(sent) == view(clean) and len(view(clean)) == 2, (view(sent), view(clean))
    print("ok failed_delivery_rescan_matches_clean_pass")


def test_activity_errors_never_block_usage() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/-p/S1.jsonl"
        _write_jsonl(f, [
            _user("h1", "go", promptSource="typed"),
            _base(type="pr-link", prNumber=1, prUrl="https://[x/pull/1", prRepository="o/r"),
            _tools("m1", _use("x1", "Read")),
            _user("r1", [{"type": "tool_result", "tool_use_id": "x1", "is_error": True,
                          "content": [{"type": "text", "text": None}]}]),
            _turn_end("td1", 9, 3),
            _tools("m2", _use("x2", "Read")),
        ])
        wm: dict = {}
        evs = _collect(home, wm)
        assert [e.attrs["host"] for e in _acts(evs, "pr")] == ["other"]    # bad URL, still a row
        assert len(_acts(evs, "turn")) == 1
        # a raise anywhere in activity extraction is counted and skipped, never blocks the rest
        f.unlink()
        _write_jsonl(f, [_user("h1", "go", promptSource="typed"), _tools("m1", _use("x1", "Read")),
                         _turn_end("td1", 9, 3)])
        saved = act.Activity._turn
        act.Activity._turn = lambda self, o, sess: 1 / 0
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                evs = _collect(home, {})
        finally:
            act.Activity._turn = saved
        assert len(_humans(evs)) == 1 and any(e.kind == "agent" for e in evs
                                              if isinstance(e, UsageEvent)), evs
        assert len(_acts(evs, "steer")) == 1 and not _acts(evs, "turn")
        assert "1 activity record" in err.getvalue(), err.getvalue()
    print("ok activity_errors_never_block_usage")


def test_repo_key_salt() -> None:
    with tempfile.TemporaryDirectory() as d:
        prev = {k: os.environ.get(k) for k in ("AUT_STATE", "AUT_CONFIG", "AUT_TEST_SALT")}
        os.environ["AUT_STATE"] = str(Path(d) / "st" / "state.json")
        try:
            k1 = RepoKeys(None).of("example.com/owner/repo")
            salt = Path(d) / "st" / "repo_key.salt"
            assert salt.exists(), salt
            if os.name != "nt":                                 # Windows has no POSIX modes
                assert (salt.stat().st_mode & 0o777) == 0o600, oct(salt.stat().st_mode)
            assert RepoKeys(None).of("example.com/owner/repo") == k1      # reused
            assert k1 != hashlib.sha256(b"example.com/owner/repo").hexdigest()
            assert k1 != _key("example.com/owner/repo")
            cfgf = Path(d) / "config.toml"
            cfgf.write_text('repo_key_salt = "env:AUT_TEST_SALT"\n')
            cfgf.chmod(0o600)
            os.environ.update(AUT_CONFIG=str(cfgf), AUT_TEST_SALT="from-env")
            from agent_usage_tracker.config import load_config
            assert load_config().repo_key_salt == "from-env"
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        with tempfile.TemporaryDirectory() as d2:
            os.environ["AUT_STATE"] = str(Path(d2) / "state.json")
            (Path(d2) / "repo_key.salt").write_text("")        # a broken salt: NULL, not unsalted
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert RepoKeys(None).of("example.com/owner/repo") is None
            assert "too short" in err.getvalue()
    print("ok repo_key_salt")


def test_commit_regex_is_linear() -> None:
    t = time.perf_counter()
    assert GIT_COMMIT.search("git " + "--no-pager " * 40 + "status --short") is None
    assert GIT_COMMIT.search("git --no-pager -C x commit -m y")
    assert time.perf_counter() - t < 0.5
    print("ok commit_regex_is_linear")


def test_usage_filter_only_on_fresh_read() -> None:
    usage = "<command-name>/usage</command-name>\n<command-message>usage</command-message>"
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/-p/S1.jsonl"
        _write_jsonl(f, [_user("h1", "go"), _tools("m1", _use("x1", "Read"))])
        wm: dict = {}
        _collect(home, wm)
        with f.open("a") as fh:      # a later chunk with only a /usage: live keeps it
            for r in (_user("u1", usage), _user("u2", "<local-command-stdout>t</local-command-stdout>")):
                fh.write(json.dumps(r) + "\n")
        live = _collect(home, wm)
        assert [e.event_id for e in _humans(live)] == [make_event_id(NAME, "human", "u1")], live
        assert len(_acts(live, "steer")) == 1
        full = _collect(home)        # and so does a full re-read of the same file
        assert make_event_id(NAME, "human", "u1") in {e.event_id for e in _humans(full)}
        g = home / ".claude/projects/-p/S2.jsonl"
        _write_jsonl(g, [_user("u3", usage, sessionId="S2")])
        assert not [e for e in _collect(home, wm) if e.session_id == "S2"]   # fresh: dropped
    print("ok usage_filter_only_on_fresh_read")


def test_relogged_blocks_across_scans() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/-p/S1.jsonl"
        _write_jsonl(f, [
            _user("h1", "go"),
            _tools("m1", _use("x3", "Skill", skill="my-skill"), _use("x1", "Read")),
            _result("r1", "x1", "The user doesn't want to proceed with this tool use.", err=True),
        ])
        wm: dict = {}
        first = _collect(home, wm)
        later = "2026-07-18T10:00:01Z"
        with f.open("a") as fh:      # Claude Code re-logs both blocks just after the scan
            for r in (_tools("m1", _use("x3", "Skill", skill="my-skill"), _use("x1", "Read"),
                             timestamp=later),
                      _result("r1b", "x1", "The user doesn't want to proceed with this tool use.",
                              err=True, timestamp=later),
                      _turn_end("td1", 9, 3)):
                fh.write(json.dumps(r) + "\n")
        second = _collect(home, wm)
        evs = first + second
        assert len(_acts(evs, "invoke")) == 1 and len(_acts(evs, "rejection")) == 1, evs
        [t] = _acts(second, "turn")
        assert (t.attrs["tool_calls"], t.attrs["tool_errors"]) == (2, 1), t.attrs
        assert "act" not in wm["files"][str(f)], wm["files"][str(f)]     # reset at turn end
        # bounded while a turn stays open
        with f.open("a") as fh:
            fh.write(json.dumps(_tools("m9", *[_use(f"z{i}", "Read") for i in range(250)])) + "\n")
        _collect(home, wm)
        assert len(wm["files"][str(f)]["act"]["seen"]) == act._SEEN_MAX
    print("ok relogged_blocks_across_scans")


def test_relog_after_turn_end_counts_once() -> None:
    # Claude Code re-logs earlier blocks after a turn ends; within one read they must not count
    # again in the next turn.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/-p/S1.jsonl", [
            _user("h1", "go"), _tools("m1", _use("x1", "Read"), _use("x2", "Bash")),
            _turn_end("td1", 9, 3),
            _user("h2", "next"), _tools("m1", _use("x1", "Read"), _use("x2", "Bash")),
            _tools("m2", _use("x9", "Edit")), _turn_end("td2", 9, 3),
        ])
        t1, t2 = _acts(_collect(home), "turn")
        assert (t1.attrs["tool_calls"], t2.attrs["tool_calls"]) == (2, 1), (t1.attrs, t2.attrs)
    print("ok relog_after_turn_end_counts_once")


def test_subagent_keeps_no_agg() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/-p/S1/subagents/agent-A1.jsonl"
        _write_jsonl(f, [_user("s1", "go", agentId="A1", isSidechain=True),
                         _tools("m1", _use("x1", "Read"), agentId="A1")])
        wm: dict = {}
        _collect(home, wm)
        carried = wm["files"][str(f)].get("act", {})
        assert "agg" not in carried and "seen" not in carried, carried
    print("ok subagent_keeps_no_agg")


class _Cur:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("insert failed")
        self.log.append((sql, list(rows)))


@contextlib.contextmanager
def _fake_psycopg(log, fail_on=None):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur(log, fail_on)

        def commit(self):
            log.append("commit")

    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn, connect_timeout: _Conn()
    prev = sys.modules.get("psycopg")
    sys.modules["psycopg"] = fake
    try:
        yield
    finally:
        if prev is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = prev


def test_pg_one_transaction() -> None:
    def check(home, repo):
        batch = _collect(home)
        for e in batch:
            e.machine = "test"
        send = push.make_sender(_cfg(home, pg_dsn="postgresql://x"))
        log: list = []
        with _fake_psycopg(log):
            send(batch)
        assert [x if x == "commit" else x[0].split(" (")[0] for x in log] == [
            "INSERT INTO agents.usage_event", "INSERT INTO agents.event_session",
            "INSERT INTO agents.activity_event", "commit"], log
        sql, rows = log[2]
        assert "ON CONFLICT (event_id, ts) DO NOTHING" in sql
        assert len(rows) == len(_acts(batch))
        assert all(json.loads(r[-1]) == e.attrs for r, e in zip(rows, _acts(batch)))
        assert len(log[0][1]) == sum(isinstance(e, UsageEvent) for e in batch)

        log = []
        with _fake_psycopg(log, fail_on="activity_event"):
            try:
                send(batch)
                raise AssertionError("a failed activity insert must raise")
            except RuntimeError as exc:
                assert "insert failed" in str(exc)
        assert "commit" not in log, log
    _run(check)
    print("ok pg_one_transaction")


def test_activity_backfill_sends_no_usage() -> None:
    def check(home, repo):
        n_acts = len({e.event_id for e in _acts(_collect(home))})
        cfg = _cfg(home, pg_dsn="postgresql://x")
        prev = os.environ.get("AUT_STATE")
        with tempfile.TemporaryDirectory() as sd:
            state = Path(sd) / "state.json"
            state.write_text('{"collectors": {"claude_code": {"files": {"keep": {"off": 7}}}}}')
            before = (state.read_text(), state.stat().st_mtime_ns)
            os.environ["AUT_STATE"] = str(state)
            log: list = []
            saved = cli.make_sender
            cli.make_sender = lambda c: (_ for _ in ()).throw(AssertionError("no usage sender"))
            try:
                with _fake_psycopg(log), contextlib.redirect_stderr(io.StringIO()):
                    counts = cli.run_session_backfill(cfg, ["claude_code"], dry_run=False,
                                                      sessions=False, activity=True)
                    dry = cli.run_session_backfill(cfg, ["claude_code"], dry_run=True,
                                                   sessions=False, activity=True)
            finally:
                cli.make_sender = saved
                if prev is None:
                    os.environ.pop("AUT_STATE", None)
                else:
                    os.environ["AUT_STATE"] = prev
            assert (state.read_text(), state.stat().st_mtime_ns) == before, "state untouched"
        inserts = [x for x in log if x != "commit"]
        assert inserts and all("agents.activity_event" in sql for sql, _ in inserts), log
        assert sum(len(r) for _, r in inserts) == n_acts
        assert counts == dry and dry["activity"] == n_acts and dry["rows"] == 0, dry
        assert dry["by_kind"]["steer"] == 9 and dry["via"]["queued"] == 2, dry
    _run(check)
    print("ok activity_backfill_sends_no_usage")


def main() -> int:
    test_steer_and_filters()
    test_interrupt_rejection_hook()
    test_commits()
    test_repo_key_normalisation()
    test_pr_rows()
    test_turn_aggregates()
    test_compaction_and_invoke()
    test_attrs_carry_no_text()
    test_turn_and_commit_carry_across_scans()
    test_commit_detection()
    test_failed_delivery_rescan_matches_clean_pass()
    test_activity_errors_never_block_usage()
    test_repo_key_salt()
    test_usage_filter_only_on_fresh_read()
    test_relogged_blocks_across_scans()
    test_relog_after_turn_end_counts_once()
    test_commit_regex_is_linear()
    test_subagent_keeps_no_agg()
    test_pg_one_transaction()
    test_activity_backfill_sends_no_usage()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
