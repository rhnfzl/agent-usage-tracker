import json
import os
import subprocess

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.decide import accept, adopt
from agent_usage_tracker.improve.store import Store


def test_skill_proposal_needs_independent_success_and_stages_only_in_candidate(tmp_path, capsys):
    scope = tmp_path / "repo"
    scope.mkdir()
    subprocess.run(["git", "init", "-q", str(scope)], check=True)
    store = Store(tmp_path / "private")
    sessions, cases = [], []
    for n in range(2):
        events = [
            {"id": f"p{n}", "kind": "prompt", "text": f"Verify workspace {n}"},
            {"id": f"c{n}", "kind": "call", "call_id": f"link{n}", "text": "Run the check"},
            {"id": f"r{n}", "kind": "result", "call_id": f"link{n}",
             "status": "success", "text": f"Verified {n}"},
            {"id": f"b{n}", "kind": "boundary", "text": "turn_complete"},
        ]
        sessions.append(store.put("sessions", {"agent": "codex", "session_id": f"s{n}",
            "cwd": str(scope), "events": events, "coverage": {"complete": True}}))
        cases.append(store.put("cases", {"session_snapshot": sessions[-1], "events": events,
            "coverage": {"complete": True}, "omitted_task_events": 0}))
    audit = store.put("audits", {"session_ids": sessions, "coverage": {}})
    draft = tmp_path / "draft.md"
    draft.write_text("---\nname: verify-setup\ndescription: Verify the project setup.\n---\n"
                     "\nRun the setup check and inspect the result.", encoding="utf-8")
    relative = ".agents/skills/verify-setup/SKILL.md"
    args = ["evidence", "--root", str(store.root), "--audit", audit, "propose-skill",
            "--scope", str(scope), "--target", relative, "--draft-file", str(draft)]
    assert main([*args, "--proof", f"{cases[0]}:r0"]) == 1
    capsys.readouterr()

    candidate = tmp_path / "candidate"
    assert main([*args, "--proof", f"{cases[0]}:r0", "--proof", f"{cases[1]}:r1",
                 "--candidate", str(candidate)]) == 0
    key = json.loads(capsys.readouterr().out)["proposal_id"]
    item = store.get("proposals", key)
    assert item["before_sha256"] is None
    assert [proof["case_id"] for proof in item["supporting_cases"]] == cases
    assert [proof["event_id"] for proof in item["supporting_cases"]] == ["r0", "r1"]
    assert not (scope / relative).exists()
    assert (candidate / relative).read_text(encoding="utf-8") == draft.read_text(encoding="utf-8")
    staged = candidate / relative
    if os.name == "posix":
        assert staged.stat().st_mode & 0o077 == 0
    subprocess.run(["git", "-C", str(scope), "apply", "--check", "-"],
                   input=item["diff"], text=True, check=True)
    assert accept(store, key)["status"] == "accepted"
    adoption = adopt(store, key, commit="a" * 40)["adoption_id"]
    assert store.get("adoptions", adoption)["kind"] == "skill"
    assert not (scope / relative).exists()
