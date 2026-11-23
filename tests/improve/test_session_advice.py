import json
import shlex
import subprocess
from pathlib import Path

import pytest

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.store import Store

CI = "pytest -q -n 3 --dist worksteal --max-worker-restart=0 --durations=15"
FAILURE = (
    "________________ ERROR collecting tests/test_demo.py _________________\n"
    "ModuleNotFoundError: No module named 'demo'\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!"
)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    git("init")
    workflow = path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(f"jobs:\n  test:\n    steps:\n      - run: {CI}\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "CI")
    return path, git("rev-parse", "HEAD")


def test_replay_uses_only_events_through_the_anchor(repo, tmp_path, capsys):
    project, revision = repo
    store = Store(tmp_path / "private")
    events = [
        {"id": "a" * 32, "kind": "call", "tool": "Bash", "call_id": "1",
         "text": json.dumps({"command": "python -m pytest -q"})},
        {"id": "b" * 32, "kind": "result", "status": "error", "call_id": "1",
         "text": FAILURE + "\nfixture-private-text"},
        {"id": "c" * 32, "kind": "call", "tool": "Bash", "call_id": "2",
         "text": json.dumps({"command": CI})},
        {"id": "d" * 32, "kind": "result", "status": "success", "call_id": "2",
         "text": "23 passed in 0.42s"},
    ]
    # Leave the worktree workflow dirty. Replay uses the selected commit.
    (project / ".github/workflows/ci.yml").write_text("jobs: {}\n", encoding="utf-8")

    def replay(items, anchor):
        session = store.put("sessions", {"agent": "claude_code", "coverage": {}, "events": items})
        audit = store.put("audits", {"session_ids": [session]})
        assert main(["advise", "replay", "--root", str(store.root), "--audit", audit,
                     "--session", session, "--event", anchor, "--project", str(project),
                     "--revision", revision]) == 0
        return json.loads(capsys.readouterr().out)

    advice = replay(events, "b" * 32)
    assert advice["command"] == CI
    assert advice["evidence"]["call_id"] == "a" * 32
    assert advice["evidence"]["event_id"] == "b" * 32
    assert "fixture-private-text" not in json.dumps(advice)
    assert advice["command"] == replay(events[:2], "b" * 32)["command"]
    assert replay(events, "d" * 32)["status"] == "quiet"
    assert replay([events[1], events[0]], "b" * 32)["status"] == "quiet"


def batch(project, *rows, session="session-one"):
    return json.dumps({"hook_event_name": "PostToolBatch", "cwd": str(project),
                       "session_id": session, "tool_calls": [
                           {"tool_name": "Bash", "tool_use_id": identity,
                            "tool_input": {"command": command}, "tool_response": output}
                           for identity, command, output in rows]}).encode()


@pytest.mark.parametrize("mode", ["record", "show"])
def test_hook_records_one_suggestion_then_a_later_retry(repo, tmp_path, mode):
    from agent_usage_tracker.improve.advice_hook import handle_batch, recent

    project, _ = repo
    root = tmp_path / "private"
    initial = batch(project, ("use-one", "python3 -m pytest -q", FAILURE),
                    ("use-parallel", "echo ready", "ready"))
    result = handle_batch(initial, root, project, mode)
    if mode == "show":
        assert result["hookSpecificOutput"]["hookEventName"] == "PostToolBatch"
        assert CI in result["hookSpecificOutput"]["additionalContext"]
        assert "decision" not in result
    else:
        assert result == {}
    assert handle_batch(initial, root, project, mode) == {}
    row, = recent(root)
    assert row["mode"] == mode
    assert row["retry"] == "unknown"
    assert row["acceptance"] == "unknown"
    assert handle_batch(batch(project, ("use-other", CI, "23 passed in 0.42s"),
                              session="another-session"), root, project, mode) == {}
    assert recent(root)[0]["retry"] == "unknown"
    other = batch(project, ("use-alternate", "pytest -q tests", "2 failed, 3 passed in 0.42s"))
    assert handle_batch(other, root, project, mode) == {}
    assert recent(root)[0]["next_test"] == "tests failed"
    assert recent(root)[0]["retry"] == "unknown"
    retry = batch(project, ("use-two", CI, [{"type": "text", "text": "Pytest: 23 passed in 0.42s"}]))
    assert handle_batch(retry, root, project, mode) == {}
    assert recent(root)[0]["retry"] == "passed"
    assert recent(root)[0]["acceptance"] == "unknown"
    assert "session-one" not in json.dumps(recent(root))


def test_hook_skips_noise_and_complex_commands(repo, tmp_path):
    from agent_usage_tracker.improve.advice_hook import handle_batch, recent

    project, _ = repo
    root = tmp_path / "private"
    for command, output in [(CI, FAILURE), ("pytest -q && echo next", FAILURE),
                            ("echo pytest", FAILURE), ("pytest -q", "3 failed in 0.1s"),
                            ("pytest -q", FAILURE.replace("ModuleNotFoundError:", "SyntaxError:"))]:
        assert handle_batch(batch(project, ("use-one", command, output)), root, project, "show") == {}
    assert handle_batch(batch(project, ("use-one", "pytest -q", FAILURE),
                              ("same-batch", CI, "23 passed in 0.42s")), root, project, "show") == {}
    assert handle_batch(b"invalid", root, project, "show") == {}
    assert recent(root) == []


def test_hook_cli_defaults_to_record_only(repo, tmp_path, monkeypatch, capsys):
    from io import BytesIO
    from types import SimpleNamespace

    from agent_usage_tracker.improve.advice_hook import recent

    project, _ = repo
    root = tmp_path / "private"
    failure = batch(project, ("use-one", "pytest -q", FAILURE))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=BytesIO(failure)))
    assert main(["advise", "claude-hook", "--project", str(project), "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert recent(root)[0]["mode"] == "record"


def test_workflow_lookup_requires_an_unambiguous_inline_ci_step(repo):
    from agent_usage_tracker.improve.advice import committed_ci

    project, _ = repo
    path = project / ".github/workflows/ci.yml"
    path.write_text(f"jobs:\n  test:\n    steps:\n      - run: |\n          {CI}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-am", "Unsupported step"],
                   check=True, capture_output=True)
    assert committed_ci(project, "HEAD") is None


def test_overview_reads_hook_receipt_after_startup(repo, tmp_path, monkeypatch):
    from agent_usage_tracker import overview_cli
    from agent_usage_tracker.improve.advice_hook import handle_batch

    root = tmp_path / "private"
    project, _ = repo
    monkeypatch.setenv("AUT_ADVICE_ROOT", str(root))
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(tmp_path / "unused"))
    monkeypatch.setattr(overview_cli, "build_report", lambda cfg: {"collectors": []})
    monkeypatch.setattr(overview_cli, "build_summary", lambda *a, **kw: {
        "window": {"since": "2026-11-23", "until": "2026-11-24", "timezone": "UTC"},
        "group_by": "agent", "groups": [], "sources": [],
        "totals": {"events": 0, "tokens": {"total_tokens": 0}, "unknown_events": {"total_tokens": 0}},
    })
    monkeypatch.setattr(overview_cli.overview, "_pricing", lambda _: "")

    def serve(token, get, post, notice, *, open_browser, on_ready):
        assert b"Session advice" not in get("")
        handle_batch(batch(project, ("start", "pytest -q", FAILURE + "\nprivate example")), root, project)
        handle_batch(batch(project, ("retry", CI, "23 passed in 0.42s")), root, project)
        page = get("")
        assert b"Session advice" in page
        assert CI.encode() in page
        assert b"Reported passed" in page
        assert b"Unknown" in page
        assert b"private example" not in page
        assert get("/advice") is None

        from agent_usage_tracker.improve.advice_hook import recent

        observation = recent(root)[0]["trigger_ref"][:12]
        assert main(["advise", "acceptance", observation, "--result", "accepted"]) == 0
        assert b"Accepted (operator review)" in get("")
        assert main(["advise", "acceptance", observation, "--result", "rejected"]) == 1
        assert recent(root)[0]["acceptance"] == "accepted"

    monkeypatch.setattr(overview_cli.local_page, "serve", serve)
    assert main(["overview", "--no-browser"]) == 0


@pytest.mark.parametrize("client", ["codex", "cursor", "pi"])
def test_other_clients_share_advice_and_use_native_response(repo, tmp_path, client):
    from agent_usage_tracker.improve.advice_hook import handle_batch, recent

    project, _ = repo
    root = tmp_path / "private"

    def payload(identity, command, output, failure=False):
        fields = {"cwd": str(project), "tool_use_id": identity, "tool_input": {"command": command}}
        if client in {"codex", "pi"}:
            fields.update(hook_event_name="PostToolUse" if client == "codex" else "tool_result",
                          session_id="same", tool_name="Bash",
                          tool_response=output)
        elif failure:
            fields.update(hook_event_name="postToolUseFailure", conversation_id="same",
                          tool_name="Shell", error_message=output, failure_type="error")
        else:
            fields.update(hook_event_name="postToolUse", conversation_id="same",
                          tool_name="Shell", tool_output=json.dumps({"output": output, "exitCode": 0}))
        return json.dumps(fields).encode()

    response = handle_batch(payload("fail", "pytest -q", FAILURE, True), root, None, "show", client)
    context = (response["hookSpecificOutput"]["additionalContext"] if client == "codex"
               else response["additional_context"])
    assert CI in context
    handle_batch(payload("retry", CI, "23 passed in 0.42s"), root, None, "show", client)
    row, = recent(root)
    assert row["retry"] == "passed"


def test_global_setup_preserves_existing_hooks_and_reuses_the_executable(tmp_path, monkeypatch):
    from agent_usage_tracker.improve.advice_setup import configure

    home = tmp_path / "user"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    original = {"other": True, "hooks": {"PostToolBatch": [{"hooks": [{"command": "keep"}]}]}}
    settings.write_text(json.dumps(original), encoding="utf-8")
    exe = tmp_path / "local bin" / "aut"
    monkeypatch.setattr("agent_usage_tracker.improve.advice_setup.shutil.which", lambda _: str(exe))
    preview = configure(home, exe, apply=False, mode="record")
    assert {item["client"] for item in preview} == {"claude", "codex", "cursor", "pi"}
    assert json.loads(settings.read_text()) == original
    applied = configure(home, exe, apply=True, mode="record")
    for item in applied:
        assert item["status"] == "installed"
        written = Path(item["path"]).read_text(encoding="utf-8")
        if item["client"] == "pi":
            assert f"const executable = {json.dumps(str(exe))};" in written
            continue
        for entries in json.loads(written)["hooks"].values():
            entry = entries[-1]
            command = entry["command"] if item["client"] == "cursor" else entry["hooks"][0]["command"]
            assert shlex.split(command)[0] == str(exe)
    assert json.loads(settings.read_text())["hooks"]["PostToolBatch"][0] == original["hooks"]["PostToolBatch"][0]
    assert all(item["status"] == "unchanged" for item in configure(home, exe, apply=True, mode="record"))
    assert all(item["status"] == "installed" for item in configure(home, exe, apply=True, mode="show"))
    assert len(json.loads(settings.read_text())["hooks"]["PostToolBatch"]) == 2
