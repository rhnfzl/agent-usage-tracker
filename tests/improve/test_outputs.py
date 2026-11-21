"""Output evidence uses disposable Git repositories and fake judges only."""

import json
import re
import subprocess
from dataclasses import replace

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import judge, outputs, rubric
from agent_usage_tracker.improve.store import Store
from .test_judge_queue import Backend


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo, text="hello\n"):
    (repo / "example.txt").write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Change example")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.test")
    first = commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", first)
    return repo


def captured(store, repo, sha, *, command="git commit -m Change", suffix=""):
    events = [
        {"id": "p", "kind": "prompt", "text": "Change the example" + suffix},
        {"id": "c", "kind": "call", "call_id": "x", "text": json.dumps({"command": command})},
        {"id": "r", "kind": "result", "call_id": "x", "status": "success",
         "text": f"[feature {sha[:7]}] Change example"},
        {"id": "a", "kind": "reply", "text": "Changed the example"},
        {"id": "b", "kind": "boundary", "text": "turn_complete"}]
    sid = store.put("sessions", {"agent": "codex", "origin": "interactive",
                                "session_id": "fixture" + suffix,
                                "cwd": str(repo), "coverage": {"complete": True},
                                "events": events})
    row = {"session_snapshot": sid, "trigger": "control", "events": events,
           "coverage": {"complete": True}, "omitted_task_events": 0}
    return store.put("cases", row)


def test_commit_parsing_uses_matching_results_in_only_this_attempt(tmp_path):
    store = Store(tmp_path / "private")
    key = captured(store, tmp_path, "abc1234")
    case = store.get("cases", key)
    session = store.get("sessions", case["session_snapshot"])
    assert outputs.commits_in(case, session) == ["abc1234"]
    other = [{"id": "oldp", "kind": "prompt", "text": "Earlier task"},
             {"id": "oldc", "kind": "call", "call_id": "old", "text": "git commit"},
             {"id": "oldr", "kind": "result", "call_id": "old", "text": "[main def1234] Old"},
             {"id": "oldb", "kind": "boundary", "text": "turn_complete"}]
    assert outputs.commits_in(case, {**session, "events": other + session["events"]}) == ["abc1234"]
    for command in ("git commit --dry-run", "echo 'git commit'", "git status"):
        session["events"][1]["text"] = command
        case["events"] = session["events"]
        assert outputs.commits_in(case, session) == []
    session["events"][1]["text"] = "git commit"
    session["events"][2]["call_id"] = "different"
    assert outputs.commits_in(case, session) == []


def test_commit_parser_respects_printed_before_and_error(tmp_path):
    store = Store(tmp_path / "private")
    key = captured(store, tmp_path, "abc1234", command="echo earlier && git commit")
    case = store.get("cases", key)
    session = store.get("sessions", case["session_snapshot"])
    case["events"] = session["events"]
    result = session["events"][2]
    result["text"] = "def1234 unrelated earlier output\nok abc1234"
    assert outputs.commits_in(case, session) == ["abc1234"]
    result["status"] = "error"
    assert outputs.commits_in(case, session) == []


def test_attach_merged_open_reverted_and_unreachable(repo, tmp_path):
    store = Store(tmp_path / "private")
    sha = git(repo, "rev-parse", "HEAD")
    key = captured(store, repo, sha)
    original = store.get("cases", key)
    attached = outputs.attach(store, key)
    assert outputs.attach(store, key) == attached
    assert store.get("case_outputs", attached)["commits"] == [
        {"sha7": sha[:7], "state": "merged", "files_changed": 1, "insertions": 1, "deletions": 0}]
    git(repo, "checkout", "-qb", "feature")
    sha = commit(repo, "new content\n")
    branch = captured(store, repo, sha)
    assert store.get("case_outputs", outputs.attach(store, branch))["commits"][0]["state"] == "open"
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--ff-only", "feature")
    git(repo, "revert", "--no-edit", sha)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert store.get("case_outputs", outputs.attach(store, branch))["commits"][0]["state"] == "reverted"
    git(repo, "checkout", "--detach", "-q")
    detached = commit(repo, "detached\n")
    git(repo, "checkout", "-q", "main")
    key2 = captured(store, repo, detached)
    assert store.get("case_outputs", outputs.attach(store, key2))["commits"][0]["state"] == "unreachable"
    assert store.get("cases", key) == original


def test_redacted_clipped_patch_and_pr_numbers(repo, tmp_path):
    git(repo, "checkout", "-qb", "feature")
    sha = commit(repo, "token=private-token\n" + "a" * 40 + "\n" + "line of code\n" * 500)
    store = Store(tmp_path / "private")
    key = captured(store, repo, sha)
    case = store.get("cases", key)
    session = store.get("sessions", case["session_snapshot"])
    more = [{"id": "prc", "kind": "call", "call_id": "pr", "text": "gh pr create"},
            {"id": "prr", "kind": "result", "call_id": "pr",
             "text": "https://github.com/example/repo/pull/42"}]
    session["events"][-1:-1] = more
    case["events"] = session["events"]
    case["session_snapshot"] = store.put("sessions", session)
    key = store.put("cases", case)
    row = store.get("case_outputs", outputs.attach(store, key))
    assert row["patch_truncated"] and len(row["patch"]) == 1500
    assert len(row["diff_stat"]) <= 800 and row["pr_numbers"] == [42]
    assert "private-token" not in row["patch"] and "[redacted]" in row["patch"]
    assert not re.search(r"[a-f0-9]{32,}", row["patch"] + row["diff_stat"])


def test_missing_repo_and_relative_cwd_do_not_read_aut_checkout(repo, tmp_path):
    store = Store(tmp_path / "private")
    key = captured(store, repo, git(repo, "rev-parse", "HEAD"))
    repo.rename(tmp_path / "unavailable-repo")
    assert outputs.attach(store, key) is None
    assert outputs.attach(store, captured(store, ".", "abc1234")) is None
    assert store.keys("case_outputs") == []


def test_refresh_appends_and_queues_changed_state(repo, tmp_path):
    store = Store(tmp_path / "private")
    git(repo, "checkout", "-qb", "feature")
    sha = commit(repo, "branch\n")
    key = captured(store, repo, sha)
    first = outputs.attach(store, key)
    assert outputs.refresh(store) == []
    assert judge.queued(store, name="merge_queue") == []
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert outputs.refresh(store) == [key]
    second = outputs.latest(store)[key][0]
    assert second != first and len(store.keys("case_outputs")) == 2
    assert store.get("case_outputs", first)["commits"][0]["state"] == "open"
    assert judge.queued(store, name="merge_queue") == [key]
    assert outputs.refresh(store) == []


def configure(monkeypatch, tmp_path, items):
    monkeypatch.setattr(judge, "_rubrics", lambda: items)
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")


def test_code_quality_activation_cache_snapshot_and_merge_trigger(repo, tmp_path, monkeypatch):
    items = rubric.load()
    configure(monkeypatch, tmp_path, items)
    store = Store(tmp_path / "private")
    git(repo, "checkout", "-qb", "feature")
    sha = commit(repo, "scoped\n")
    case_id = captured(store, repo, sha)
    case = store.get("cases", case_id)
    monkeypatch.setattr(judge, "cases", lambda *a, **kw: [{**case, "case_id": case_id}])
    audit = store.put("audits", {"session_ids": [case["session_snapshot"]]})
    calls, requests = [], []

    class Recording(Backend):
        def distribution(self, request):
            requests.append(request)
            return super().distribution(request)

    def backend(_):
        return Recording(calls)
    first = judge.diagnose(store, audit, models=("fixture",), backend_for=backend)
    assert any(task == "improve_code_quality" for _, task in calls)
    quality = next(r for r in requests if r.task == "improve_code_quality")
    out = quality.facts["untrusted_outputs"]
    assert out["commits"][0]["state"] == "open"
    assert [e["kind"] for e in quality.facts["untrusted_historical_evidence"]["events"]] == ["prompt", "reply"]
    assert not re.search(r"[a-f0-9]{32,}", json.dumps(out))
    again = judge.diagnose(store, audit, models=("fixture",), backend_for=backend)
    assert again["calls"] == 0
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert outputs.refresh(store) == [case_id]
    second = judge.diagnose(store, audit, models=("fixture",), backend_for=backend)
    assert second["calls"] == 2
    rows = [store.get("judgments", key) for key in second["judgment_ids"]]
    merged = [r for r in rows if r["trigger"] == "merge"]
    assert len(merged) == 1 and merged[0]["rubric"] == "code_quality"
    original = next(store.get("judgments", key) for key in first["judgment_ids"]
                    if store.get("judgments", key)["rubric"] == "code_quality")
    assert original["evaluation_key"] != merged[0]["evaluation_key"]
    assert original["outputs_id"] != merged[0]["outputs_id"]
    assert judge.queued(store, name="merge_queue") == []
    assert store.get("cases", case_id)["trigger"] == "control"


def test_merge_queue_budget_reuse_bound_and_concurrent_add(repo, tmp_path, monkeypatch):
    quality = next(r for r in rubric.load() if r.id == "code_quality")
    configure(monkeypatch, tmp_path, [replace(quality, max_cases=4)])
    store = Store(tmp_path / "private")
    sha = git(repo, "rev-parse", "HEAD")
    keys = [captured(store, repo, sha, suffix=str(i)) for i in range(6)]
    for key in keys:
        outputs.attach(store, key)
    judge.update_queue(store, add=keys[:5], name="merge_queue")
    audit = store.put("audits", {"session_ids": []})
    calls = []
    def backend(name, callback=lambda: None):
        instance = Backend(calls, callback)
        instance.model_id = name
        return instance
    def callback():
        judge.update_queue(store, add=keys[5:], name="merge_queue")
    report = judge.diagnose(store, audit, models=("one", "two"), max_calls=1,
                            backend_for=lambda name: backend(name, callback))
    assert report["complete"] and report["calls"] == 1 and report["optional_total"] == 4
    assert set(judge.queued(store, name="merge_queue")) == set(keys)
    report = judge.diagnose(store, audit, models=("one", "two"),
                            backend_for=backend)
    assert report["calls"] == 7 and len(judge.queued(store, name="merge_queue")) == 2
    finished = next(key for key in keys if key not in judge.queued(store, name="merge_queue"))
    judge.update_queue(store, add=[finished], name="merge_queue")
    report = judge.diagnose(store, audit, models=("one", "two"),
                            backend_for=backend)
    assert report["calls"] == 4 and judge.queued(store, name="merge_queue") == []


def test_new_snapshot_cannot_be_dequeued_by_old_judgment(repo, tmp_path):
    store = Store(tmp_path / "private")
    git(repo, "checkout", "-qb", "feature")
    sha = commit(repo, "change\n")
    key = captured(store, repo, sha)
    old = outputs.attach(store, key)
    judge.update_queue(store, add=[key], name="merge_queue")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    outputs.refresh(store)
    outputs.consume(store, {key: old})
    assert judge.queued(store, name="merge_queue") == [key]
    outputs.consume(store, {key: outputs.latest(store)[key][0]})
    assert judge.queued(store, name="merge_queue") == []


def test_output_limit_and_clipping_abstain_without_inference(repo, tmp_path, monkeypatch):
    quality = next(r for r in rubric.load() if r.id == "code_quality")
    configure(monkeypatch, tmp_path, [quality])
    store = Store(tmp_path / "private")
    sha = commit(repo, "line\n" * 700)
    key = captured(store, repo, sha)
    monkeypatch.setattr(judge, "cases", lambda *a, **kw: [{**store.get("cases", key), "case_id": key}])
    audit = store.put("audits", {"session_ids": []})
    first = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend([]))
    assert first["calls"] == 0 and first["verdicts"]["unknown"] == 1
    configure(monkeypatch, tmp_path, [replace(quality, max_input_chars=1)])
    second = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend([]))
    assert second["calls"] == 0
    assert store.get("judgments", second["judgment_ids"][0])["review_skipped"] == "review_input_limit"


def test_total_attachment_cap_and_failure_are_explicit(tmp_path, monkeypatch):
    quality = next(r for r in rubric.load() if r.id == "code_quality")
    configure(monkeypatch, tmp_path, [quality])
    store = Store(tmp_path / "private")
    keys = [captured(store, tmp_path, "abc1234", suffix=str(i)) for i in range(5)]
    monkeypatch.setattr(judge, "cases", lambda *a, **kw: [
        {**store.get("cases", key), "case_id": key} for key in keys])
    clock, budgets = [100.0], []
    monkeypatch.setattr(judge.time, "monotonic", lambda: clock[0])
    def slow(store, key, *, budget_s):
        budgets.append(budget_s)
        clock[0] += budget_s
        return None
    monkeypatch.setattr(outputs, "attach", slow)
    audit = store.put("audits", {"session_ids": []})
    report = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend([]))
    assert budgets == [10, 10, 10] and report["outputs_skipped"] == 5
    assert report["complete"] and report["calls"] == 0


def test_bounded_git_output_and_timeout(repo, monkeypatch):
    import sys
    import time

    monkeypatch.setattr(outputs, "git_argv", lambda *args: [
        sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"])
    text, clipped = outputs._git(str(repo), time.monotonic() + 5, "show")
    assert len(text) == outputs.MAX_BYTES and clipped
    monkeypatch.setattr(outputs, "git_argv", lambda *args: [
        sys.executable, "-c", "import time; time.sleep(10)"])
    start = time.monotonic()
    with pytest.raises(AutError, match="time limit"):
        outputs._git(str(repo), start + .1, "show")
    assert time.monotonic() - start < 3


def test_deleted_repo_is_explicit_and_saved_merge_evidence_still_works(repo, tmp_path, monkeypatch):
    quality = next(r for r in rubric.load() if r.id == "code_quality")
    configure(monkeypatch, tmp_path, [quality])
    store = Store(tmp_path / "private")
    key = captured(store, repo, git(repo, "rev-parse", "HEAD"))
    outputs.attach(store, key)
    judge.update_queue(store, add=[key], name="merge_queue")
    repo.rename(tmp_path / "unavailable-repo")
    audit = store.put("audits", {"session_ids": []})
    report = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend([]))
    assert report["outputs_skipped"] == 1 and report["calls"] == 1
    assert report["complete"] and judge.queued(store, name="merge_queue") == []


def test_refresh_deleted_repo_keeps_previous_snapshot_and_records_skip(repo, tmp_path):
    store = Store(tmp_path / "private")
    git(repo, "checkout", "-qb", "feature")
    key = captured(store, repo, commit(repo, "open\n"))
    before = outputs.attach(store, key)
    repo.rename(tmp_path / "unavailable-repo")
    assert outputs.refresh(store) == []
    assert outputs.latest(store)[key][0] == before
    rows = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "outputs_skipped" and rows[-1]["reason"] == "refresh_unavailable"


def test_merge_queue_real_concurrent_add_is_preserved(tmp_path):
    from threading import Barrier, Thread

    store = Store(tmp_path / "private")
    old, new = "a" * 64, "b" * 64
    judge.update_queue(store, name="merge_queue", add=[old])
    gate = Barrier(2)
    def add():
        gate.wait()
        judge.update_queue(store, name="merge_queue", add=[new])
    thread = Thread(target=add)
    thread.start()
    gate.wait()
    judge.update_queue(store, name="merge_queue", remove=[old])
    thread.join(5)
    assert not thread.is_alive() and judge.queued(store, name="merge_queue") == [new]


def test_empty_commit_does_not_activate_code_quality(repo, tmp_path, monkeypatch):
    quality = next(r for r in rubric.load() if r.id == "code_quality")
    configure(monkeypatch, tmp_path, [quality])
    git(repo, "commit", "--allow-empty", "-qm", "Empty")
    store = Store(tmp_path / "private")
    key = captured(store, repo, git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(judge, "cases", lambda *a, **kw: [{**store.get("cases", key), "case_id": key}])
    audit = store.put("audits", {"session_ids": []})
    report = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend([]))
    assert report["optional_total"] == report["calls"] == 0


@pytest.mark.parametrize("command_key,structured", [("cmd", False), ("command", True), ("cmd", True)])
def test_native_codex_capture_parses_command_and_result_envelopes(tmp_path, command_key, structured):
    from agent_usage_tracker.improve.evidence import read_session
    from .test_evidence import write_log

    result = "[main abc1234] Commit the change"
    if structured:
        result = json.dumps({"exit_code": 0, "output": result})
    path = write_log(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "native", "source": "cli", "cwd": str(tmp_path)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Commit the change"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
         "call_id": "call", "arguments": json.dumps({command_key: "git commit -m Change"})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call",
         "output": result}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Committed"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}}])
    store = Store(tmp_path / "private")
    session = read_session(path, "codex")
    sid = store.put("sessions", session)
    audit = store.put("audits", {"session_ids": [sid]})
    [case] = judge.cases(store, audit)
    assert outputs.commits_in(case, session) == ["abc1234"]


def test_failed_enqueue_keeps_old_snapshot_retryable(repo, tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    git(repo, "checkout", "-qb", "feature")
    key = captured(store, repo, commit(repo, "branch\n"))
    old = outputs.attach(store, key)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    original = judge.update_queue
    def fail(*args, **kwargs):
        raise OSError("fixture queue write failure")
    monkeypatch.setattr(judge, "update_queue", fail)
    with pytest.raises(OSError):
        outputs.refresh(store)
    assert outputs.latest(store)[key][0] == old
    monkeypatch.setattr(judge, "update_queue", original)
    assert outputs.refresh(store) == [key]
    assert judge.queued(store, name="merge_queue") == [key]


def test_partial_clone_cannot_fetch_missing_blobs_or_write_packs(repo, tmp_path):
    import time

    remote, clone = tmp_path / "remote.git", tmp_path / "partial"
    subprocess.check_call(["git", "clone", "--quiet", "--bare", str(repo), str(remote)])
    git(remote, "config", "uploadpack.allowFilter", "true")
    subprocess.check_call(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
                           remote.as_uri(), str(clone)])
    packs = list((clone / ".git/objects/pack").glob("*.pack"))
    assert len(packs) == 1
    with pytest.raises(AutError, match="Git read failed"):
        outputs._git(str(clone), time.monotonic() + 5, "show", "HEAD", "--")
    assert list((clone / ".git/objects/pack").glob("*.pack")) == packs


def test_human_output_labels_bind_request_save_queue_and_qualification(repo, tmp_path, monkeypatch):
    from agent_usage_tracker.improve import qualify

    quality = next(r for r in rubric.load() if r.id == "code_quality")
    configure(monkeypatch, tmp_path, [quality])
    store = Store(tmp_path / "private")
    git(repo, "checkout", "-qb", "feature")
    key = captured(store, repo, commit(repo, "branch\n"))
    # The summary is eligible even if unrelated middle events were omitted from the attempt.
    case = store.get("cases", key)
    case["omitted_task_events"] = 2
    key = store.put("cases", case)
    old = outputs.attach(store, key)
    [candidate] = qualify.queue(store, quality)
    assert candidate["case_id"] == key and candidate["outputs_id"] == old
    seen = []
    def serve(task, reqs, *, spec, **kwargs):
        request = reqs[key]
        seen.append(request.facts)
        assert request.facts["untrusted_outputs"]["patch"]
        assert [e["kind"] for e in request.facts["untrusted_historical_evidence"]["events"]] == ["prompt", "reply"]
        spec.save(key, "pass")
        return {}
    monkeypatch.setattr(qualify.gold, "serve", serve)
    # The unmodified case would be incomplete for attempt-input judges.
    # Keep only this case in the human selection, so the duplicate complete fixture is excluded.
    original_cases = qualify._cases
    monkeypatch.setattr(qualify, "_cases", lambda *args: {key: original_cases(*args)[key]})
    qualify.review(store, "code_quality", open_browser=False)
    assert seen and judge.queued(store) == [key]
    audit = store.put("audits", {"session_ids": []})
    judged = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend([]))
    assert judged["calls"] == 1 and judge.queued(store) == []
    assert qualify.run(store, "code_quality")["counts"] == {"reference_pass": 1, "supported_passes": 1, "abstentions": 0,
        "detected_failures": 0, "missed_failures": 0, "false_alarms": 0, "decisions_on_unknown": 0}
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    outputs.refresh(store)
    assert qualify.labels(store, quality) == {}
    before = len(store.keys("human_labels"))
    with pytest.raises(AutError, match="evidence changed"):
        qualify.save(store, quality, key, "fail", outputs_id=old)
    assert len(store.keys("human_labels")) == before
