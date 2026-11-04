"""Hermetic tests for outcome_git: what a clone says, and that reading one never runs its config.

Locks the rules:
  - a fetch that fails or times out is skipped and counted, never runs gc and is terminated on
    timeout;
  - a clone's own config, attributes and hooks never make git run a command (diff.external, a
    textconv driver, core.fsmonitor, a hook, remote uploadpack);
  - a failed git read is told apart from an empty answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_usage_tracker import outcome_git as og
from agent_usage_tracker import outcomes as oc
from agent_usage_tracker.git_safe import git_argv
from agent_usage_tracker.git_safe import git_env
from agent_usage_tracker.repo_keys import RepoKeys
from test_outcomes import (NOW, SALT, _commit, _g, _key, _lines, _row, _states, _with_git,
                           _world)


def test_fetch_failure_and_timeout_are_counted():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        bad = Path(d) / "projects" / "bad"
        bad.mkdir()
        _g(bad, "init", "-q")
        _g(bad, "remote", "add", "origin", str(Path(d) / "missing.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert len(clones) == 2
        assert oc.fetch_clones(clones) == (1, 1)
        assert sum(c.fetched_at > 0 for c in clones) == 1 and og.FETCH_TIMEOUT_S == 30


def test_find_clones_dedups_worktrees_and_skips_vendored():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        _g(work, "worktree", "add", "-q", str(Path(d) / "projects" / "wt"), "-b", "wt-branch")
        nm = Path(d) / "projects" / "node_modules" / "dep"
        nm.mkdir(parents=True)
        _g(nm, "init", "-q")
        clones = oc.find_clones([str(Path(d) / "projects"), str(Path(d) / "nope")],
                                RepoKeys(SALT))
        assert len(clones) == 1, clones


def test_local_branch_is_never_the_default():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d) / "projects" / "solo"
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        a = _commit(work, "a.txt", _lines("a"), 30)
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert clones[0].default is None, clones
        outs, _ = oc.evaluate([_row(a, None, 30)], clones, NOW, cache={})
        assert outs[0]["state"] == "open", outs                     # never pushed: not merged


def test_fetch_leaves_the_repo_alone_and_terminates_on_timeout(monkeypatch):
    seen: list = []
    real = og.subprocess.Popen

    def fake(cmd, **kw):
        if "fetch" in cmd:
            seen.append((cmd, kw.get("env", {}).get("GIT_TERMINAL_PROMPT")))
            return real(["sleep", "30"], **kw)                       # hangs like a dead remote
        return real(cmd, **kw)
    monkeypatch.setattr(og.subprocess, "Popen", fake)
    monkeypatch.setattr(og, "FETCH_TIMEOUT_S", 0.3)
    t = time.time()
    assert oc.fetch_clones([oc.Clone("/nonexistent", None, None, None)]) == (0, 1)
    assert time.time() - t < 10
    cmd, prompt = seen[0]
    for flag in ("gc.auto=0", "--no-auto-maintenance", "--no-write-fetch-head",
                 "--no-recurse-submodules", "--no-tags"):
        assert flag in cmd, (flag, cmd)
    assert prompt == "0" and "--prune" not in cmd


def test_repo_config_never_runs_commands():
    if os.name == "nt":                    # the fakes are POSIX shell scripts
        return
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        marks = Path(d) / "marks"
        marks.mkdir()
        git_dir = work / ".git"
        for ref in ("ctl1", "ctl2"):        # on origin but not fetched yet: each fetch adds a ref
            _g(work, "push", "-q", "origin", f"{s['A']}:refs/heads/{ref}")
            _g(work, "update-ref", "-d", f"refs/remotes/origin/{ref}")
        tools = {}
        for name in ("external", "textconv", "fsmonitor", "hook", "uploadpack"):
            sc = Path(d) / f"evil-{name}.sh"
            sc.write_text(f"#!/bin/sh\necho ran > '{marks / name}'\n")
            sc.chmod(0o755)
            tools[name] = str(sc)
        _g(work, "config", "diff.external", tools["external"])
        _g(work, "config", "diff.evil.textconv", tools["textconv"])
        _g(work, "config", "core.fsmonitor", tools["fsmonitor"])
        _g(work, "config", "remote.origin.uploadpack", tools["uploadpack"])
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "attributes").write_text("*.txt diff=evil\n")
        (work / ".gitattributes").write_text("*.txt diff=evil\n")
        hook = git_dir / "hooks" / "reference-transaction"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(Path(tools["hook"]).read_text())
        hook.chmod(0o755)

        def raw(*args):
            subprocess.run(["git", "-C", str(work), *args], capture_output=True, timeout=60)
        # control: plain git does run each of them, so the guarded run below proves something
        raw("log", "-p", "-1", s["A"])
        raw("status")
        raw("diff", "HEAD~1", "HEAD")
        raw("fetch", "origin")                                       # runs the uploadpack
        raw("fetch", "--upload-pack=git-upload-pack", "origin",
            "ctl1:refs/remotes/origin/ctl1")                         # runs the hook
        assert {p.name for p in marks.iterdir()} == set(tools), sorted(marks.iterdir())
        for p in marks.iterdir():
            p.unlink()

        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert oc.fetch_clones(clones) == (1, 0)                      # fetch to a local bare remote
        assert _g(work, "rev-parse", "refs/remotes/origin/ctl2") == s["A"]    # it did update refs
        outs, _ = oc.evaluate([_row(s["A"], key, 30), _row(s["B"], key, 30)], clones, NOW,
                              cache={})                             # log -p, patch-id, show, blame
        assert _states(outs)[s["A"][:9]][:2] == ("merged", 1.0), _states(outs)
        assert _states(outs)[s["B"][:9]][0] == "merged", _states(outs)     # via its patch-id twin
        assert RepoKeys(SALT).of_cwd(str(work)) == key
        for args in (("status",), ("diff", "HEAD~1", "HEAD"), ("blame", "HEAD", "--", "a.txt")):
            subprocess.run(git_argv(str(work), *args), capture_output=True, env=git_env(),
                           timeout=60)
        assert not list(marks.iterdir()), sorted(marks.iterdir())


def test_signing_proxy_alternates_and_helpers_never_run(monkeypatch):
    if os.name == "nt":                    # the fakes are POSIX shell scripts
        return
    import http.server
    import threading

    class Deny(http.server.BaseHTTPRequestHandler):       # every request asks for credentials
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Deny)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/r.git"
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        marks = Path(d) / "marks"
        marks.mkdir()
        tools = {}
        for name in ("gpg", "proxy", "alt", "cred", "credurl", "global"):
            sc = Path(d) / f"evil-{name}.sh"
            sc.write_text(f"#!/bin/sh\necho ran > '{marks / name}'\n")
            sc.chmod(0o755)
            tools[name] = str(sc)
        body = (f"tree {_g(work, 'rev-parse', 'HEAD^{tree}')}\nparent {s['A']}\n"
                "author t <t@example.com> 1 +0000\ncommitter t <t@example.com> 1 +0000\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n \n x\n -----END PGP SIGNATURE-----\n\nsigned\n")
        signed = subprocess.run(["git", "-C", str(work), "hash-object", "-t", "commit", "-w",
                                 "--stdin"], input=body, capture_output=True, text=True,
                                check=True).stdout.strip()
        _g(work, "update-ref", "refs/heads/signed", signed)
        _g(work, "config", "log.showSignature", "true")
        _g(work, "config", "gpg.program", tools["gpg"])
        _g(work, "config", "core.gitProxy", tools["proxy"])
        _g(work, "remote", "add", "gp", "git://127.0.0.1:9/x")
        subprocess.run(["git", "init", "-q", "--bare", str(Path(d) / "alt.git")], check=True)
        (work / ".git" / "objects" / "info" / "alternates").write_text(
            str(Path(d) / "alt.git" / "objects") + "\n")
        _g(work, "config", "core.alternateRefsCommand", tools["alt"])
        _g(work, "config", "credential.helper", "!" + tools["cred"])
        _g(work, "config", f"credential.{url}.helper", "!" + tools["credurl"])
        gconf = Path(d) / "global.gitconfig"
        gconf.write_text(f"[credential]\n\thelper = !{tools['global']}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gconf))
        try:
            def run(argv):
                subprocess.run(argv, capture_output=True, timeout=60, env=git_env())

            def plain(argv):                       # the control: git as the user runs it
                subprocess.run(argv, capture_output=True, timeout=60, env=dict(os.environ))

            def new_ref(name):                     # a fetch with nothing new never negotiates
                _g(work, "push", "-q", "origin", f"{s['A']}:refs/heads/{name}")
                _g(work, "update-ref", "-d", f"refs/remotes/origin/{name}")
            # control: plain git runs each of them
            g = ["git", "-C", str(work)]
            plain([*g, "log", "--format=%H", "-1", signed])
            plain([*g, "fetch", "gp"])
            new_ref("ctl1")
            plain([*g, "fetch", "origin"])
            plain([*g, "fetch", url])
            assert {p.name for p in marks.iterdir()} == set(tools), sorted(marks.iterdir())
            for p in marks.iterdir():
                p.unlink()
            run(git_argv(str(work), "log", "--format=%H %ct %P", "-1", signed))
            run(git_argv(str(work), "show", "-s", signed))
            run(git_argv(str(work), "fetch", "gp"))
            new_ref("ctl2")
            run(git_argv(str(work), "fetch", "origin"))
            assert _g(work, "rev-parse", "refs/remotes/origin/ctl2") == s["A"]   # fetch still works
            # the test server speaks plain http, which git_env() refuses: allow it for this run only
            argv = git_argv(str(work), "fetch", url)
            subprocess.run([argv[0], "-c", "protocol.http.allow=always", *argv[1:]],
                           capture_output=True, timeout=60,
                           env={**git_env(), "GIT_ALLOW_PROTOCOL": "https:ssh:file:http"})
            # the repo's helpers are gone, the user's global one still answers
            assert {p.name for p in marks.iterdir()} == {"global"}, sorted(marks.iterdir())
        finally:
            srv.shutdown()


def test_patch_id_failure_is_not_an_empty_answer():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        assert s["A"] in og._patch_ids(str(work), ["-1", s["A"]])
        assert og._patch_ids(str(work), ["refs/heads/no-such-ref"]) is None


def test_a_clone_git_cannot_identify_is_unreadable_not_gone(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        _work, s = _world(Path(d))
        with monkeypatch.context() as m:
            m.setattr(og, "_common_dir", lambda path: None)      # rev-parse timed out
            clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert not clones[0].readable, clones
        outs, stats = oc.evaluate([_row(s["A"], None, 30)], clones, NOW, cache={})
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
        assert stats["repo_unreadable"] == 1 and not stats["abandoned_not_found"], stats
        # a .git that holds no repository is simply no clone
        stale = Path(d) / "stale" / "x"
        (stale / ".git").mkdir(parents=True)
        assert og._common_dir(str(stale)) == ""
        assert oc.find_clones([str(stale.parent)], RepoKeys(SALT)) == []


def test_default_ref_ignores_an_origin_head_outside_origin():
    with tempfile.TemporaryDirectory() as d:
        work, _s = _world(Path(d))
        tip = _g(work, "rev-parse", "refs/remotes/origin/main").strip()
        (work / ".git" / "--ext-diff").write_text(tip + "\n")   # would reach git log as a flag
        (work / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: --ext-diff\n")
        assert og._default_ref(str(work)) == ("refs/remotes/origin/main", True)
        _g(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/main")
        assert og._default_ref(str(work)) == ("refs/remotes/origin/main", True)


def test_default_ref_failure_is_not_no_default():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        assert og._default_ref(str(work)) == ("refs/remotes/origin/main", True)
        _g(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
        assert og._default_ref(str(work)) == ("refs/remotes/origin/main", True)   # dangling HEAD
        _g(work, "update-ref", "-d", "refs/remotes/origin/main")
        assert og._default_ref(str(work)) == (None, True)            # no default: a real answer
        clones = _with_git(lambda a: a[0] == "for-each-ref",
                           lambda: oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT)))
        assert clones[0].default is None and not clones[0].readable, clones
        outs, _ = oc.evaluate([_row(s["A"], None, 30)], clones, NOW, cache={})
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs


def test_file_deleted_by_the_base_counts_as_zero_alive():
    with tempfile.TemporaryDirectory() as d:
        origin, work = Path(d) / "origin.git", Path(d) / "projects" / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        _g(work, "remote", "add", "origin", str(origin))
        _commit(work, "base.txt", "base\n", 60)
        x = _commit(work, "x.txt", _lines("x"), 40)
        _g(work, "rm", "-q", "x.txt")
        _g(work, "commit", "-q", "-m", "drop x", age=35)
        _commit(work, "later.txt", "later\n", 1)
        _g(work, "push", "-q", "origin", "main")
        _g(work, "remote", "set-head", "origin", "main")
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        cache: dict = {}
        outs, _ = oc.evaluate([_row(x, _key(str(origin)), 40)], clones, NOW, cache=cache)
        assert (outs[0]["state"], outs[0]["survival"]) == ("reworked", 0.0), outs
        assert cache == {x: 0.0}, cache


def test_a_clone_with_no_branches_still_answers():
    """No branch or remote ref at all: git exits 0 with nothing, which is not a failure."""
    with tempfile.TemporaryDirectory() as d:
        work = Path(d) / "projects" / "solo"
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        a = _commit(work, "a.txt", _lines("a"), 30)
        _g(work, "checkout", "-q", "--detach")
        _g(work, "branch", "-q", "-D", "main")
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        outs, stats = oc.evaluate([_row(a, None, 30)], clones, NOW, cache={})
        assert outs[0]["state"] == "abandoned" and not stats["repo_unreadable"], (outs, stats)


def test_repo_cannot_switch_a_protocol_back_on():
    # protocol.<name>.allow beats protocol.allow, so a repo setting protocol.git.allow=always
    # would turn git:// (and with it the repo's own core.gitProxy) back on; GIT_ALLOW_PROTOCOL wins.
    with tempfile.TemporaryDirectory() as d:
        work, _s = _world(Path(d))
        mark = Path(d) / "proxy-ran"
        sc = Path(d) / "evil-proxy.sh"
        sc.write_text(f"#!/bin/sh\necho ran > '{mark}'\n")
        sc.chmod(0o755)
        _g(work, "config", "protocol.git.allow", "always")
        _g(work, "config", "core.gitProxy", str(sc))
        _g(work, "remote", "add", "gp", "git://127.0.0.1:9/x")
        plain = subprocess.run(["git", "-C", str(work), "-c", "protocol.allow=never", "fetch",
                                "-q", "gp"], capture_output=True, timeout=60)
        assert plain.returncode != 0 and mark.exists(), "the repo setting must win without the guard"
        mark.unlink()
        r = subprocess.run(git_argv(str(work), "fetch", "-q", "gp"), capture_output=True,
                           timeout=60, env=git_env())
        assert r.returncode != 0 and not mark.exists(), r.stderr


def test_patch_ids_leaves_no_process_behind_on_timeout(monkeypatch):
    started = []
    real = subprocess.Popen

    def popen(cmd, **kw):
        started.append(real([sys.executable, "-c", "import time; time.sleep(60)"], **kw))
        return started[-1]

    monkeypatch.setattr(og.subprocess, "Popen", popen)
    monkeypatch.setattr(og, "PATCH_ID_TIMEOUT_S", 0.2)
    assert og._patch_ids("/nowhere", ["--no-merges"], "abc\n") is None
    assert len(started) == 2 and all(p.poll() is not None for p in started)


def test_a_failed_origin_read_is_an_unreadable_clone_not_a_clone_without_origin(tmp_path, monkeypatch):
    real = og.run_git
    monkeypatch.setattr(og, "_default_ref", lambda path: ("main", True))
    monkeypatch.setattr(og, "run_git", lambda path, *a, **kw: None if a[0] == "remote" else real(path, *a, **kw))
    assert not og._clone_at(str(tmp_path), str(tmp_path), RepoKeys("s" * 40)).readable
    monkeypatch.setattr(og, "run_git", lambda path, *a, **kw: "upstream\n" if a[0] == "remote" else None)
    clone = og._clone_at(str(tmp_path), str(tmp_path), RepoKeys("s" * 40))
    assert clone.readable and clone.key is None, "no origin at all is still readable"
