"""Human decisions on proposals: accept, reject and adopt.

Each decision is a new append-only record that points at a proposal; nothing is edited in place.
Accept commits only inside the proposal's candidate clone. Nothing here writes to a target."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404 # fixed git argv through git_argv, no shell
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AutError
from ..git_safe import git_argv, git_env
from ..label.core import data_dir
from ..state import _locked
from .groups import _journal
from .proposals import file_digest, prepare, stage
from .store import Store, repository

KINDS = ("instruction", "skill", "config", "code")
_PAST = {"accept": "accepted", "reject": "rejected", "stale": "marked stale"}
# prepare() fields; everything else on a proposal is its `extra` and is copied on re-prepare
_CORE = {"version", "case_id", "scope", "target", "before_sha256", "after_sha256", "before",
         "after", "diff", "span", "authority", "state"}
_MANAGED = re.compile(r"<!-- BEGIN\b.*?-->.*?<!-- END\b.*?-->", re.S)
NOTICE = "The title and date are sent to the sink by publish-improve; targets and commit stay private."


def command(store: Store) -> str:
    """`aut improve`, plus --root (shell-quoted) when the store is not the default."""
    if store.root == (data_dir() / "improve").resolve():
        return "aut improve"
    return f"aut improve --root {shlex.quote(str(store.root))}"


def resolve(store: Store, prefix: str) -> str:
    """The proposal whose key starts with this hex prefix (at least 6 characters)."""
    prefix = prefix.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6,64}", prefix):
        raise AutError("improve: a proposal id is 6 to 64 hex characters")
    matches = [key for key in store.keys("proposals") if key.startswith(prefix)]
    if len(matches) > 1:
        raise AutError("improve: ambiguous proposal id; matches "
                       + ", ".join(key[:12] for key in matches))
    if not matches:
        raise AutError("improve: no proposal has this id")
    return matches[0]


def short_id(store: Store, proposal_id: str) -> str:
    """The shortest unique prefix, at least 8 hex characters."""
    others = [key for key in store.keys("proposals") if key != proposal_id]
    size = 8
    while any(key.startswith(proposal_id[:size]) for key in others):
        size += 1
    return proposal_id[:size]


def decisions(store: Store) -> dict[str, dict[str, Any]]:
    """The latest decision per proposal id."""
    rows = sorted((store.get("decisions", key) | {"decision_id": key}
                   for key in store.keys("decisions")),
                  key=lambda row: (row["decided_at"], row["decision_id"]))
    return {row["proposal_id"]: row for row in rows}


def open_proposals(store: Store) -> list[str]:
    """Proposals with no decision yet, newest first."""
    decided = decisions(store)
    order = {row["proposal_id"]: index for index, row in enumerate(_journal(store))
             if row.get("kind") == "proposal_prepared" and "proposal_id" in row}
    keys = [key for key in store.keys("proposals") if key not in decided]
    return sorted(keys, key=lambda key: (order.get(key, -1), key), reverse=True)


def staged_at(store: Store, proposal_id: str) -> Path | None:
    """The candidate directory of the newest `proposal_staged` journal row, if any."""
    row = next((row for row in reversed(_journal(store)) if row.get("kind") == "proposal_staged"
                and row.get("proposal_id") == proposal_id), None)
    return Path(row["candidate"]) if row and isinstance(row.get("candidate"), str) else None


def managed_spans(text: str) -> list[tuple[int, int]]:
    """Generated `<!-- BEGIN … -->` … `<!-- END … -->` regions that a tool rewrites itself."""
    return [match.span() for match in _MANAGED.finditer(text)]


def group_status(store: Store) -> dict[str, str]:
    """Group key to adopted, proposed, accepted (waiting for its merge) or "rejected on <day>";
    groups not listed are open."""
    latest = decisions(store)
    status: dict[str, str] = {}
    accepted = set()
    for key in store.keys("proposals"):
        group, decision = store.get("proposals", key).get("group_key"), latest.get(key)
        if group and decision and decision["action"] == "reject":
            status[group] = max(status.get(group, ""), f"rejected on {decision['decided_at'][:10]}")
        elif group and decision and decision["action"] == "accept":
            accepted.add(group)
    status |= dict.fromkeys(accepted, "accepted")
    for key in open_proposals(store):
        if group := store.get("proposals", key).get("group_key"):
            status[group] = "proposed"
    for key in store.keys("adoptions"):
        if group := store.get("adoptions", key).get("group_key"):
            status[group] = "adopted"
    return status


def _chain(latest: dict[str, dict[str, Any]], proposal_id: str) -> list[str]:
    """This proposal and every proposal that superseded it, oldest first."""
    chain = [proposal_id]
    while (latest.get(chain[-1]) or {}).get("superseded_by"):
        chain.append(latest[chain[-1]]["superseded_by"])
        if chain[-1] in chain[:-1]:
            raise AutError("improve: proposal supersede chain loops")
    return chain


def _follow(latest: dict[str, dict[str, Any]], proposal_id: str) -> str:
    """A superseded proposal stands for the proposal that replaced it."""
    return _chain(latest, proposal_id)[-1]


def _journaled(store: Store, kind: str, field: str, key: str, **attrs: Any) -> None:
    """Add the journal row a crash between a record write and its journal row left out."""
    if not any(row.get("kind") == kind and row.get(field) == key for row in _journal(store)):
        store.append(kind, **{field: key}, **attrs)


def _settled(store: Store, latest: dict[str, dict[str, Any]], proposal_id: str,
             action: str) -> dict[str, Any] | None:
    decision = latest.get(proposal_id)
    if decision is None:
        return None
    if decision["action"] == "stale" and action == "reject":
        return None     # a stale proposal can still be rejected, which blocks its group
    done = f"{_PAST[decision['action']]} on {decision['decided_at'][:10]}"
    if decision["action"] != action:
        raise AutError(f"improve: proposal {proposal_id[:8]} was already {done}")
    _journaled(store, "decision", "decision_id", decision["decision_id"],
               proposal_id=proposal_id, action=action)
    return {"status": "unchanged", "proposal_id": proposal_id,
            "decision_id": decision["decision_id"], "message": f"Already {done}; nothing changed."}


def _record(store: Store, proposal_id: str, proposal: dict[str, Any], action: str,
            **fields: Any) -> str:
    value = {"version": 1, "proposal_id": proposal_id, "group_key": proposal.get("group_key"),
             "action": action, "reason": None, "note": None, "candidate_commit": None,
             "decided_at": datetime.now(timezone.utc).isoformat(), **fields}
    key = store.put("decisions", value)
    store.append("decision", decision_id=key, proposal_id=proposal_id, action=action)
    return key


def _git(path: Path, sub: str, *args: str, check: bool = True,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:    # literal pathspecs: a target named rules[1].md must not also match rules1.md
        result = subprocess.run(git_argv(str(path), sub, *args),  # nosec B603
                                capture_output=True, text=True, timeout=60,
                                env=git_env() | {"GIT_LITERAL_PATHSPECS": "1"} | (env or {}))
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutError(f"improve: git {sub} did not run in the candidate") from exc
    if check and result.returncode:
        line = (result.stderr.strip().splitlines() or [""])[0][:200]
        raise AutError(f"improve: git {sub} failed in the candidate: {line}")
    return result


def _commit(proposal_id: str, proposal: dict[str, Any], candidate: Path) -> str | None:
    """Commit the staged file on aut/improve-<id8> inside the candidate, with plumbing only, so
    no checkout, hook or clean filter runs. None unless the candidate's git dir is its own
    `.git`: a worktree writes its refs into another repository."""
    own = str((candidate / ".git").resolve())
    gitdir = _git(candidate, "rev-parse", "--absolute-git-dir", check=False)
    if gitdir.returncode or str(Path(gitdir.stdout.strip()).resolve()) != own \
            or repository(candidate) != own:
        return None
    target = proposal["target"]
    ref, marker = f"refs/heads/aut/improve-{proposal_id[:8]}", f"Proposal: {proposal_id}"
    if _git(candidate, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0:
        if marker in _git(candidate, "cat-file", "commit", ref).stdout:
            return _git(candidate, "rev-parse", ref).stdout.strip()   # a retried accept
        raise AutError(f"improve: the candidate already has {ref} for other work")
    listed = _git(candidate, "ls-tree", "HEAD", "--", target).stdout.split()
    mode = listed[0] if listed and listed[0] in {"100644", "100755"} else "100644"
    blob = _git(candidate, "hash-object", "-w", "--no-filters", "--",
                str(candidate / target)).stdout.strip()
    with tempfile.TemporaryDirectory() as scratch:     # a private index; the candidate's stays
        index = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(candidate, "read-tree", "HEAD", env=index)
        _git(candidate, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{target}",
             env=index)
        tree = _git(candidate, "write-tree", env=index).stdout.strip()
    title = (str(proposal.get("title") or f"Update {target}").splitlines() or [""])[0]
    commit = _git(candidate, "commit-tree", "--no-gpg-sign", tree, "-p", "HEAD",
                  "-m", title[:120], "-m", marker).stdout.strip()
    _git(candidate, "update-ref", ref, commit, "")    # "" creates only, never moves a branch
    return commit


def _reprepare(store: Store, proposal_id: str, proposal: dict[str, Any],
               source: Path) -> str | None:
    """A new proposal from the same span when the target changed elsewhere; None when the span
    is gone, no longer unique or inside a managed block (or the proposal predates spans)."""
    span: dict[str, str] = proposal.get("span") or {}
    try:
        text = source.read_bytes().decode("utf-8")
        start = text.index(span["before"]) if text.count(span["before"]) == 1 else -1
    except (OSError, UnicodeError, TypeError, KeyError):
        return None
    end = start + len(span["before"])
    if start < 0 or any(low < end and start < high for low, high in managed_spans(text)):
        return None
    extra = {key: value for key, value in proposal.items() if key not in _CORE}
    try:
        return prepare(store, proposal["case_id"], Path(proposal["scope"]), source,
                       span["before"], span["after"], authority=proposal["authority"],
                       extra=extra | {"supersedes": proposal_id})
    except AutError:
        return None


def _next_steps(store: Store, proposal_id: str, proposal: dict[str, Any],
                candidate: Path | None, commit: str | None) -> list[str]:
    scope = Path(proposal["scope"])
    target = scope / proposal["target"]
    adopt = f"after the merge: {command(store)} adopt {proposal_id} --commit <sha> [--pr N]"
    if commit and candidate:
        branch = f"aut/improve-{proposal_id[:8]}"
        return [f"git -C {shlex.quote(str(scope))} fetch {shlex.quote(str(candidate))} {branch}",
                f"git -C {shlex.quote(str(scope))} switch -c {branch} FETCH_HEAD",
                "review it, push and open a pull request", adopt]
    if candidate:
        return [f"check that sha256 of {shlex.quote(str(target))} is still "
                f"{proposal['before_sha256']}",
                f"cp {shlex.quote(str(candidate / proposal['target']))} "
                f"{shlex.quote(str(target))}", adopt]
    return ["not staged: apply the diff saved in "
            f"{shlex.quote(str(store.path('proposals', proposal_id)))} by hand", adopt]


def accept(store: Store, proposal_id: str, note: str | None = None) -> dict[str, Any]:
    note = _text(note, 500, "note")
    with _locked(store.root / "decide"):
        latest = decisions(store)
        proposal_id = _follow(latest, proposal_id)
        if done := _settled(store, latest, proposal_id, "accept"):
            return done
        proposal = store.get("proposals", proposal_id)
        source = Path(proposal["scope"]) / proposal["target"]
        candidate = staged_at(store, proposal_id)
        superseded = None
        try:
            unchanged = file_digest(source) == proposal["before_sha256"]
        except FileNotFoundError:
            unchanged = False
        except OSError as exc:      # a passing lock or permission problem must not mark it stale
            raise AutError("improve: the proposal target cannot be read now; try again") from exc
        if not unchanged:
            new_id = _reprepare(store, proposal_id, proposal, source)
            if new_id is None:
                key = _record(store, proposal_id, proposal, "stale", reason="target changed")
                return {"status": "stale", "proposal_id": proposal_id, "decision_id": key,
                        "message": "improve: target changed: the next draft run may redraft "
                                   "this group"}
            if new_id in latest:    # made before a crash and already decided on its own
                _record(store, proposal_id, proposal, "stale", superseded_by=new_id,
                        reason="target changed outside the span; superseded")
                newest = _follow(latest, new_id)
                if done := _settled(store, latest, newest, "accept"):    # raises after a reject
                    return done
                raise AutError(f"improve: superseded by {newest[:8]}; accept that id")
            if candidate:   # the old candidate holds the old content, so stage beside it
                candidate = store.root / "candidates" / new_id
                stage(store, new_id, candidate)
            _record(store, proposal_id, proposal, "stale", superseded_by=new_id,
                    reason="target changed outside the span; superseded")
            superseded, proposal_id = proposal_id, new_id
            proposal = store.get("proposals", proposal_id)
        commit = None
        if candidate:
            staged = candidate / proposal["target"]
            if not staged.is_file() or file_digest(staged) != proposal["after_sha256"]:
                raise AutError("improve: the staged candidate changed; stage the proposal again")
            commit = _commit(proposal_id, proposal, candidate)
        key = _record(store, proposal_id, proposal, "accept", note=note, candidate_commit=commit)
    return {"status": "accepted", "proposal_id": proposal_id, "decision_id": key,
            "supersedes": superseded, "candidate_commit": commit,
            "next": _next_steps(store, proposal_id, proposal, candidate, commit)}


def _writable(func: Any, path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE)    # git objects are read-only, and Windows refuses to delete them
    func(path)


def _remove_candidate(store: Store, proposal_id: str) -> bool:
    """Delete <store>/candidates/<id> only; an operator's own --candidate path stays."""
    base = (store.root / "candidates").resolve()
    path = (base / proposal_id).resolve()
    if path.parent != base or not path.is_dir():
        return False
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable)
    else:
        shutil.rmtree(path, onerror=_writable)
    return True


def reject(store: Store, proposal_id: str, reason: str) -> dict[str, Any]:
    text = _text(reason, 500, "reason")
    if not text or not any(c.isalnum() for c in text):    # the page's placeholder is "…"
        raise AutError("improve: a reject needs a reason")
    with _locked(store.root / "decide"):
        latest = decisions(store)
        chain = _chain(latest, proposal_id)
        proposal_id = chain[-1]
        result = _settled(store, latest, proposal_id, "reject")
        if result is None:
            proposal = store.get("proposals", proposal_id)
            key = _record(store, proposal_id, proposal, "reject", reason=text)
            result = {"status": "rejected", "proposal_id": proposal_id, "decision_id": key}
        result["candidate_removed"] = any([_remove_candidate(store, key) for key in chain])
    return result


def _text(value: str | None, limit: int, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > limit:
        raise AutError(f"improve: {name} is longer than {limit} characters")
    return value or None


def adopt(store: Store, proposal_id: str | None = None, *, commit: str,
          title: str | None = None, pr: int | None = None, adopted_at: str | None = None,
          targets: list[str] | None = None, rubric_ids: list[str] | None = None,
          kind: str | None = None, note: str | None = None) -> dict[str, Any]:
    with _locked(store.root / "decide"):
        proposal: dict[str, Any] = {}
        if proposal_id is not None:
            latest = decisions(store)
            proposal_id = _follow(latest, proposal_id)
            if (latest.get(proposal_id) or {}).get("action") != "accept":
                raise AutError("improve: accept the proposal before adopting it")
            proposal = store.get("proposals", proposal_id)
        if proposal.get("authority", "operator_scoped_proposal") != "operator_scoped_proposal":
            proposal = proposal | {"title": None}   # a drafted title is model text: a human writes it
        title = _text(title or proposal.get("title"), 120, "title")
        if not title or any(ord(c) < 32 or ord(c) == 127 for c in title):
            raise AutError("improve: adopt needs a one-line --title")
        commit = commit.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            raise AutError("improve: --commit is 7 to 40 hex characters")
        if pr is not None and pr < 1:
            raise AutError("improve: --pr is a positive number")
        try:
            day = date.fromisoformat(adopted_at).isoformat() if adopted_at \
                else date.today().isoformat()
        except ValueError as exc:
            raise AutError("improve: --date is YYYY-MM-DD") from exc
        kind = kind or proposal.get("kind") or "instruction"
        rubric_ids = list(rubric_ids or proposal.get("rubric_ids") or [])
        if kind not in KINDS or not all(isinstance(r, str) and re.fullmatch(r"[a-z_]{1,40}", r)
                                        for r in rubric_ids):
            raise AutError("improve: unknown --kind or --rubric id")
        if not targets and proposal:
            targets = [str(Path(proposal["scope"]) / proposal["target"])]
        value = {"version": 1, "title": title, "adopted_at": day, "kind": kind,
                 "targets": sorted({str(Path(t).expanduser().absolute()) for t in targets or []}),
                 "commit": commit, "pr": pr, "rubric_ids": sorted(set(rubric_ids)),
                 "proposal_id": proposal_id, "group_key": proposal.get("group_key"),
                 "note": _text(note, 500, "note"), "benefit_claim": False}
        same = ("proposal_id", "commit") if proposal_id else ("title", "commit")
        for key in store.keys("adoptions"):     # again on a later day is still the same adoption
            old = store.get("adoptions", key)
            if all(old.get(name) == value[name] for name in same):
                _journaled(store, "adoption", "adoption_id", key, proposal_id=old["proposal_id"])
                return {"status": "unchanged", "adoption_id": key, "adopted_at": old["adopted_at"],
                        "notice": NOTICE}
        key = store.put("adoptions", value)
        store.append("adoption", adoption_id=key, proposal_id=proposal_id)
    return {"status": "adopted", "adoption_id": key, "adopted_at": day, "notice": NOTICE}
