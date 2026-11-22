import json
from pathlib import Path

import pytest

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve import discovery
from agent_usage_tracker.improve.access import Evidence
from agent_usage_tracker.improve.store import Store


def _snapshot(store, name, where, prompt, response, *, task=None):
    return store.put("sessions", {
        "session_id": task or name, "agent": "codex", "cwd": where, "coverage": {},
        "events": [
            {"kind": "prompt", "id": name + "-lead", "text": prompt, "status": "unknown"},
            {"kind": "reply", "id": name + "-response", "text": response, "status": "unknown"},
        ],
    })


class Model:
    model_id = "test-local"
    model_rev = "one"

    def __init__(self, _name):
        pass

    def prepare(self):
        pass

    def generate(self, prompt, max_tokens):
        data = json.loads(prompt.splitlines()[-2])
        if max_tokens == 400:
            if data["searched"]:
                return '{"leads":[]}'
            return json.dumps({"leads": [{"anchor": 0, "query": "rehearsal", "kind": "reply",
                                         "question": "Who owns the decision after rehearsal?"}]})
        if max_tokens == 80:
            return '{"supported":true}'
        assert any("rehearsal" == record["arrival"] for record in data["records"])
        assert {row["project"] for row in data["records"]} == {"red", "blue"}
        return json.dumps({"comparable": True, "title": "Rehearse first",
                           "observation": "Both wait for a person after rehearsal.",
                           "next_check": "Check where this practice is recorded.",
                           "events": [0, 0]})



def test_discovery_follows_a_lead_and_replays_a_new_cross_project_comparison(
        tmp_path, monkeypatch, capsys):
    pytest.importorskip("pydantic_monty")
    store = Store(tmp_path / "private")
    seed = _snapshot(store, "seed", "/red", "Only do a rehearsal today.",
                     "I will ask you before applying it.")
    other = _snapshot(store, "other", "/blue", "Keep it as an independent dry run.",
                      "The rehearsal is recorded. I am waiting for the owner.")
    # The follow-up finds an additional reply from a separate project.
    first = store.put("audits", {"session_ids": [seed], "coverage": {}})
    later = store.put("audits", {"session_ids": [other], "coverage": {}})
    monkeypatch.setattr(discovery, "MlxLetter", Model)
    monkeypatch.setattr(discovery, "_project", lambda path: f"{path}/.git")

    base = ["evidence", "--root", str(store.root), "--audit", first]
    args = [*base, "discover", "--include-audit", later]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    report = store.get("discoveries", result["report_id"])
    assert report["findings"][0]["project_relation"] == "cross-project"
    assert report["counts"]["pairs_considered"] == 1
    assert report["counts"]["candidate_records"] >= 2
    assert Path(result["report"]).stat().st_mode & 0o077 == 0

    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["reused"] is True
    assert main([*base, "replay-code", report["runs"][1], "--include-audit", later]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["matches"] is True
    assert any(read["operation"] == "search" and read["lead"] for read in replay["reads"])


def test_shared_source_does_not_prove_independent_comparison():
    first = {"scope": "1", "session_id": "first", "project": "red", "project_key": "red",
             "run": "a", "sources": ["mirror"], "items": [
                 {"position": 3, "id": "one", "text": "Read the first report."}]}
    second = {**first, "scope": "2", "project_key": "blue", "session_id": "second"}
    evidence = [{"record": 0, "event": 0}, {"record": 1, "event": 0}]
    assert discovery._citations(evidence, [first, second]) == []


def test_context_reads_across_tools_respects_scope_and_bounds(tmp_path):
    store = Store(tmp_path / "private")
    events = [{"kind": "prompt", "id": "start", "text": "source prompt"}]
    events.extend({"kind": "call", "id": f"tool-{i}", "text": "tool argument"} for i in range(1150))
    events.extend([{"kind": "reply", "id": "reply", "text": "long response " * 100 + "CHOOSE THIS"},
                   {"kind": "boundary", "id": "end", "text": "turn_complete"}])
    snapshot = store.put("sessions", {"agent": "codex", "coverage": {}, "events": events})
    audit = store.put("audits", {"session_ids": [snapshot], "coverage": {}})
    around = Evidence("redacted", store, audit).context(snapshot, "reply", radius=1, limit=5, view="conversation")
    assert [item["id"] for item in around["neighbors"]] == ["start", "end"]
    assert around["anchor"]["position"] == 1151
    assert around["anchor"]["text"] == "long "
    assert around["anchor"]["truncated"] is True
    focused = Evidence("redacted", store, audit).context(
        snapshot, "reply", radius=1, limit=30, focus="choose this")
    assert focused["anchor"]["text"].endswith("CHOOSE THIS")
    assert focused["anchor"]["text_offset"] > 1_000
    metadata = Evidence("metadata", store, audit).context(snapshot, "reply", radius=1)
    assert all("text" not in item for item in [metadata["anchor"], *metadata["neighbors"]])


def test_reuses_prior_questions_when_a_new_audit_arrives(tmp_path, monkeypatch):
    pytest.importorskip("pydantic_monty")
    store = Store(tmp_path / "private")
    left = _snapshot(store, "left", "/red", "Only do a rehearsal today.",
                     "I will ask you before applying it.")
    right = _snapshot(store, "right", "/blue", "Keep it as an independent dry run.",
                      "The rehearsal is recorded. I am waiting for the owner.")
    first = store.put("audits", {"session_ids": [left, right], "coverage": {}})
    second = store.put("audits", {"session_ids": [left, right],
                                  "coverage": {"t3_native_mirrors": 1}})
    inputs = []
    original = Model.generate

    def capture(self, prompt, max_tokens):
        if max_tokens == 400:
            inputs.append(json.loads(prompt.splitlines()[-2])["prior_open_questions"])
        return original(self, prompt, max_tokens)

    monkeypatch.setattr(Model, "generate", capture)
    monkeypatch.setattr(discovery, "MlxLetter", Model)
    monkeypatch.setattr(discovery, "_project", lambda path: f"{path}/.git")
    initial = discovery.run(store, first, [], question=None, model_name=None, rounds=1)
    continued = discovery.run(store, second, [], question=None, model_name=None, rounds=1)
    assert continued["report_id"] != initial["report_id"]
    report = store.get("discoveries", continued["report_id"])
    assert report["previous_report"] == initial["report_id"]
    assert report["findings"][0]["change"] == "Seen before"
    assert inputs == [[], ["Who owns the decision after rehearsal?"]]


def test_private_discoveries_are_excluded_from_both_backup_roots(tmp_path, monkeypatch):
    from agent_usage_tracker.backup import default_excludes
    from agent_usage_tracker.label.core import data_dir

    custom = tmp_path / "custom"
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(custom))
    excluded = set(default_excludes())
    for directory in ("discoveries", "discovery_heads", "investigation_runs"):
        assert str(custom / directory) in excluded
        assert str(data_dir() / "improve" / directory) in excluded
