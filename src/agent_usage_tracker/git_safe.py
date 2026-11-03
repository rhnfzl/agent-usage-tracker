"""Every git command aut runs goes through here. aut runs git in clones it did not write (session
working dirs, everything under outcome_repo_roots), and a clone's own config, attributes and hooks
can name commands for git to run: diff.external, a textconv driver, core.fsmonitor,
core.sshCommand, core.askPass, remote.<name>.uploadpack, hooks (reference-transaction fires on
fetch), an ext:: remote, gpg.program (log.showSignature), core.gitProxy (git://),
core.alternateRefsCommand, a credential helper. These are switched off on the command line, which
outranks repo config. The system config is skipped; the user's global config (HOME) still applies,
so fetch keeps their ssh setup and gets their credential helpers back after the reset.
"""

from __future__ import annotations

import os
import subprocess

_CONFIG = ("core.fsmonitor=", "core.hooksPath=/dev/null", "protocol.ext.allow=never",
           "diff.external=", "core.sshCommand=ssh", "core.askPass=", "gc.auto=0",
           "maintenance.auto=false", "log.showSignature=false", "core.alternateRefsCommand=",
           # core.gitProxy is first-match-wins, so the repo's value beats -c: refuse git:// itself,
           # and with it every transport but these (a `foo::` remote helper too)
           "protocol.allow=never", "protocol.https.allow=always", "protocol.ssh.allow=always",
           "protocol.file.allow=always")
_NO_DRIVERS = ("--no-textconv", "--no-ext-diff")
# remote.<name>.uploadpack is multi-valued and the repo's value wins over -c: the flag beats both
_SUB_FLAGS = {"log": _NO_DRIVERS, "show": _NO_DRIVERS, "diff": _NO_DRIVERS,
              "blame": ("--no-textconv",), "fetch": ("--upload-pack=git-upload-pack",)}


def git_argv(path: str, sub: str, *args: str) -> list[str]:
    """argv for `git -C path <sub> <args>` with repo-defined commands switched off."""
    cfg = (*_CONFIG, *_credential_config()) if sub == "fetch" else _CONFIG
    return ["git", "-C", path, *(x for c in cfg for x in ("-c", c)), sub,
            *_SUB_FLAGS.get(sub, ()), *args]


def _credential_config() -> tuple[str, ...]:
    """An empty credential.helper clears every helper read so far (the repo's, url-scoped ones
    too), then the user's global ones go back in their order."""
    try:
        r = subprocess.run(["git", "config", "--global", "--includes", "-z", "--get-regexp",
                            r"^credential\..*helper$"], cwd=os.path.expanduser("~"),
                           capture_output=True, text=True, timeout=10, env=git_env())
        out = r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        out = ""
    pairs = (e.split("\n", 1) for e in out.split("\0") if e)
    return ("credential.helper=", *(f"{k}={v[0] if v else ''}" for k, *v in pairs))


def git_env() -> dict[str, str]:
    # GIT_ALLOW_PROTOCOL replaces every protocol.*.allow setting, the repo's own included: a repo
    # setting protocol.git.allow=always would otherwise beat the -c protocol.allow=never above.
    return dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                GIT_ALLOW_PROTOCOL="https:ssh:file")
