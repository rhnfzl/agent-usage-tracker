import hashlib
import os

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.proposals import prepare, stage
from agent_usage_tracker.improve.store import Store


def test_drafting_is_read_only_and_staging_verifies_the_base(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "instructions.txt"
    target.write_bytes(b"Use the old command.\n")
    store = Store(tmp_path / "private")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result", "status": "error"}]})
    proposal = prepare(store, case, root, target, "old command", "supported command")
    assert target.read_text() == "Use the old command.\n"
    saved = store.get("proposals", proposal)
    assert saved["before_sha256"] == hashlib.sha256(b"Use the old command.\n").hexdigest()
    candidate = tmp_path / "candidate"
    stage(store, proposal, candidate)
    assert (candidate / "instructions.txt").read_text() == "Use the supported command.\n"
    assert hashlib.sha256((candidate / "instructions.txt").read_bytes()).hexdigest() == saved["after_sha256"]
    target.write_text("A different command.\n")
    with pytest.raises(AutError, match="base changed"):
        stage(store, proposal, tmp_path / "another")


def test_ambiguous_edits_and_paths_outside_scope_are_rejected(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "instructions.txt"
    target.write_text("old old")
    other = tmp_path / "outside.txt"
    other.write_text("old")
    store = Store(tmp_path / "private")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result", "status": "error"}]})
    with pytest.raises(AutError, match="unique"):
        prepare(store, case, root, target, "old", "new")
    with pytest.raises(AutError, match="scope"):
        prepare(store, case, root, other, "old", "new")
    link = root / "link.txt"
    link.symlink_to(other)
    with pytest.raises(AutError, match="scope"):
        prepare(store, case, root, link, "old", "new")


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_proposal_staging_preserves_exact_newline_bytes(tmp_path, ending):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "instructions.txt"
    original = b"Use old." + ending + b"Keep this." + ending
    target.write_bytes(original)
    store = Store(tmp_path / "private")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    proposal = prepare(store, case, root, target, "old", "new")
    staged = stage(store, proposal, tmp_path / "candidate")
    assert staged.read_bytes() == original.replace(b"old", b"new")
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == store.get("proposals", proposal)["after_sha256"]


def test_stage_cannot_overwrite_original_or_an_unrelated_candidate(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "config.txt"
    target.write_text("old")
    store = Store(tmp_path / "private")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result", "status": "error"}]})
    proposal = prepare(store, case, root, target, "old", "new")
    with pytest.raises(AutError):
        stage(store, proposal, root)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "config.txt").write_text("unrelated")
    with pytest.raises(AutError, match="candidate"):
        stage(store, proposal, candidate)


def test_candidate_alias_cannot_modify_any_original_source_file(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "config.txt"
    target.write_text("old")
    store = Store(tmp_path / "private")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    proposal = prepare(store, case, root, target, "old", "new")
    (tmp_path / "config.txt").symlink_to(target)
    with pytest.raises(AutError):
        stage(store, proposal, tmp_path)
    assert target.read_text() == "old"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_stage_preserves_executable_mode(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "check.sh"
    target.write_text("#!/bin/sh\necho old\n")
    target.chmod(0o755)
    store = Store(tmp_path / "private")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    proposal = prepare(store, case, root, target, "old", "new")
    staged = stage(store, proposal, tmp_path / "candidate")
    assert staged.stat().st_mode & 0o777 == 0o755
