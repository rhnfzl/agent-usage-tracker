"""Attach private, bounded local Git evidence to captured attempts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Any

from ..collectors.commits import commit_call, commit_shas
from ..collectors.activity_common import text_of
from ..config import AutError
from ..git_safe import git_argv, git_env
from ..outcome_git import default_ref
from ..redact import redact
from ..state import _locked
from .case_view import _attempt
from .evidence import event_links
from .rubric import _shell_text
from .store import Store

MAX_COMMITS = 8
MAX_BYTES = 65536


def _command(event: dict[str, Any]) -> str:
    return _shell_text(event.get("text", ""), event.get("tool", "exec_command")) or ""


def _result(event: dict[str, Any]) -> str:
    text = event.get("text", "")
    try:
        value = json.loads(text)
    except ValueError:
        return str(text)
    if isinstance(value, dict):
        return text_of(value.get("output", value.get("aggregated_output", value.get("content", ""))))
    return text_of(value) if isinstance(value, list) else str(text)


def commit_evidence(events: list[dict[str, Any]], links: dict[str, str] | None = None
                    ) -> Iterator[dict[str, str]]:
    paired = event_links(events) if links is None else links
    by_id = {event["id"]: event for event in events}
    for call in events:
        call_id = call["id"]
        result_id = paired.get(call_id)
        if result_id is None:
            continue
        result = by_id[result_id]
        matched = commit_call(_command(call))
        if matched is not None:
            for sha in commit_shas(_result(result), err=result.get("status") == "error",
                                   before=matched[1]):
                yield {"call_event_id": call_id, "result_event_id": result_id, "observed_sha": sha}


def commits_in(case: dict[str, Any], session: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(row["observed_sha"][:7]
                              for row in commit_evidence(_attempt(session, case))))


def _git(cwd: str, deadline: float, *args: str) -> tuple[str, bool]:
    """Read at most MAX_BYTES; stop the process on output or time exhaustion."""
    remaining = min(5, deadline - time.monotonic())
    if remaining <= 0:
        raise AutError("improve: outputs time limit")
    try:
        with subprocess.Popen(git_argv(cwd, *args), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env={**git_env(), "GIT_NO_LAZY_FETCH": "1",
                                   "GIT_ALLOW_PROTOCOL": ""}) as process:
            assert process.stdout is not None
            stdout = process.stdout
            chunks: list[bytes] = []

            def read() -> None:
                chunks.append(stdout.read(MAX_BYTES + 1))
                if len(chunks[0]) > MAX_BYTES:
                    process.kill()

            reader = Thread(target=read, daemon=True)
            reader.start()
            reader.join(remaining)
            if reader.is_alive():
                process.kill()
                reader.join()
                raise AutError("improve: outputs time limit")
            try:
                process.wait(timeout=max(.001, min(5, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                process.kill()
                raise
            clipped = len(chunks[0]) > MAX_BYTES
            if process.returncode and not clipped:
                raise AutError("improve: outputs Git read failed")
            return chunks[0][:MAX_BYTES].decode("utf-8", "replace"), clipped
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutError("improve: outputs Git read failed") from exc


def latest(store: Store) -> dict[str, tuple[str, dict[str, Any]]]:
    rows: dict[str, tuple[str, dict[str, Any]]] = {}
    for key in store.keys("case_outputs"):
        row = store.get("case_outputs", key)
        previous = rows.get(row["case_id"])
        if previous is None or row["checked_at"] > previous[1]["checked_at"]:
            rows[row["case_id"]] = key, row
    return rows


def _attach(store: Store, case_id: str, budget_s: float) -> str | None:
    deadline = time.monotonic() + max(0, budget_s)
    case = store.get("cases", case_id)
    session = store.get("sessions", case["session_snapshot"])
    shas = commits_in(case, session)
    cwd = session.get("cwd")
    if not shas or not cwd:
        return None
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    if time.monotonic() >= deadline:
        return None
    default, readable = default_ref(cwd, timeout=max(.001, min(5, deadline - time.monotonic())))
    if not readable:
        return None
    commits, stats, patches, warnings = [], [], [], []
    truncated = len(shas) > MAX_COMMITS
    for sha in shas[:MAX_COMMITS]:
        try:
            full, _ = _git(cwd, deadline, "rev-parse", "--verify", f"{sha}^{{commit}}")
            full = full.strip()
            if not re.fullmatch(r"[a-f0-9]{40,64}", full):
                raise AutError("improve: unresolved output commit")
            # shortcut: local ancestry misses squash merges; use sink outcomes if this is too coarse.
            refs, clipped = _git(cwd, deadline, "for-each-ref", "--format=%(refname)",
                                 f"--contains={full}", "refs/heads", "refs/remotes/origin")
            if clipped:
                raise AutError("improve: output refs limit")
            state = "merged" if default and default in refs.splitlines() else \
                "open" if any(r.startswith("refs/heads/") for r in refs.splitlines()) else "unreachable"
            if default:
                reverted, clipped = _git(cwd, deadline, "log", "-1", "--format=%h", "-F",
                                        f"--grep=This reverts commit {full}", default, "--")
                if clipped:
                    raise AutError("improve: output log limit")
                if reverted.strip():
                    state = "reverted"
            stat, stat_cut = _git(cwd, deadline, "show", "--stat=100", "--format=%h", full, "--")
            numbers, num_cut = _git(cwd, deadline, "show", "--numstat", "--no-renames",
                                   "--format=", full, "--")
            if num_cut:
                raise AutError("improve: output stat limit")
            counts = [line.split("\t", 2) for line in numbers.splitlines() if "\t" in line]
            patch, patch_cut = _git(cwd, deadline, "show", "--format=", "-U1", "--no-color", full, "--")
            if counts:
                stats.append(redact(stat))
            patches.append(redact(patch))
            truncated |= stat_cut or patch_cut
            commits.append({"sha7": sha, "state": state, "files_changed": len(counts),
                            "insertions": sum(int(c[0]) for c in counts if c[0].isdigit()),
                            "deletions": sum(int(c[1]) for c in counts if c[1].isdigit())})
        except AutError as exc:
            warnings.append(str(exc))
            truncated = True
    if not commits:
        return None
    events = _attempt(session, case)
    pr_calls = {e.get("call_id") for e in events if e["kind"] == "call"
                and re.search(r"\bgh\s+pr\s+create\b", _command(e))}
    prs = sorted({int(n) for e in events if e["kind"] == "result" and e.get("call_id") in pr_calls
                  for n in re.findall(r"/pull/(\d+)\b", _result(e))})
    patch = "\n".join(patches)
    value = {"case_id": case_id, "session_snapshot": case["session_snapshot"], "commits": commits,
             "diff_stat": "\n".join(stats)[:800], "patch": patch[:1500],
             "patch_truncated": truncated or len(patch) > 1500, "pr_numbers": prs,
             "warnings": sorted(set(warnings))}
    previous = latest(store).get(case_id)
    if previous and value == {k: v for k, v in previous[1].items()
                              if k not in {"checked_day", "checked_at"}}:
        return previous[0]
    now = datetime.now(timezone.utc)
    if previous:
        now = max(now, datetime.fromisoformat(previous[1]["checked_at"]) + timedelta(microseconds=1))
    if previous and [(c["sha7"], c["state"]) for c in previous[1]["commits"]] != [
            (c["sha7"], c["state"]) for c in commits]:
        from .judge import update_queue

        update_queue(store, add=[case_id], name="merge_queue")
    return store.put("case_outputs", {**value, "checked_day": now.date().isoformat(),
                                      "checked_at": now.isoformat()})


def attach(store: Store, case_id: str, *, budget_s: float = 10) -> str | None:
    with _locked(store.root / "outputs", blocking=False):
        return _attach(store, case_id, budget_s)


def refresh(store: Store, *, days: int = 30) -> list[str]:
    changed = []
    deadline = time.monotonic() + 30
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _locked(store.root / "outputs"):
        for case_id, (_, old) in latest(store).items():
            if time.monotonic() >= deadline:
                store.append("outputs_skipped", reason="refresh_time_limit")
                break
            if old["checked_day"] < cutoff or not any(c["state"] == "open" for c in old["commits"]):
                continue
            key = _attach(store, case_id, min(10, deadline - time.monotonic()))
            if key is None:
                store.append("outputs_skipped", case_id=case_id, reason="refresh_unavailable")
                continue
            new = store.get("case_outputs", key)
            if [(c["sha7"], c["state"]) for c in old["commits"]] != [
                    (c["sha7"], c["state"]) for c in new["commits"]]:
                changed.append(case_id)
    return changed


def consume(store: Store, completed: dict[str, str]) -> None:
    """A refresh during judging must keep its newer snapshot queued."""
    from .judge import update_queue

    if completed:
        with _locked(store.root / "outputs"):
            current = latest(store)
            update_queue(store, name="merge_queue", remove=[case_id for case_id, key
                         in completed.items() if current.get(case_id, (None,))[0] == key])
