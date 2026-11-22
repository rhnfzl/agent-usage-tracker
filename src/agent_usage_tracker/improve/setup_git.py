"""Verify proposal contents in a local Git commit, independent of the worktree."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from ..git_safe import git_argv, git_env

COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _read_git(scope: Path, sub: str, *args: str) -> bytes:
    env = {**git_env(), "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LITERAL_PATHSPECS": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_GLOB_PATHSPECS",
                "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"):
        env.pop(key, None)
    return subprocess.run(git_argv(str(scope), sub, *args), stdout=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=5, check=True, env=env).stdout


def verify_setup(proposal: dict[str, Any], revision: str, condition: str) -> dict[str, str]:
    """Check the approved regular file at a literal path, using only local objects."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{7,64}", revision):
        return {"status": "unavailable"}
    scope = Path(proposal["scope"])
    target = PurePosixPath(proposal["target"])
    if target.is_absolute() or not target.parts or ".." in target.parts:
        return {"status": "unavailable"}
    try:
        commit = _read_git(scope, "rev-parse", "--verify", "--end-of-options",
                           f"{revision}^{{commit}}").strip().decode("ascii")
        if not COMMIT_ID.fullmatch(commit) or not commit.startswith(revision):
            return {"status": "unavailable"}
        prefix = _read_git(scope, "rev-parse", "--show-prefix").removesuffix(b"\n")
        path = prefix + os.fsencode(target.as_posix())
        entry = _read_git(scope, "ls-tree", "-z", "--full-tree", commit, "--", os.fsdecode(path))
        result = {"status": "different", "revision": commit}
        expected = proposal[f"{condition}_sha256"]
        if not entry:
            return result | {"status": "verified"} if expected is None else result
        fields, _, actual_path = entry.removesuffix(b"\0").partition(b"\t")
        mode, kind, oid = fields.decode("ascii").split()
        if (actual_path != path or expected is None or mode not in {"100644", "100755"}
                or kind != "blob" or not COMMIT_ID.fullmatch(oid)):
            return result
        size = int(_read_git(scope, "cat-file", "-s", oid))
        if size != len(proposal[condition].encode("utf-8")):
            return result
        blob = _read_git(scope, "cat-file", "blob", oid)
        return result | {"status": "verified"} if hashlib.sha256(blob).hexdigest() == expected else result
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"status": "unavailable"}


def target_in_workspace(proposal: dict[str, Any], cwd: str) -> Path | None:
    """Find the proposal path in the observed worktree of the same local repository."""
    scope, workspace = Path(proposal["scope"]).resolve(), Path(cwd)
    target = PurePosixPath(proposal["target"])
    if target.is_absolute() or not target.parts or ".." in target.parts or not workspace.is_absolute():
        return None
    if workspace.resolve() == scope:
        return scope / target
    try:
        args = ("--path-format=absolute", "--git-common-dir")
        if _read_git(scope, "rev-parse", *args) != _read_git(workspace, "rev-parse", *args):
            return None
        root = Path(os.fsdecode(_read_git(workspace, "rev-parse", "--show-toplevel").removesuffix(b"\n")))
        prefix = os.fsdecode(_read_git(scope, "rev-parse", "--show-prefix").removesuffix(b"\n"))
        return root / prefix / target if root.is_absolute() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
