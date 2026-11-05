"""Hermetic tests for commit detection: which shell commands and outputs count as a commit."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from agent_usage_tracker.collectors import session_digest
from agent_usage_tracker.collectors.commits import (GIT_COMMIT, commit_args_at, commit_call,
                                                    commit_dir)
from agent_usage_tracker.event import make_event_id
from test_activity import (NAME, _acts, _collect, _result, _tools, _use, _user,
                           _write_jsonl)


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


def test_segments_follow_shell_quoting() -> None:
    cases = [   # (command, the message of the commit it runs)
        ("echo 'a; git commit -m no' && git commit -m 'yes'", "yes"),
        ("cat > n.md <<'EOF'\ndon't\nEOF\ngit add n.md && git commit -m 'x'", "x"),
        ("# don't lint\ngit commit -m 'x'", "x"),
        ("echo it\\'s; git commit -m 'x'", "x"),
        ("echo don't\ngit commit -m \"x\"", "x"),
        ("git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"", "msg"),
        ('git commit -m "a; b"', "a; b"),
        # a backslash-newline joins lines: the next line is no new segment and starts no word
        ("git add a && \\\ngit commit -m 'x'", "x"),
        ("cd /r && \\\n  git commit -m 'x'", "x"),
        ("GIT_AUTHOR_DATE=x \\\n git commit -m 'x'", "x"),
        ("env A=1 \\\n git commit -m 'x'", "x"),
        ("git add a && \\\ngit commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"", "msg"),
        # a flag named in a quoted message is no flag
        ('git commit -m "add flag\n\nAdds --dry-run to the cli"', "add flag"),
        ("git -c user.name=x commit -qm 'y --dry-run'", None),
        # every heredoc delimiter form keeps an apostrophe in its body from pairing with a later quote
        ("cat > f <<'END-MSG'\ndon't\nEND-MSG\ngit add f && git commit -m 'x'", "x"),
        ("cat > f <<\\EOF\ndon't\nEOF\ngit add f && git commit -m 'x'", "x"),
        # the quoted argument of a shell -c is commands
        ("bash -c 'cd /x; git add a && git commit -m \"y\"'", "y"),
        ("uv run bash -c 'git add a && git commit -m \"y\"'", "y"),
        ('/bin/zsh -lc "cd /x\ngit add a\ngit -c user.name=n commit -q -m \'y\'"', "y"),
        ('bash -c "git commit -m \\"y\\""', "y"),
    ]
    for cmd, want in cases:
        assert commit_call(cmd) and commit_args_at(cmd) is not None, cmd
        d = session_digest.Digest("x")
        d.note_shell(cmd, "", False)
        assert d.view()["commits"] == ([want] if want else []), (cmd, d.view())
    for cmd in ("echo 'a; git commit -m no'", 'echo "x && git commit -m no"',
                "ls # it's; git commit -m no", "bash -c 'echo \"git commit -m no\"'",
                "echo 'bash -c \"git commit -m no\"'", "bash -c 'git commit --dry-run'",
                "git commit --dry-run -m 'x' # --amend", "ssh host 'git commit -m no'"):
        assert commit_call(cmd) is None and commit_args_at(cmd) is None, cmd
    assert commit_dir("cd 'a;b' && git commit -m x") == "a;b"
    assert commit_dir("bash -c 'cd /x; git commit -m y'") == "/x"
    # a cd in a shell of its own moves nothing after it
    assert commit_dir("bash -c 'cd /x && make' && git commit -m y") is None
    assert commit_call("bash -c 'cd /x && git add a && git commit -m y'") == (False, False)
    assert commit_call("bash -c 'make; git commit -m y'") == (False, True)
    # a flag only counts as a word of the command, not of its message
    assert commit_call('git commit -m "first\n\nno --amend here"') == (False, False)
    assert commit_call('git commit --amend -m "x"') == (True, False)


def test_commit_regex_is_linear() -> None:
    t = time.perf_counter()
    assert GIT_COMMIT.search("git " + "--no-pager " * 40 + "status --short") is None
    assert GIT_COMMIT.search("git --no-pager -C x commit -m y")
    assert time.perf_counter() - t < 0.5
