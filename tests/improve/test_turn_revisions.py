import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from agent_usage_tracker.improve.access import Evidence
from agent_usage_tracker.improve.evidence import read_session
from agent_usage_tracker.improve.file_reads import path_fingerprint
from agent_usage_tracker.improve.judge import freeze_case
from agent_usage_tracker.improve.store import Store, audit
from agent_usage_tracker.improve.study_independence import ReservedEvidence
from agent_usage_tracker.improve.study_observe import _sample, baseline_gate
from agent_usage_tracker.improve.turn_revisions import RECORD_KIND, _file_digest, lookup, record_hook


def git(repo, *args):
    return subprocess.check_output([
        "git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args,
    ], text=True).strip()


def hook(cwd, turn, event="UserPromptSubmit"):
    return json.dumps({
        "hook_event_name": event, "session_id": "session", "turn_id": turn, "cwd": str(cwd),
        "prompt": "private-fixture-prompt", "transcript_path": "private-fixture-path",
    }).encode()


def test_project_hook_records_a_later_worktree_turn_for_baseline_preflight(tmp_path, monkeypatch):
    project, worktree = tmp_path / "project", tmp_path / "worktree"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    git(project, "commit", "-qm", "session start", "--allow-empty")
    header = git(project, "rev-parse", "HEAD")
    git(project, "worktree", "add", "--detach", str(worktree))
    git(worktree, "commit", "-qm", "turn start", "--allow-empty")
    revision = git(worktree, "rev-parse", "HEAD")

    root = tmp_path / "private"
    relative_target = "skills folder/SKILL.md"
    target_key = path_fingerprint(worktree / relative_target)
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    done = subprocess.run([sys.executable, "-m", "agent_usage_tracker.cli", "evidence",
                           "--root", str(root), "record-codex-turn", "--project", str(project),
                           "--setup-file", relative_target],
                          input=hook(worktree, "later"), capture_output=True, check=True)
    assert not done.stdout and not done.stderr
    store = Store(root, read_only=True)
    record = lookup(store, "session", "later")
    assert record["commit"] == revision
    assert record["setup_files"] == {target_key: None}
    saved = store.path(RECORD_KIND, record["turn_key"]).read_text()
    assert "private-fixture" not in saved and str(worktree) not in saved and relative_target not in saved
    receipt_time = datetime.fromisoformat(record["recorded_at"])
    record_hook(root, project, hook(worktree, "later"))
    assert lookup(store, "session", "later") == record

    home = tmp_path / "home"
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    path = home / ".codex/sessions/rollout-local-fixture.jsonl"
    path.parent.mkdir(parents=True)
    rows = [("session_meta", start, {"id": "session", "source": "cli", "cwd": str(worktree),
                                    "git": {"commit_hash": header}})]
    for turn, began, ended in (
        ("opening", start, start + timedelta(milliseconds=25)),
        ("later", receipt_time - timedelta(milliseconds=10), receipt_time + timedelta(milliseconds=10)),
    ):
        rows.extend([
            ("event_msg", began, {"type": "task_started", "turn_id": turn}),
            ("turn_context", began, {"turn_id": turn, "model": "test", "cwd": str(worktree)}),
            ("event_msg", began, {"type": "user_message", "message": "Check " + turn}),
            ("event_msg", ended, {"type": "agent_message", "message": "Done " + turn}),
            ("event_msg", ended, {"type": "task_complete", "turn_id": turn}),
        ])
    path.write_text("".join(json.dumps({"type": kind, "timestamp": time.isoformat(),
                                        "payload": payload}) + "\n" for kind, time, payload in rows))
    writable = Store(root)
    captured = audit(writable, home, project=project, idle_s=0)
    reader = Evidence("redacted", store, captured["audit_id"])
    snapshot = reader.keys[0]
    events = [event for event in reader.session(snapshot)["items"] if event["kind"] == "boundary"]
    assert events[0]["turn_context"]["session_start_revision"] == header
    assert events[1]["turn_context"]["turn_start_revision"] == {
        "commit": revision, "recorded_at": receipt_time.isoformat(), "setup_files_checked": 1,
    }
    bundle = tmp_path / "share.json"
    reader.export(bundle, [snapshot])
    assert Evidence("redacted", bundle=bundle).session(snapshot)["items"] == reader.session(snapshot)["items"]
    assert target_key not in bundle.read_text()
    metadata = Evidence("metadata", store, captured["audit_id"])
    assert "turn_start_revision" not in metadata.event(snapshot, events[1]["id"])["turn_context"]
    case = freeze_case(writable, captured["audit_id"], snapshot, events[1]["id"])
    saved_attempt = _sample(store, case)
    assert saved_attempt["setup_revision"] == revision
    reserved = ReservedEvidence(frozenset(), frozenset(), frozenset())
    policy = {"metric": "tool_calls", "coverage_basis": "tool_events",
              "require_setup_revision": True, "require_turn_context": True, "require_worktree_setup": True}
    proposal = {"scope": str(project), "target": relative_target, "before_sha256": None}
    check = baseline_gate(policy, proposal, reserved)
    assert check(saved_attempt) is None
    assert check(saved_attempt | {"setup_files": {}}) == "worktree_setup_unverified"

    local = worktree / relative_target
    local.parent.mkdir()
    local.write_text("untracked local instructions", encoding="utf-8")
    record_hook(root, project, hook(worktree, "dirty"), (relative_target,))
    dirty = lookup(store, "session", "dirty")
    assert dirty["commit"] == revision
    assert set(dirty["setup_files"]) == {target_key}
    assert dirty["setup_files"][target_key] is not None
    assert check(saved_attempt | {"setup_files": dirty["setup_files"]}) == "worktree_setup_unverified"
    record_hook(root, project, hook(worktree, "later"), (relative_target,))
    assert lookup(store, "session", "later")["conflict"] is True

    git(worktree, "commit", "-qm", "worktree moves", "--allow-empty")
    record_hook(root, project, hook(worktree, "later"))
    assert lookup(store, "session", "later")["conflict"] is True
    assert _sample(store, case)["setup_revision"] == revision


def test_hook_skips_other_projects_and_unrecognized_inputs(tmp_path):
    project, other = tmp_path / "project", tmp_path / "other"
    for repo in (project, other):
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "commit", "-qm", "start", "--allow-empty")
    root = tmp_path / "private"
    record_hook(root, project, hook(other, "turn"))
    record_hook(root, project, hook(project, "turn", "Stop"))
    record_hook(root, project, b"invalid")
    assert not root.exists()


def test_hook_checks_literal_scoped_paths_and_keeps_unavailable_files_unknown(tmp_path):
    project, root = tmp_path / "project", tmp_path / "private"
    scope = project / "subproject"
    scope.mkdir(parents=True)
    git(project, "init", "-q")
    git(project, "commit", "-qm", "start", "--allow-empty")
    (scope / "present").write_bytes(b"correct\r\n")
    (scope / "folder").mkdir()
    (scope / "large").write_bytes(b"x" * (1024 * 1024 + 1))
    record_hook(root, scope, hook(project, "escape"), ("../other",))
    assert not root.exists()
    record_hook(root, scope, hook(project, "files"), ("present", "missing", "folder", "large"))
    files = lookup(Store(root, read_only=True), "session", "files")["setup_files"]
    assert files == {path_fingerprint(scope.resolve() / "present"): sha256(b"correct\r\n").hexdigest(),
                     path_fingerprint(scope.resolve() / "missing"): None}


@pytest.mark.parametrize("changes_during_read", [False, True])
def test_setup_digest_compares_change_timestamps_from_the_same_api(tmp_path, monkeypatch,
                                                                   changes_during_read):
    target = tmp_path / "setup"
    contents = b"setup instructions\r\n"
    target.write_bytes(contents)
    fstat = os.fstat
    calls = 0

    def handle_stat(fd):
        nonlocal calls
        calls += 1
        result = fstat(fd)
        # On affected Windows builds, path and handle APIs return different ctimes.
        return SimpleNamespace(st_mode=result.st_mode, st_size=result.st_size,
                               st_ino=result.st_ino, st_dev=result.st_dev,
                               st_mtime_ns=result.st_mtime_ns,
                               st_ctime_ns=result.st_ctime_ns + 100
                               + (calls if changes_during_read else 0))

    monkeypatch.setattr(os, "fstat", handle_stat)
    if changes_during_read:
        with pytest.raises(ValueError, match="changed while reading"):
            _file_digest(tmp_path.resolve(), (target.name,))
    else:
        assert _file_digest(tmp_path.resolve(), (target.name,)) == sha256(contents).hexdigest()


def test_turn_revision_rejects_late_mismatched_and_ambiguous_receipts(tmp_path):
    path = tmp_path / "rollout.jsonl"
    start = datetime(2026, 11, 21, tzinfo=timezone.utc)
    rows = [("session_meta", 0, {"id": "session", "source": "cli", "cwd": str(tmp_path),
                                 "git": {"commit_hash": "b" * 40}})]
    receipts = {"first": {"conflict": True}}
    for index, turn in enumerate(("first", "late", "wrong-worktree", "valid")):
        began = 10 * index
        if turn != "first":
            receipts[turn] = {
                "commit": "a" * 40,
                "workspace_sha256": path_fingerprint(tmp_path / "elsewhere" if turn == "wrong-worktree"
                                                     else tmp_path),
                "recorded_at": (start + timedelta(seconds=began + (4 if turn == "late" else 1))).isoformat(),
            }
        rows.extend([
            ("event_msg", began, {"type": "task_started", "turn_id": turn}),
            ("turn_context", began, {"turn_id": turn, "model": "test", "cwd": str(tmp_path)}),
            ("event_msg", began, {"type": "user_message", "message": "Check " + turn}),
            ("event_msg", began + 3, {"type": "agent_message", "message": "Done " + turn}),
            ("event_msg", began + 5, {"type": "task_complete", "turn_id": turn}),
        ])
    path.write_text("".join(json.dumps({"type": kind, "payload": payload,
                                        "timestamp": (start + timedelta(seconds=offset)).isoformat()})
                            + "\n" for kind, offset, payload in rows))
    session = read_session(path, "codex", turn_revision=lambda _, turn: receipts[turn])
    contexts = [row["turn_context"] for row in session["events"] if row["kind"] == "boundary"]
    assert all("turn_start_revision" not in context for context in contexts[:3])
    assert "session_start_revision" not in contexts[0]
    assert contexts[3]["turn_start_revision"]["commit"] == "a" * 40
