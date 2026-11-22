from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.store import Store


def test_replay_runs_selected_call_and_suffix_without_prefix(tmp_path, capsys):
    import json
    from agent_usage_tracker.improve.checkpoint_replay import run

    store = Store(tmp_path / "private")
    calls = []

    def baseline(state, call):
        calls.append(call["tool"])
        if call["tool"] == "read":
            state["text"] = "old"
            return {"state": state, "call": {"tool": "edit", "arguments": {"replacement": "old"}}}
        return {"result": {"text": call["arguments"]["replacement"]}}

    original = run(store, baseline, start={"state": {},
                                           "call": {"tool": "read", "arguments": {}}})
    checkpoint = original["checkpoints"][1]
    assert calls == ["read", "edit"]
    assert store.get("replay_runs", original["run_id"])["result"] == {"text": "old"}

    adapter = tmp_path / "candidate.py"
    adapter.write_text("""
def advance(state, call):
    assert state == {"text": "old"}
    assert call["tool"] == "edit"
    if call["arguments"]["replacement"] == "old":
        return {"state": state, "call": {"tool": "edit", "arguments": {"replacement": "new"}}}
    return {"result": {"text": call["arguments"]["replacement"]}}
""", encoding="utf-8")
    args = ["improve", "--root", str(store.root), "replay", "--checkpoint", checkpoint]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["step"] == 1
    assert "text" not in preview
    assert main([*args, "--adapter", str(adapter), "--execute"]) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert len(candidate["checkpoints"]) == 2
    receipt = store.get("replay_runs", candidate["run_id"])
    assert receipt["resumed_from"] == checkpoint
    assert receipt["result"] == {"text": "new"}
    assert store.get("replay_checkpoints", checkpoint)["input"]["state"] == {"text": "old"}


def test_failed_and_step_limited_replays_keep_resumable_checkpoints(tmp_path):
    from agent_usage_tracker.improve.checkpoint_replay import run

    store = Store(tmp_path / "private")
    start = {"state": {}, "call": {"tool": "one", "arguments": {}}}

    def loop(state, call):
        return {"state": state, "call": {"tool": "two", "arguments": {}}}

    limited = run(store, loop, start=start, max_steps=1)
    assert limited["status"] == "step_limit"
    checkpoint = limited["checkpoints"][-1]

    def fail(state, call):
        assert call["tool"] == "two"
        raise RuntimeError("private diagnostic")

    failed = run(store, fail, checkpoint=checkpoint)
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError"
    resumed = run(store, lambda state, call: {"result": call["tool"]},
                  checkpoint=failed["checkpoints"][0])
    assert store.get("replay_runs", resumed["run_id"])["result"] == "two"
