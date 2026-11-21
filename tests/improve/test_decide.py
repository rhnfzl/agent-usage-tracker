import json
import subprocess
import sys
import threading

import pytest

from agent_usage_tracker import cli
from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import decide
from agent_usage_tracker.improve.proposals import prepare, stage
from agent_usage_tracker.improve.store import Store


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@x",
                           "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name in ("AUTHOR", "COMMITTER"):     # CI runners have no git identity
        monkeypatch.setenv(f"GIT_{name}_NAME", "T")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "t@x")
    return Store(tmp_path / "private")


def proposal(store, tmp_path, text="Use the old command.\n", before="old", after="new", **extra):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "rules.md").write_text(text, newline="")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result", "status": "error"}]})
    return source, prepare(store, case, source, source / "rules.md", before, after, extra=extra)


def repo(tmp_path, text="Use the old command.\n"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "rules.md").write_text(text, newline="")
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "rules.md")
    git(source, "commit", "-qm", "base")
    return source


def test_accept_commits_only_inside_a_shared_clone_candidate(store, tmp_path):
    source = repo(tmp_path)
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    key = prepare(store, case, source, source / "rules.md", "old", "new", extra={"title": "<b>T"})
    candidate = tmp_path / "candidate"
    subprocess.run(["git", "clone", "-q", "--shared", str(source), str(candidate)], check=True)
    stage(store, key, candidate)
    before = git(source, "rev-list", "--all")
    result = decide.accept(store, key)
    assert result["status"] == "accepted" and result["candidate_commit"]
    assert git(candidate, "rev-parse", f"aut/improve-{key[:8]}") == result["candidate_commit"]
    assert git(candidate, "rev-list", "--count", f"main..aut/improve-{key[:8]}") == "1"
    assert f"Proposal: {key}" in git(candidate, "log", "-1", "--format=%B", f"aut/improve-{key[:8]}")
    assert git(source, "rev-list", "--all") == before and git(source, "branch") == "* main"
    assert (source / "rules.md").read_text() == "Use the old command.\n"
    assert any("fetch" in step for step in result["next"])
    again = decide.accept(store, key)
    assert again["status"] == "unchanged" and "Already accepted" in again["message"]
    assert len(store.keys("decisions")) == 1


def test_a_worktree_candidate_gets_no_commit(store, tmp_path):
    source = repo(tmp_path)
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    key = prepare(store, case, source, source / "rules.md", "old", "new")
    candidate = tmp_path / "wt"
    git(source, "worktree", "add", "-q", "--detach", str(candidate))
    stage(store, key, candidate)
    branches = git(source, "branch", "--list")
    result = decide.accept(store, key)
    assert result["candidate_commit"] is None
    assert git(source, "branch", "--list") == branches
    assert any(step.startswith("cp ") for step in result["next"])


def test_a_retried_accept_reuses_the_commit_already_made(store, tmp_path):
    source = repo(tmp_path)
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    key = prepare(store, case, source, source / "rules.md", "old", "new")
    candidate = tmp_path / "candidate"
    subprocess.run(["git", "clone", "-q", "--shared", str(source), str(candidate)], check=True)
    stage(store, key, candidate)
    first = decide._commit(key, store.get("proposals", key), candidate)   # then a crash
    assert decide.accept(store, key)["candidate_commit"] == first


def test_accept_supersedes_when_the_target_changed_outside_the_span(store, tmp_path):
    text = "<!-- BEGIN router (managed) -->\nv1\n<!-- END router -->\nUse the old command.\n"
    source, key = proposal(store, tmp_path, text=text, title="T", group_key="g")
    stage(store, key, tmp_path / "operator-candidate")
    (source / "rules.md").write_text(text.replace("v1", "v2"), newline="")
    result = decide.accept(store, key)
    new = result["proposal_id"]
    assert result["supersedes"] == key and new != key
    saved = store.get("proposals", new)
    assert saved["supersedes"] == key and saved["title"] == "T" and saved["group_key"] == "g"
    assert (store.root / "candidates" / new / "rules.md").read_text().endswith("new command.\n")
    latest = decide.decisions(store)
    assert latest[key]["action"] == "stale" and latest[key]["superseded_by"] == new
    assert latest[new]["action"] == "accept"
    assert decide.accept(store, key)["status"] == "unchanged"      # the old id follows the chain
    assert decide.open_proposals(store) == []


@pytest.mark.parametrize("change", ["span", "managed", "legacy"])
def test_accept_marks_stale_when_the_span_cannot_be_found_again(store, tmp_path, change):
    text = "<!-- BEGIN x -->\nold\n<!-- END x -->\nend\n" if change == "managed" \
        else "Use the old command.\n"
    source, key = proposal(store, tmp_path, text=text, group_key="g")
    if change == "legacy":      # a proposal saved before spans were recorded
        value = store.get("proposals", key)
        del value["span"]
        store.path("proposals", key).unlink()
        key = store.put("proposals", value)
    (source / "rules.md").write_text({"span": "Use the other command.\n",
                                      "managed": text.replace("end", "end2"),
                                      "legacy": "Use the old command. More.\n"}[change])
    result = decide.accept(store, key)
    assert result["status"] == "stale"
    assert decide.decisions(store)[key]["action"] == "stale"
    assert decide.group_status(store).get("g") is None          # stale does not block the group
    with pytest.raises(AutError, match="already marked stale"):
        decide.accept(store, key)


def test_conflicting_decisions_are_refused(store, tmp_path):
    _, key = proposal(store, tmp_path)
    decide.accept(store, key)
    with pytest.raises(AutError, match="already accepted"):
        decide.reject(store, key, "no")
    _, other = proposal(store, tmp_path, text="Use the old way.\n")
    decide.reject(store, other, "too broad")
    assert decide.reject(store, other, "again")["status"] == "unchanged"
    with pytest.raises(AutError, match="already rejected"):
        decide.accept(store, other)


def test_two_concurrent_accepts_write_one_decision(store, tmp_path):
    _, key = proposal(store, tmp_path)
    results = []
    threads = [threading.Thread(target=lambda: results.append(decide.accept(store, key)["status"]))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["accepted", "unchanged", "unchanged", "unchanged"]
    assert len(store.keys("decisions")) == 1


def test_reject_blocks_the_group_and_removes_only_the_store_candidate(store, tmp_path):
    source, key = proposal(store, tmp_path, group_key="g")
    candidate = store.root / "candidates" / key
    stage(store, key, candidate)
    (candidate / ".git").mkdir()
    (candidate / ".git" / "obj").write_text("x")
    (candidate / ".git" / "obj").chmod(0o444)       # git objects are read-only
    result = decide.reject(store, key, "  wrong layer  ")
    assert result["candidate_removed"] and not candidate.exists()
    assert decide.decisions(store)[key]["reason"] == "wrong layer"
    assert decide.group_status(store)["g"].startswith("rejected on ")
    _, other = proposal(store, tmp_path, text="Use the old way.\n")
    outside = tmp_path / "operator-candidate"
    stage(store, other, outside)
    assert decide.reject(store, other, "no")["candidate_removed"] is False
    assert (outside / "rules.md").exists()
    with pytest.raises(AutError, match="reason"):
        decide.reject(store, other, "   ")
    with pytest.raises(AutError, match="500"):
        decide.reject(store, other, "x" * 501)


def test_reject_needs_a_reason_on_the_command_line(store, tmp_path, capsys):
    _, key = proposal(store, tmp_path)
    with pytest.raises(SystemExit) as stop:
        cli.main(["improve", "--root", str(store.root), "inbox", "reject", key[:8]])
    assert stop.value.code == 2
    assert cli.main(["improve", "--root", str(store.root), "inbox", "reject", key[:8],
                     "--reason", "no"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


def test_resolve_takes_a_unique_prefix_and_lists_an_ambiguous_one(store, tmp_path, monkeypatch):
    _, key = proposal(store, tmp_path)
    assert decide.resolve(store, key[:6].upper()) == key
    for bad in (key[:5], "zzzzzz", "abc def"):
        with pytest.raises(AutError):
            decide.resolve(store, bad)
    keys = ["abcdef01" + "0" * 56, "abcdef02" + "0" * 56]
    monkeypatch.setattr(store, "keys", lambda kind: keys)
    with pytest.raises(AutError, match="abcdef010000, abcdef020000"):
        decide.resolve(store, "abcdef")
    assert decide.short_id(store, keys[0]) == "abcdef01"


def test_adopt_records_a_manual_change_once(store, tmp_path, capsys):
    argv = ["improve", "--root", str(store.root), "adopt",
            "--title", "Run tests with CI's pytest entrypoint, not python -m pytest",
            "--commit", "EF03DF5", "--pr", "53", "--date", "2026-11-14",
            "--target", str(tmp_path / "AGENTS.md"), "--rubric", "avoidable_recovery",
            "--kind", "instruction", "--note", "Command-check trial 6/6 vs 0/6."]
    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "adopted" and "sink" in first["notice"]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"
    (key,) = store.keys("adoptions")
    saved = store.get("adoptions", key)
    assert saved["commit"] == "ef03df5" and saved["pr"] == 53 and saved["benefit_claim"] is False
    assert saved["adopted_at"] == "2026-11-14" and saved["proposal_id"] is None
    journal = (store.root / "events.jsonl").read_text().splitlines()
    assert sum(json.loads(row)["kind"] == "adoption" for row in journal) == 1
    assert "2026-11-14: Run tests with CI" in (store.root / "inbox.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("change", [{"title": "x" * 121}, {"title": "a\nb"}, {"title": " "},
                                    {"commit": "xyz1234"}, {"pr": 0}, {"adopted_at": "14/11"},
                                    {"rubric_ids": ["Bad-Id"]}])
def test_adopt_refuses_bad_fields(store, change):
    fields = {"title": "T", "commit": "ef03df5"} | change
    with pytest.raises(AutError):
        decide.adopt(store, **fields)


def test_adopting_a_proposal_needs_an_accept_and_copies_its_fields(store, tmp_path):
    source, key = proposal(store, tmp_path, title="Fix it", group_key="g", kind="skill")
    with pytest.raises(AutError, match="accept"):
        decide.adopt(store, key, commit="abc1234")
    decide.accept(store, key)
    result = decide.adopt(store, key, commit="abc1234", pr=7)
    saved = store.get("adoptions", result["adoption_id"])
    assert saved["title"] == "Fix it" and saved["kind"] == "skill" and saved["group_key"] == "g"
    assert saved["targets"] == [str((source / "rules.md").absolute())]
    assert decide.group_status(store)["g"] == "adopted"


def test_an_accepted_group_waits_for_its_merge_and_is_not_open(store, tmp_path):
    _, key = proposal(store, tmp_path, group_key="g")
    decide.accept(store, key)
    assert decide.group_status(store)["g"] == "accepted"


def test_an_unreadable_target_is_not_marked_stale(store, tmp_path, monkeypatch):
    _, key = proposal(store, tmp_path, group_key="g")

    def locked(path):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(decide, "file_digest", locked)
    with pytest.raises(AutError, match="cannot be read now"):
        decide.accept(store, key)
    assert store.keys("decisions") == []


def test_a_stale_proposal_can_still_be_rejected(store, tmp_path):
    source, key = proposal(store, tmp_path, group_key="g")
    (source / "rules.md").write_text("Something else.\n")
    assert decide.accept(store, key)["status"] == "stale"
    assert decide.reject(store, key, "stop drafting this")["status"] == "rejected"
    assert decide.group_status(store)["g"].startswith("rejected on ")


def test_the_unedited_placeholder_is_not_a_reason(store, tmp_path):
    _, key = proposal(store, tmp_path)
    with pytest.raises(AutError, match="reason"):
        decide.reject(store, key, "…")


def test_rejecting_a_superseded_id_removes_every_store_candidate(store, tmp_path):
    source, old = proposal(store, tmp_path)
    stage(store, old, store.root / "candidates" / old)
    (source / "rules.md").write_text("Intro.\nUse the old command.\n")
    new = decide._reprepare(store, old, store.get("proposals", old), source / "rules.md")
    stage(store, new, store.root / "candidates" / new)
    decide._record(store, old, store.get("proposals", old), "stale", superseded_by=new)
    result = decide.reject(store, old, "no")         # a crash came before the accept
    assert result["proposal_id"] == new and result["candidate_removed"]
    assert not (store.root / "candidates" / old).exists()
    assert not (store.root / "candidates" / new).exists()


def test_a_drafted_title_is_never_adopted_without_a_human_title(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "rules.md").write_text("Use old.\n")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    key = prepare(store, case, source, source / "rules.md", "old", "new",
                  authority="model_drafted_proposal", extra={"title": "Model wrote this"})
    decide.accept(store, key)
    with pytest.raises(AutError, match="title"):
        decide.adopt(store, key, commit="abc1234")
    result = decide.adopt(store, key, commit="abc1234", title="Human title")
    assert store.get("adoptions", result["adoption_id"])["title"] == "Human title"


def test_adopting_again_on_a_later_day_changes_nothing(store, tmp_path):
    first = decide.adopt(store, title="T", commit="abc1234", adopted_at="2026-11-20")
    again = decide.adopt(store, title="T", commit="abc1234", adopted_at="2026-11-21")
    assert again == first | {"status": "unchanged"}
    assert len(store.keys("adoptions")) == 1


def test_a_retried_accept_never_overrides_a_reject_of_the_superseding_proposal(store, tmp_path):
    source, old = proposal(store, tmp_path)
    (source / "rules.md").write_text("Intro.\nUse the old command.\n")
    new = decide._reprepare(store, old, store.get("proposals", old), source / "rules.md")
    decide.reject(store, new, "not this")           # a crash came before the old id was marked
    with pytest.raises(AutError, match="already rejected"):
        decide.accept(store, old)
    assert decide.decisions(store)[new]["action"] == "reject"
    assert decide.decisions(store)[old]["superseded_by"] == new


def clone_candidate(store, tmp_path, source, target="rules.md"):
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    key = prepare(store, case, source, source / target, "old", "new")
    candidate = tmp_path / "candidate"
    subprocess.run(["git", "clone", "-q", "--shared", str(source), str(candidate)], check=True)
    return key, candidate


@pytest.mark.skipif(sys.platform == "win32", reason="the filter command is a POSIX shell line")
def test_accept_runs_no_clean_filter_from_the_candidate(store, tmp_path):
    source = repo(tmp_path)
    key, candidate = clone_candidate(store, tmp_path, source)
    (candidate / ".gitattributes").write_text("* filter=evil\n")
    git(candidate, "config", "filter.evil.clean", f"touch {source / 'pwned'}; cat")
    stage(store, key, candidate)
    assert decide.accept(store, key)["candidate_commit"]
    assert not (source / "pwned").exists()


def test_a_worktree_of_another_clone_gets_no_commit(store, tmp_path):
    source = repo(tmp_path)
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    key = prepare(store, case, source, source / "rules.md", "old", "new")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(source), str(other)], check=True)
    candidate = tmp_path / "wt"
    git(other, "worktree", "add", "-q", "--detach", str(candidate))
    stage(store, key, candidate)
    assert decide.accept(store, key)["candidate_commit"] is None
    assert "aut/" not in git(other, "branch", "--list", "--all")


def test_the_commit_holds_only_the_literal_target_path(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "rules[1].md").write_text("Use the old command.\n")
    (source / "rules1.md").write_text("Other.\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    key, candidate = clone_candidate(store, tmp_path, source, "rules[1].md")
    (candidate / "rules1.md").write_text("Unrelated local edit.\n")
    stage(store, key, candidate)
    decide.accept(store, key)
    assert git(candidate, "diff", "--name-only", "main", f"aut/improve-{key[:8]}") == "rules[1].md"


def test_a_crash_before_the_journal_row_is_repaired_on_retry(store, tmp_path):
    _, key = proposal(store, tmp_path)
    store.put("decisions", {"version": 1, "proposal_id": key, "group_key": None,
                            "action": "accept", "reason": None, "note": None,
                            "candidate_commit": None, "decided_at": "2026-11-21T00:00:00+00:00"})
    assert decide.accept(store, key)["status"] == "unchanged"
    decide.accept(store, key)
    rows = [json.loads(r) for r in (store.root / "events.jsonl").read_text().splitlines()]
    assert sum(row["kind"] == "decision" for row in rows) == 1


def test_the_adopt_step_keeps_a_custom_store(store, tmp_path):
    _, key = proposal(store, tmp_path)
    steps = decide.accept(store, key)["next"]
    assert f"aut improve --root {store.root} adopt {key}" in steps[-1]
