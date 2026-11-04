"""Hermetic tests for the link rows (agents.activity_event kind `link`) on synthetic fixtures.

Run: `python tests/test_links.py`  (or `pytest`).
Locks the rules:
  - one link row per closed turn: repo_key, branch_key, ticket_keys, sha_refs, commit_keys,
    pr_keys, role;
  - the source order for repo and branch (commit line and its directory, worktree, session meta,
    record) and the default-branch exclusion;
  - the strict ticket pattern, the sha_refs filter, the PR URL forms, the caps;
  - a branch_key never pairs one repo with another repo's branch;
  - commit rows find their repo from the commit's directory, the cwd, then the worktree;
  - a subagent or scripted file closes its link turn once complete and idle, an hour-long gap
    between records closes it too, and a scan split anywhere gives the same rows as one full read;
  - a Codex watermark from before link fields existed still gets the session's repo and branch;
  - nothing but keys, enums and ints reaches link attrs (no raw SHA: sha_refs and commit_keys are
    keyed).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_activity import (SALT, _base, _collect, _git_repo, _key, _plain_value,  # noqa: E402
                           _result, _tools, _turn_end, _use, _user, _write_jsonl)

from agent_usage_tracker.collectors import activity_common as ac  # noqa: E402
from agent_usage_tracker.collectors import commits  # noqa: E402
from agent_usage_tracker.repo_keys import RepoKeys  # noqa: E402
from agent_usage_tracker.collectors import codex, pi  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import ActivityEvent  # noqa: E402

OLD = 1_600_000_000            # an mtime long past: the file is idle
R1, R2 = "https://example.com/owner/one.git", "git@example.com:owner/two.git"
K1, K2 = _key("example.com/owner/one"), _key("example.com/owner/two")
SHA_A, SHA_B = "1a2b3c4d", "5e6f7a8b9c"


def _bk(repo_key: str, branch: str) -> str:
    return _key(repo_key + branch)


def _sk(sha: str) -> str:
    return _key("sha7:" + sha[:7])


def _links(evs: list, sid: str | None = None) -> list:
    return [e for e in evs if isinstance(e, ActivityEvent) and e.kind == "link"
            and (sid is None or e.session_id == sid)]


def _repos(d: Path) -> tuple[Path, Path]:
    (d / "a").mkdir()
    (d / "b").mkdir()
    return _git_repo(d / "a", R1), _git_repo(d / "b", R2)


def _keys() -> RepoKeys:
    return RepoKeys(SALT)


# ---------------------------------------------------------------- unit rules


def test_ticket_pattern() -> None:
    link = ac.Link(_keys(), {})
    for t in ("ABC-123", "feat/ABC-124-thing", "X1-5", "fix [QA-7]: x"):
        link.note_tickets(t)
    for t in ("abc-125", "A-1", "ABCDEFGHIJK-1", "UTF-8", "SHA-256", "ISO-8601", "ABC-12x",
              "xABC-12", "ABC-"):
        link.note_tickets(t)
    assert link.s["tix"] == [_key(t) for t in ("ABC-123", "ABC-124", "X1-5", "QA-7")], link.s
    capped = ac.Link(_keys(), {})
    capped.note_tickets(" ".join(f"AB-{i}" for i in range(9)))
    assert len(capped.s["tix"]) == ac.MAX_TICKETS
    print("ok ticket_pattern")


def test_sha_refs_filter() -> None:
    link = ac.Link(_keys(), {})
    keep = ["abc1234", "0123456789abcdef0123456789abcdef01234567", "deadbee5"]
    drop = ["abc123", "facade", "decade0"[:6], "deadbeef", "1234567", "a" * 41 + "1",
            "0123456789abcdef" * 4, "550e8400-e29b-41d4-a716-446655440000", "ABC1234",
            "abc1234_x", "x_abc1234", "abc1234g", "zabc1234"]
    link.note_text(" ".join(drop) + "\n" + " ".join(keep) + " abc1234")
    assert link.s["shas"] == [_sk(v) for v in keep], link.s
    many = ac.Link(_keys(), {})
    many.note_text(" ".join(f"{i:07x}a" for i in range(ac.MAX_SHAS + 10)))
    assert len(many.s["shas"]) == ac.MAX_SHAS
    # a 32- or 40-hex secret passes the SHA filter: only a keyed hash of 7 hex is kept
    secret = "9f3c1e0b7a6d5c4b3a291807f6e5d4c3"
    leak = ac.Link(_keys(), {})
    leak.note_text(f'webhook_secret = "{secret}"\nhttps://{secret}@o1.ingest.example.com/1 '
              f"tok={secret}0a1b2c3d")
    assert leak.s["shas"] == [_sk(secret)], leak.s
    assert secret[:7] not in json.dumps(leak.s), leak.s
    # a watermark saved before keying carried raw hex: the state upgrade drops it
    act = {"link": {"shas": [secret[:12], _sk(secret)]}}
    ac.drop_raw_shas(act)
    assert act["link"]["shas"] == [_sk(secret)]
    ac.drop_raw_shas(act)                               # safe to run again, and on nothing
    ac.drop_raw_shas(None)
    assert act["link"]["shas"] == [_sk(secret)]
    print("ok sha_refs_filter")


def test_junk_commit_sha_gets_no_key() -> None:
    # Claude Code's structured gitOperation sha has been '1', '2', '90': keyed, they would hard-join
    # every session that printed them
    link = ac.Link(_keys(), {})
    for junk in ("1", "2", "90", "abc123", "abc1234g", "a" * 41, ""):
        assert ac.sha_key(_keys(), junk) is None, junk
        link.note_commit(None, "", commits.commit_shas("", False, False, junk))
    assert link.s.get("cks", []) == [], link.s
    link.note_commit(None, "", commits.commit_shas("", False, False, "ABC1234"))
    assert link.s["cks"] == [_sk("abc1234")], link.s
    print("ok junk_commit_sha_gets_no_key")


def test_pr_urls() -> None:
    keys = _keys()
    link = ac.Link(keys, {})
    link.note_text("see https://bitbucket.org/Team/Thing/pull-requests/42/overview and "
              "https://github.com/Owner/Widget/pull/7#issuecomment-1, again "
              "https://github.com/owner/widget/pull/7 and not https://github.com/o/r/issues/8 "
              "nor https://bitbucket.org/t/r/pull/9 nor https://example.com/o/r/pull/3")
    want = [_key("bitbucket.org/team/thing|42"), _key("github.com/owner/widget|7")]
    assert link.s["prs"] == want, link.s
    link.note_pr("github.com", "owner/widget", 7)           # a pr-link record of the same PR
    assert link.s["prs"] == want
    many = ac.Link(keys, {})
    many.note_text(" ".join(f"https://github.com/o/r/pull/{i}" for i in range(15)))
    assert len(many.s["prs"]) == ac.MAX_PRS
    print("ok pr_urls")


def test_commit_dir() -> None:
    cases = [
        ("git commit -m x", None),
        ("cd /w/one && git commit -m x", "/w/one"),
        ("cd sub && git add -A && git commit -m x", "sub"),
        ("git -C /w/two commit -m x", "/w/two"),
        ("cd /w && git -C two commit -m x", "/w/two"),
        ('git -C "/w/a b" -c user.name=x commit -m y', "/w/a b"),
        ("git -C /w/x add f && git commit -m y", None),        # a -C moves only its own command
        ("git -C /w/x commit --dry-run && git commit -m y", None),
        ("cd $DIR && git commit -m x", None),
        ("(cd /w/one && git commit -m x)", "/w/one"),
        ("ls", None),
    ]
    for cmd, want in cases:                                      # Windows joins with "\\"
        got = commits.commit_dir(cmd)
        assert (got and os.path.normpath(got)) == (want and os.path.normpath(want)), (cmd, got)
    print("ok commit_dir")


def test_default_branches_get_no_key() -> None:
    keys = _keys()
    for b in ("main", "master", "develop", "trunk", "HEAD", "", None):
        assert ac.branch_key(keys, K1, b) is None, b
    assert ac.branch_key(keys, None, "feat/x") is None            # no repo, no branch key
    assert ac.branch_key(keys, K1, "feat/x") == _bk(K1, "feat/x")
    assert ac.branch_key(keys, K1, "feat/x") != ac.branch_key(keys, K2, "feat/x")
    print("ok default_branches_get_no_key")


def test_branch_never_crosses_repos() -> None:
    # the branch_key comes from the rank that gave the repo_key, or a lower one of the same repo
    keys = _keys()

    def attrs(rk, out):
        link = ac.Link(keys, {})
        link.place(3, K1, "feat/x")                    # the record: repo one on feat/x
        link.note_commit(rk, out, ["abc1234"])
        return link.attrs("interactive")["repo_key"], link.attrs("interactive")["branch_key"]
    assert attrs(K2, "ok abc1234") == (K2, None)                  # no branch line: not one's
    assert attrs(K2, "[feat/y abc1234] x") == (K2, _bk(K2, "feat/y"))
    assert attrs(K1, "[main abc1234] x") == (K1, None)    # a default branch stops the fallback
    assert attrs(K1, "ok abc1234") == (K1, _bk(K1, "feat/x"))     # same repo: the record's
    assert attrs(None, "[feat/y abc1234] x") == (K1, _bk(K1, "feat/x"))
    old = ac.Link(keys, {"at": {"0": [K2, None], "3": [K1, _bk(K1, "feat/x")]}})  # older state
    assert (old.attrs(None)["repo_key"], old.attrs(None)["branch_key"]) == (K2, None)
    print("ok branch_never_crosses_repos")


def test_commit_repo_order() -> None:
    # the command's own directory, then the record's cwd, then the worktree's original checkout
    with tempfile.TemporaryDirectory() as d:
        one, two = _repos(Path(d))
        keys = _keys()
        assert commits.commit_repo(keys, str(one), str(two), None) == K2
        assert commits.commit_repo(keys, str(two), None, K1) == K2         # cwd resolves: cwd wins
        assert commits.commit_repo(keys, "/gone/wt", None, K1) == K1       # else the worktree
        assert commits.commit_repo(keys, "/gone/wt", "/gone/x", None) is None
        # with no cwd a relative target means nothing (never the aut process's own directory)
        here = os.getcwd()
        try:
            os.chdir(d)
            rel = os.path.relpath(one, d)
            assert commits.commit_repo(_keys(), None, rel, None) is None
            assert commits.commit_repo(_keys(), None, str(one), None) == K1
        finally:
            os.chdir(here)
    print("ok commit_repo_order")


# ---------------------------------------------------------------- Claude Code sources


def test_link_attrs_carry_no_text() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, _two = _repos(home)
        br = "feature/ABC-1-clientname-import"
        _write_jsonl(home / ".claude/projects/-p/S1.jsonl", [
            _user("h1", "see https://github.com/acmecorp/clientname/pull/12", cwd=str(one),
                  gitBranch=br),
            _base(type="custom-title", customTitle="clientname ABC-1 import"),
            _base(type="worktree-state", worktreeSession={"originalCwd": str(one),
                                                          "worktreeBranch": br}),
            _tools("m1", _use("c1", "Bash", command=f"cd {one} && git commit -m x"),
                   cwd=str(one), gitBranch=br),
            _result("r1", "c1", f"[{br} abc1234] fix clientname import", cwd=str(one)),
            _base(type="pr-link", prNumber=12, prUrl="https://github.com/acmecorp/clientname/pull/12",
                  prRepository="acmecorp/clientname"),
            _turn_end("td1", 5, 3),
        ])
        [lk] = _links(_collect(home))
        for k, v in lk.attrs.items():
            assert _plain_value(k, v), (k, v)
        blob = json.dumps(lk.attrs)
        for leak in ("ABC-1", "clientname", "acmecorp", "feature", "github", str(one), "/"):
            assert leak not in blob, leak
        assert lk.attrs["pr_keys"] == [_key("github.com/acmecorp/clientname|12")], lk.attrs
        assert set(lk.attrs) == {"repo_key", "branch_key", "ticket_keys", "sha_refs",
                                 "commit_keys", "pr_keys", "role"}
        assert lk.attrs["commit_keys"] == [_sk("abc1234")] and "abc1234" not in blob, lk.attrs
    print("ok link_attrs_carry_no_text")


# ---------------------------------------------------------------- Codex, Pi


def _cfg(home: Path) -> Config:
    return Config(machine="test", agents=("codex", "pi"), webhook_url=None, webhook_secret=None,
                  pg_dsn=None, home=home, repo_key_salt=SALT)


def _run_mod(mod, home: Path) -> list:
    out = []
    for events, commit in mod.collect(_cfg(home), {}):
        out.extend(events)
        commit()
    return out


def test_codex_link() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, two = _repos(home)
        day = home / ".codex/sessions/2026/07/18"

        def meta(i: str, cwd: str, **git) -> dict:
            return {"timestamp": "2026-07-18T10:00:00Z", "type": "session_meta",
                    "payload": {"id": i, "cwd": cwd, "source": "cli", "git": git}}

        def ev(ts: int, **p) -> dict:
            return {"timestamp": f"2026-07-18T10:00:0{ts}Z", "type": "event_msg", "payload": p}
        _write_jsonl(day / "rollout-a.jsonl", [
            meta("T1", str(one), branch="feat/AB-5-x", repository_url=R1),
            ev(1, type="task_started", turn_id="u1"),
            ev(1, type="user_message", message=f"check {SHA_A}"),
            {"timestamp": "2026-07-18T10:00:02Z", "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "k",
                         "output": "https://github.com/o/r/pull/9"}},
            ev(3, type="item_completed", turn_id="u1", item={
                "type": "CommandExecution", "id": "x1", "cwd": str(one), "exit_code": 0,
                "command": ["bash", "-lc", f"git -C {two} commit -m y"],
                "aggregated_output": "[feat/q 7c7c7c7] y"}),
            ev(4, type="task_complete", turn_id="u1"),
            ev(5, type="task_started", turn_id="u2"),
            ev(6, type="task_complete", turn_id="u2"),
        ])
        # the cwd is gone: the remote URL gives the repo; an exec run is a child
        _write_jsonl(day / "rollout-b.jsonl", [
            {**meta("T2", "/gone/x", branch="main", repository_url=R2),
             "payload": {"id": "T2", "cwd": "/gone/x", "source": "exec",
                         "git": {"branch": "main", "repository_url": R2}}},
            ev(1, type="task_started", turn_id="v1"),
            ev(2, type="task_complete", turn_id="v1"),
        ])
        links = {(e.session_id, e.turn_id): e.attrs for e in _links(_run_mod(codex, home))}
        assert links[("T1", "u1")] == {
            "repo_key": K2, "branch_key": _bk(K2, "feat/q"), "ticket_keys": [_key("AB-5")],
            "sha_refs": [_sk(SHA_A), _sk("7c7c7c7")], "commit_keys": [_sk("7c7c7c7")],
            "pr_keys": [_key("github.com/o/r|9")],
            "role": "seed"}, links[("T1", "u1")]
        assert links[("T1", "u2")]["repo_key"] == K1
        assert links[("T1", "u2")]["branch_key"] == _bk(K1, "feat/AB-5-x")
        assert links[("T1", "u2")]["sha_refs"] == []
        assert links[("T2", "v1")] == {"repo_key": K2, "branch_key": None, "ticket_keys": [],
                                       "sha_refs": [], "commit_keys": [], "pr_keys": [],
                                       "role": "child"}
        commit = [e for e in _run_mod(codex, home) if getattr(e, "kind", "") == "commit"]
        assert [e.attrs["repo_key"] for e in commit] == [K2]
    print("ok codex_link")


def test_codex_link_from_older_watermark() -> None:
    # a watermark saved before link fields existed has meta but no meta link: rebuilt from the
    # file's first record, so later turns still carry the session's repo and branch
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, _two = _repos(home)
        f = home / ".codex/sessions/2026/07/18/rollout-a.jsonl"
        recs = [{"timestamp": "2026-07-18T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "T1", "cwd": str(one), "source": "cli",
                             "git": {"branch": "feat/x"}}},
                {"timestamp": "2026-07-18T10:00:01Z", "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": "u1"}}]
        _write_jsonl(f, recs)
        wm: dict = {}
        for _events, commit in codex.collect(_cfg(home), wm):
            commit()
        assert wm["files"][str(f)]["meta"].pop("link") is not None
        del wm["state_v"]           # state from before the link was kept has no version either
        with f.open("a") as fh:
            fh.write(json.dumps({"timestamp": "2026-07-18T10:00:02Z", "type": "event_msg",
                                 "payload": {"type": "task_complete", "turn_id": "u1"}}) + "\n")
        out = []
        for events, commit in codex.collect(_cfg(home), wm):
            out.extend(events)
            commit()
        [lk] = _links(out)
        assert (lk.attrs["repo_key"], lk.attrs["branch_key"]) == (K1, _bk(K1, "feat/x")), lk.attrs
    print("ok codex_link_from_older_watermark")


def test_pi_link() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, _two = _repos(home)

        def msg(eid: str, s: int, role: str, content, **m) -> dict:
            return {"type": "message", "id": eid, "timestamp": f"2026-07-18T10:00:0{s}Z",
                    "message": {"role": role, "content": content, **m}}
        _write_jsonl(home / ".pi/agent/sessions/-p/x.jsonl", [
            {"type": "session", "id": "PI1", "timestamp": "2026-07-18T10:00:00Z", "cwd": str(one)},
            msg("u1", 1, "user", [{"type": "text", "text": f"see {SHA_A}"}]),
            msg("a1", 2, "assistant", [{"type": "toolCall", "id": "t1", "name": "bash",
                                        "arguments": {"command": "git commit -m x"}}],
                stopReason="toolUse"),
            msg("r1", 3, "toolResult", [{"type": "text", "text": "[feat/AB-2 abcd123] x"}],
                toolCallId="t1"),
            msg("a2", 4, "assistant", [{"type": "text", "text": "done"}], stopReason="stop"),
            msg("u2", 5, "user", [{"type": "text", "text": "thanks"}]),
            msg("a3", 6, "assistant", [{"type": "text", "text": "ok"}], stopReason="stop"),
        ])
        links = {e.turn_id: e.attrs for e in _links(_run_mod(pi, home))}
        assert links["u1"] == {"repo_key": K1, "branch_key": _bk(K1, "feat/AB-2"),
                               "ticket_keys": [_key("AB-2")],
                               "sha_refs": [_sk(SHA_A), _sk("abcd123")],
                               "commit_keys": [_sk("abcd123")], "pr_keys": [], "role": "seed"}, links
        assert links["u2"]["sha_refs"] == [] and links["u2"]["repo_key"] == K1, links
    print("ok pi_link")


def test_cursor_link() -> None:
    import test_cursor_cli as tc

    def check(home, repo):
        links = {(e.session_id, e.turn_id): e.attrs for e in _links(tc._collect(home))}
        k = _key("example.com/owner/repo")
        assert links[(tc.A, "r1")] == {
            "repo_key": k, "branch_key": _bk(k, "feature/ABC-1-clientname"),
            "ticket_keys": [_key("ABC-1")], "sha_refs": [_sk("1a2b3c4")],
            "commit_keys": [_sk("1a2b3c4")], "pr_keys": [], "role": "seed"}, links[(tc.A, "r1")]
        assert links[(tc.B, "rb")]["role"] == "child", links
        assert links[(tc.A, "r3")]["repo_key"] is None, links      # no working_directory
    tc._run(check)
    print("ok cursor_link")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
