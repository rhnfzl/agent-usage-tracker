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
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import cli, push  # noqa: E402
from agent_usage_tracker.collectors import claude_code  # noqa: E402
from agent_usage_tracker.collectors import claude_code_activity as act  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import ActivityEvent, UsageEvent, make_event_id  # noqa: E402

TS = "2026-07-18T10:00:00Z"
NAME = claude_code.NAME
TYPED = "please tidy the parser module"
QUEUED = "and then run the checks"


def _cfg(home: Path, pg_dsn: str | None = None) -> Config:
    return Config(machine="test", agents=("claude_code",), webhook_url=None,
                  webhook_secret=None, pg_dsn=pg_dsn, home=home)


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


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


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
        got = {(e.attrs["sha"], e.attrs["branch"], e.attrs.get("amend")) for e in _acts(evs, "commit")}
        # c2 is a --dry-run, c4 failed with no SHA; c5 is the condensed "ok <sha>" form
        assert got == {("1a2b3c4", "feat/x", False), ("def5678", "feat/x", True),
                       ("0a1b2c3", "feat/x", False)}, got
        keys = {e.attrs["repo_key"] for e in _acts(evs, "commit")}
        assert keys == {_sha("example.com/owner/repo")}, keys      # creds and .git stripped
    _run(check)
    print("ok commits")


def test_repo_key_normalisation() -> None:
    k = act.repo_identity
    assert k("https://user:secret@Example.com/Owner/Repo.git") == "example.com/owner/repo"
    assert k("git@example.com:Owner/Repo.git") == "example.com/owner/repo"
    assert k("ssh://git@example.com:22/Owner/Repo/") == "example.com/owner/repo"
    assert act.repo_key("/no/such/dir/anywhere", {}) is None
    with tempfile.TemporaryDirectory() as d:
        plain = Path(d) / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        assert act.repo_key(str(plain), {}) is None                # repo without a remote
    print("ok repo_key_normalisation")


def test_pr_rows() -> None:
    def check(home, repo):
        prs = {e.attrs["number"]: e for e in _acts(_collect(home), "pr")}
        assert sorted(prs) == [3, 4, 12], prs                       # the repeat link is one row
        assert {n: e.attrs["host"] for n, e in prs.items()} == {12: "github", 3: "bitbucket",
                                                                4: "other"}
        assert prs[12].attrs["repo_key"] == _sha("github.com/acme/widget")
        assert prs[12].event_id == make_event_id(NAME, "pr", "S1", 12) and prs[12].ts == TS
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


_ID = re.compile(r"^[\w.:/@+-]{1,100}$")       # enums, ids, names, hex: no spaces, no free text


def _plain_value(v) -> bool:
    if v is None or isinstance(v, (bool, int, float)):
        return True
    if isinstance(v, str):
        return bool(_ID.match(v)) and not v.startswith(("/", "~")) and "://" not in v
    if isinstance(v, dict):
        return all(_plain_value(k) and isinstance(n, int) for k, n in v.items())
    return False


def test_attrs_carry_no_text() -> None:
    def check(home, repo):
        evs = _acts(_collect(home))
        assert {e.kind for e in evs} == {"steer", "interrupt", "rejection", "commit", "pr", "turn",
                                         "compaction", "invoke"}
        for e in evs:
            for k, v in e.attrs.items():
                assert _plain_value(v), (e.kind, k, v)
            blob = json.dumps(e.attrs)
            for leak in (TYPED, QUEUED, "tidy", "acme", "widget", "Owner", str(repo), "blocked"):
                assert leak not in blob, (e.kind, leak)
        assert not _plain_value("free text here") and not _plain_value("/abs/path")
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
    test_pg_one_transaction()
    test_activity_backfill_sends_no_usage()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
