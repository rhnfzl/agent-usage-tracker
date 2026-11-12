import sys
import hashlib

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.improve.trial import command, run_trial


def test_trial_pairs_independent_directories_and_resumes_without_rerunning(tmp_path):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "answer").write_text("wrong")
    (candidate / "answer").write_text("right")
    store = Store(tmp_path / "private")
    spec = store.put("trial_specs", {"version": 1, "kind": "command_check",
        "directories": {"baseline": str(baseline), "candidate": str(candidate)},
        "cases": [{"id": "acceptance", "argv": [sys.executable, "-c",
            "from pathlib import Path; assert Path('answer').read_text() == 'right'"],
        "timeout_s": 5}], "repeats": 2, "seed": 7,
        "files": {"baseline": {"answer": hashlib.sha256(b"wrong").hexdigest()},
                  "candidate": {"answer": hashlib.sha256(b"right").hexdigest()}}})
    result = run_trial(store, spec, execute=True)
    assert result["complete"] is True
    assert result["accepted"] == {"baseline": 0, "candidate": 2}
    assert result["agent_behaviour_verified"] is False
    again = run_trial(store, spec, execute=True)
    assert again["executed"] == 0
    assert again["accepted"] == result["accepted"]
    (candidate / "answer").write_text("changed after trial")
    with pytest.raises(AutError, match="input changed"):
        run_trial(store, spec, execute=True)


def test_trial_requires_explicit_execution_and_reports_timeouts(tmp_path):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    store = Store(tmp_path / "private")
    spec = store.put("trial_specs", {"version": 1, "kind": "command_check",
        "directories": {"baseline": str(baseline), "candidate": str(candidate)},
        "cases": [{"id": "timeout", "argv": [sys.executable, "-c",
            "import time; time.sleep(10)"], "timeout_s": .05}], "repeats": 1, "seed": 7})
    preview = run_trial(store, spec, execute=False)
    assert preview["executed"] == 0
    result = run_trial(store, spec, execute=True)
    assert result["timeouts"] == 2
    assert result["accepted"] == {"baseline": 0, "candidate": 0}


def test_trial_refuses_same_directory_and_changed_input(tmp_path):
    store = Store(tmp_path / "private")
    spec = store.put("trial_specs", {"version": 1, "kind": "command_check",
        "directories": {"baseline": str(tmp_path), "candidate": str(tmp_path)},
        "cases": [{"id": "one", "argv": [sys.executable, "-c", "pass"], "timeout_s": 5}],
        "repeats": 1, "seed": 1})
    with pytest.raises(AutError, match="independent"):
        run_trial(store, spec, execute=True)


def test_trial_stops_excess_output_instead_of_accepting_a_truncated_pass(tmp_path):
    result = command([sys.executable, "-c", "print('text ' * 300000)"], tmp_path, 5)
    assert result["output_limit"] is True
    assert result["accepted"] is False
    assert len(result["output"]) <= 12000


def test_interrupt_kills_and_reaps_the_owned_child(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import trial

    original = trial.subprocess.Popen
    children = []

    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        wait = child.wait
        first = True

        def interrupted(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt
            return wait(*args, **kwargs)

        child.wait = interrupted
        children.append(child)
        return child

    monkeypatch.setattr(trial.subprocess, "Popen", start)
    try:
        with pytest.raises(KeyboardInterrupt):
            command([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, 5)
        assert children[0].poll() is not None
    finally:
        if children and children[0].poll() is None:
            children[0].kill()
            children[0].wait()
