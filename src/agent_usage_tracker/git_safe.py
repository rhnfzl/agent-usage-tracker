"""Every git command aut runs goes through here. aut runs git in clones it did not write (session
working dirs, everything under outcome_repo_roots), and a clone's own config, attributes and hooks
can name commands for git to run: diff.external, a textconv driver, core.fsmonitor,
core.sshCommand, core.askPass, remote.<name>.uploadpack, hooks (reference-transaction fires on
fetch), an ext:: remote. These are switched off on the command line, which outranks repo config.
The system config is skipped; the user's global config (HOME) still applies, so fetch keeps their
ssh and credential setup.
"""

from __future__ import annotations

import os

_CONFIG = ("core.fsmonitor=", "core.hooksPath=/dev/null", "protocol.ext.allow=never",
           "diff.external=", "core.sshCommand=ssh", "core.askPass=", "gc.auto=0",
           "maintenance.auto=false")
_NO_DRIVERS = ("--no-textconv", "--no-ext-diff")
# remote.<name>.uploadpack is multi-valued and the repo's value wins over -c: the flag beats both
_SUB_FLAGS = {"log": _NO_DRIVERS, "show": _NO_DRIVERS, "diff": _NO_DRIVERS,
              "blame": ("--no-textconv",), "fetch": ("--upload-pack=git-upload-pack",)}


def git_argv(path: str, sub: str, *args: str) -> list[str]:
    """argv for `git -C path <sub> <args>` with repo-defined commands switched off."""
    return ["git", "-C", path, *(x for c in _CONFIG for x in ("-c", c)), sub,
            *_SUB_FLAGS.get(sub, ()), *args]


def git_env() -> dict[str, str]:
    return dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
