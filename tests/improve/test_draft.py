"""Drafting uses synthetic evidence and fake models only."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import draft
from agent_usage_tracker.improve.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = home / "repo"
    repo.mkdir()
    target = repo / "AGENTS.md"
    target.write_text("Use old command.\npassword=private-value\nKeep this.\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.test", "commit", "-qm", "initial"], check=True)
    store = Store(tmp_path / "private")
    session = store.put("sessions", {"cwd": str(repo), "agent": "pi", "events": []})
    case = store.put("cases", {"session_snapshot": session,
                              "events": [{"text": "EVENT_SENTINEL", "kind": "result"}]})
    judgment = store.put("judgments", {"case_id": case,
                                      "review": {"rationale": "token=private-rationale"}})
    group = {"group_key": "a" * 64, "recurring": True, "case_ids": [case],
             "judgment_ids": [judgment], "signals": {"cause": "instruction",
             "rubric": "avoidable_recovery"}, "distinct_tasks": 3,
             "projects": {"repo": 3}, "audit_incidents": 5}
    key = store.put("group_reports", {"groups": [group]})
    store.append("groups", report_id=key)
    monkeypatch.setattr(draft.adapters, "discover", lambda *a, **kw: [
        {"cli": "pi", "available": True, "model": "fake", "quota": None}])
    return home, repo, target, store, group


def answer(**changes):
    return json.dumps({"target": "target_1", "before": "old command", "after": "new command",
                       "title": "Use supported command", "cause": "Old instructions",
                       "expected_effect": "Avoid the missing command", "rollback_note": "Revert",
                       "rubric_ids": ["avoidable_recovery"], **changes})


def test_packet_privacy_exact_sent_record_and_source_git_immutability(setup, monkeypatch):
    home, repo, target, store, group = setup
    original = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    sent = []

    def invoke(adapter, prompt, **kw):
        sent.append(prompt)
        assert "EVENT_SENTINEL" not in prompt
        assert "private-value" not in prompt and "private-rationale" not in prompt
        assert str(home) not in prompt
        return answer(), None
    monkeypatch.setattr(draft.adapters, "invoke", invoke)
    result = draft.run(store, home=home)
    assert len(result["proposals"]) == 1
    key = result["proposals"][0]["proposal_id"]
    saved = store.get("proposals", key)
    assert saved["authority"] == "model_drafted_proposal"
    assert saved["group_key"] == group["group_key"]
    assert saved["drafter"]["cost_usd"] is None
    assert store.get("draft_packets", saved["drafter"]["packet_id"])["prompt"] == sent[0]
    assert (store.root / "candidates" / key / target.name).read_text().startswith("Use new")
    assert original == {str(p.relative_to(repo)): p.read_bytes()
                        for p in repo.rglob("*") if p.is_file()}


def test_retry_nonunique_then_valid_and_dirty_target_not_staged(setup, monkeypatch):
    home, repo, target, store, group = setup
    target.write_text(target.read_text() + "Dirty\n")
    replies = iter([answer(before="command", after="command"), answer()])
    calls = []

    def invoke(adapter, prompt, **kw):
        calls.append(prompt)
        return next(replies), .01
    monkeypatch.setattr(draft.adapters, "invoke", invoke)
    result = draft.run(store, home=home)
    assert len(calls) == 2 and "validation_error" in calls[1]
    assert result["proposals"][0]["staged"] is False
    assert "candidate contains unrelated changes" in result["proposals"][0]["stage_error"]


@pytest.mark.parametrize("change", [
    {"target": "/outside"}, {"before": "password=private-value", "after": "changed"},
    {"before": "command.\npassword=[redacted]\nKeep", "after": "changed"},
    {"after": "password=new-secret"}, {"after": "x" * 1220}, {"title": "x" * 81},
    {"rubric_ids": ["invented"]}, {"extra": True}, {"before": "missing"},
    {"rollback_note": None}, {"before": ""},
])
def test_untrusted_output_cannot_prepare_anything(setup, monkeypatch, change):
    home, repo, target, store, group = setup
    monkeypatch.setattr(draft.adapters, "invoke", lambda *a, **kw: (answer(**change), None))
    result = draft.run(store, home=home)
    assert result["proposals"] == [] and not store.keys("proposals")
    assert result["attempts"][-1]["status"] == "invalid"


@pytest.mark.parametrize("text", ["not JSON", "```json\n{}\n```", "null", "[]", "{}"])
def test_malformed_model_output_stops_after_two_calls(setup, monkeypatch, text):
    home, repo, target, store, group = setup
    calls = []
    monkeypatch.setattr(draft.adapters, "invoke", lambda *a, **kw:
                        calls.append(a) or (text, None))
    assert draft.run(store, home=home)["proposals"] == []
    assert len(calls) == 2 and store.keys("proposals") == []


def test_reported_dollar_budget_stops_repair_admission(setup, monkeypatch):
    home, repo, target, store, group = setup
    calls = []
    monkeypatch.setattr(draft.adapters, "invoke", lambda *a, **kw:
                        calls.append(a) or (answer(before="missing"), .5))
    result = draft.run(store, home=home, budget_usd=.5)
    assert not result["proposals"] and len(calls) == 1
    assert result["known_spend_usd"] == .5 and result["unpriced_calls"] == 0


def test_managed_spans_hidden_and_uncrossable(setup, monkeypatch):
    home, repo, target, store, group = setup
    target.write_text("Use old command.\n<!-- BEGIN managed -->private-block<!-- END managed -->")
    seen = []

    def invoke(adapter, prompt, **kw):
        seen.append(prompt)
        return answer(before="private-block", after="replace"), None
    monkeypatch.setattr(draft.adapters, "invoke", invoke)
    assert draft.run(store, home=home)["proposals"] == []
    assert all("private-block" not in p and "[managed block: do not edit]" in p for p in seen)


def test_excerpts_opt_in_and_remote_failover_never_inherits_local_events(setup, monkeypatch):
    home, repo, target, store, group = setup
    monkeypatch.setattr(draft.adapters, "discover", lambda *a, **kw: [
        {"cli": cli, "available": True, "model": "fake", "quota": None}
        for cli in ("claude", "pi", "mlx")])
    seen = []

    def invoke(adapter, prompt, **kw):
        seen.append((adapter["cli"], prompt))
        if adapter["cli"] == "pi":
            raise TimeoutError("private error contents")
        return answer(), None
    monkeypatch.setattr(draft.adapters, "invoke", invoke)
    result = draft.run(store, home=home)
    assert result["proposals"] and len(seen) == 2
    assert all("EVENT_SENTINEL" not in p for _, p in seen)
    # A new store keeps the same fixture evidence but permits a second run.
    for key in store.keys("proposals"):
        store.put("decisions", {"proposal_id": key, "action": "stale", "decided_at": "2026"})
    seen.clear()
    draft.run(store, home=home, excerpts="all")
    assert all("EVENT_SENTINEL" in p for _, p in seen)


def test_pick_reject_reconsider_open_accepted_adopted_stale(setup):
    home, repo, target, store, group = setup
    assert draft.pick(store) == [group]
    key = store.put("proposals", {"group_key": group["group_key"]})
    assert draft.pick(store) == []
    for action in ("reject", "accept", "stale"):
        # Chronological decisions, not dictionary order.
        store.put("decisions", {"proposal_id": key, "action": action,
                                "decided_at": str(len(store.keys("decisions")))})
        assert bool(draft.pick(store)) == (action == "stale")
        assert bool(draft.pick(store, reconsider=group["group_key"])) == (action != "accept")
    store.put("adoptions", {"group_key": group["group_key"]})
    assert draft.pick(store) == []


def test_targets_profile_homes_limits_and_no_automatic_code(setup, monkeypatch):
    home, repo, target, store, group = setup
    assert draft.targets(group, store, home=home)[0]["path"] == target
    group["projects"] = {"repo": 2, "other": 1}
    profiles = [home / ".claude", home / ".claude-personal"]
    for p in profiles:
        p.mkdir()
        (p / "CLAUDE.md").write_text("Use old command.")
    monkeypatch.setattr(draft, "engine_homes", lambda home, engine:
                        profiles if engine == "claude_code" else [])
    assert {t["path"] for t in draft.targets(group, store, home=home)} == {
        p / "CLAUDE.md" for p in profiles}
    (profiles[0] / "CLAUDE.md").write_text("x" * 9000)
    skipped = []
    assert len(draft.targets(group, store, home=home, skipped=skipped)) == 1
    assert skipped
    code = repo / "main.py"
    code.write_text("old command")
    group["signals"]["cause"] = "environment"
    assert draft.targets(group, store, home=home) == []
    assert draft.targets(group, store, home=home, target=code)[0]["path"] == code


def test_numeric_bounds_before_discovery(setup, monkeypatch):
    home, repo, target, store, group = setup
    for kwargs in ({"budget_usd": float("nan")}, {"budget_s": 0}, {"max_groups": 0}):
        with pytest.raises(AutError):
            draft.run(store, home=home, **kwargs)


def test_config_allowlist_does_not_automatically_offer_code(setup):
    home, repo, target, store, group = setup
    code = repo / "main.py"
    code.write_text("old command")
    config = repo / "settings.toml"
    config.write_text('command="old"')
    group["signals"]["cause"] = "environment"
    offered = draft.targets(group, store, home=home, allowlist=[str(repo / "*")])
    assert [t["path"] for t in offered] == [config]


def test_plain_global_target_stages_without_touching_original(setup, monkeypatch):
    home, repo, target, store, group = setup
    target = home / "global.md"
    target.write_text("Use old command.")
    monkeypatch.setattr(draft.adapters, "invoke", lambda *a, **kw: (answer(), None))
    result = draft.run(store, home=home, target=target)
    assert result["proposals"][0]["staged"]
    assert target.read_text() == "Use old command."


def test_source_edit_during_call_is_refused(setup, monkeypatch):
    home, repo, target, store, group = setup

    def invoke(*a, **kw):
        target.write_text("changed")
        return answer(), None
    monkeypatch.setattr(draft.adapters, "invoke", invoke)
    assert draft.run(store, home=home)["proposals"] == []
    assert store.keys("proposals") == []


def test_automatic_target_symlinks_cannot_offer_code(setup):
    home, repo, target, store, group = setup
    code = repo / "main.py"
    code.write_text("PRIVATE_CODE_SENTINEL")
    target.unlink()
    target.symlink_to(code)
    assert draft.targets(group, store, home=home) == []
    link = repo / "settings.toml"
    link.symlink_to(code)
    group["signals"]["cause"] = "environment"
    assert draft.targets(group, store, home=home, allowlist=[str(link)]) == []
    assert draft.targets(group, store, home=home, target=target)[0]["path"] == code


def test_nested_and_unclosed_managed_blocks_stay_hidden(setup, monkeypatch):
    home, repo, target, store, group = setup
    original = ("Use old command.\n<!-- BEGIN outer -->outer prefix\n"
                "<!-- BEGIN inner -->inner<!-- END inner -->\nhidden outer<!-- END outer -->\n"
                "<!-- BEGIN unmatched -->hidden prefix<!-- END other -->hidden mismatched\n"
                "<!-- BEGIN unfinished -->hidden unfinished")
    target.write_text(original)
    offered = draft.targets(group, store, home=home)
    assert "hidden outer" not in offered[0]["text"]
    assert "hidden unfinished" not in offered[0]["text"]
    assert "hidden mismatched" not in offered[0]["text"]
    for before in ("hidden outer", "hidden unfinished", "hidden mismatched", "-->inner<!--"):
        with pytest.raises(AutError, match="managed"):
            draft.validate(answer(before=before), offered, ["avoidable_recovery"])


def test_multiline_secret_redacted_before_json_and_protected(setup):
    home, repo, target, store, group = setup
    target.write_text("password\n=supersecret\nUse old command.\n")
    offered = draft.targets(group, store, home=home)
    assert "supersecret" not in draft.packet(group, store, offered, excerpts=False)
    with pytest.raises(AutError, match="redacted"):
        draft.validate(answer(before="supersecret"), offered, ["avoidable_recovery"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO target")
def test_special_files_never_opened_as_text_targets(setup, monkeypatch):
    home, repo, target, store, group = setup
    fifo = repo / "blocked.toml"
    os.mkfifo(fifo)
    read = Path.read_bytes

    def checked(path):
        assert path != fifo, "a FIFO would block target discovery"
        return read(path)
    monkeypatch.setattr(Path, "read_bytes", checked)
    assert draft.targets(group, store, home=home, target=fifo) == []


def test_cli_sink_independent_and_excerpts_setting_overridden(tmp_path, monkeypatch, capsys):
    from agent_usage_tracker import cli, config
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text('[improve]\ndraft_excerpts="all"\n')
    monkeypatch.setattr(config, "load_config", lambda *a: pytest.fail("sink configuration loaded"))
    calls = []
    monkeypatch.setattr(draft, "run", lambda store, **kw: calls.append(kw) or {"proposals": []})
    assert cli.main(["improve", "--root", str(tmp_path / "store"), "draft", "--excerpts", "none"]) == 0
    assert calls[0]["excerpts"] == "none"
    assert cli.main(["improve", "--root", str(tmp_path / "store"), "draft"]) == 0
    assert calls[1]["excerpts"] == "all"
