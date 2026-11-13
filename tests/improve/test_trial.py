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


def agent_spec(tmp_path, *, accepts=True):
    roots = {name: tmp_path / name for name in ("baseline", "candidate")}
    for root in roots.values():
        root.mkdir()
        (root / "input.txt").write_text("untouched")
    oracle = tmp_path / "accept.py"
    oracle.write_text("from pathlib import Path\nimport sys\n"
                      "assert (Path(sys.argv[1])/'input.txt').read_text() == 'done'\n"
                      + ("" if accepts else "raise SystemExit(1)\n"))
    script = ("import json,sys; from pathlib import Path; "
              "assert Path('input.txt').read_text()=='untouched'; "
              "Path('input.txt').write_text('done'); "
              "Path(sys.argv[1]).write_text(json.dumps({'tool_calls':2,'interventions':0}))")
    spec = {"version": 1, "kind": "agent_task",
        "directories": {k: str(v) for k, v in roots.items()}, "repeats": 2,
        "files": {k: {"input.txt": hashlib.sha256(b"untouched").hexdigest()} for k in roots},
        "cases": [{"id": "edit", "argv": [sys.executable, "-c", script, "{metrics_file}"],
                   "timeout_s": 5, "acceptance_argv": [sys.executable, str(oracle), "{workspace}"],
                   "acceptance_files": {str(oracle): hashlib.sha256(oracle.read_bytes()).hexdigest()}}]}
    return spec, roots, oracle


def test_agent_trials_use_fresh_inputs_and_independent_acceptance(tmp_path):
    spec, roots, _ = agent_spec(tmp_path)
    store = Store(tmp_path / "private")
    key = store.put("trial_specs", spec)
    result = run_trial(store, key, execute=True)
    assert result["complete"] and result["trial_kind"] == "agent_task"
    assert result["accepted"] == {"baseline": 2, "candidate": 2}
    rows = [store.get("trial_results", key) for key in store.keys("trial_results")]
    assert len({row["workspace"] for row in rows}) == 4
    assert all(row["metrics"] == {"tool_calls": 2, "interventions": 0} for row in rows)
    assert all((root / "input.txt").read_text() == "untouched" for root in roots.values())
    assert run_trial(store, key, execute=True)["executed"] == 0


def test_agent_exit_success_cannot_replace_independent_acceptance(tmp_path):
    spec, _, _ = agent_spec(tmp_path, accepts=False)
    store = Store(tmp_path / "private")
    report = run_trial(store, store.put("trial_specs", spec), execute=True)
    assert report["accepted"] == {"baseline": 0, "candidate": 0}
    assert all(not store.get("trial_results", key)["acceptance"]["accepted"]
               for key in store.keys("trial_results"))


def test_agent_trial_rejects_changed_or_unprotected_acceptance_code(tmp_path):
    spec, roots, oracle = agent_spec(tmp_path)
    store = Store(tmp_path / "private")
    oracle.write_text("print('changed')")
    with pytest.raises(AutError, match="acceptance.*changed"):
        run_trial(store, store.put("trial_specs", spec), execute=True)
    spec["cases"][0]["acceptance_files"] = {str(roots["baseline"] / "input.txt"):
        hashlib.sha256(b"untouched").hexdigest()}
    spec["cases"][0]["acceptance_argv"] = [sys.executable,
        str(roots["baseline"] / "input.txt"), "{workspace}"]
    with pytest.raises(AutError, match="outside"):
        run_trial(store, store.put("trial_specs", spec), execute=True)


def test_agent_cannot_poison_independent_python_acceptance_imports(tmp_path):
    spec, _, _ = agent_spec(tmp_path, accepts=False)
    poison = ("Path('src').mkdir(); "
              "Path('src/sitecustomize.py').write_text('import os; os._exit(0)'); ")
    spec["cases"][0]["argv"][2] = spec["cases"][0]["argv"][2].replace(
        "Path(sys.argv[1])", poison + "Path(sys.argv[1])")
    store = Store(tmp_path / "private")
    assert run_trial(store, store.put("trial_specs", spec), execute=True)["accepted"] == {
        "baseline": 0, "candidate": 0}


def test_invalid_metrics_are_terminal_failures_and_not_rerun(tmp_path):
    spec, _, _ = agent_spec(tmp_path)
    spec["cases"][0]["argv"][2] = spec["cases"][0]["argv"][2].replace(
        "'tool_calls':2", "'tool_calls':'invented'")
    store = Store(tmp_path / "private")
    key = store.put("trial_specs", spec)
    first = run_trial(store, key, execute=True)
    assert first["complete"] and first["accepted"] == {"baseline": 0, "candidate": 0}
    assert all(store.get("trial_results", k)["validation_error"]
               for k in store.keys("trial_results"))
    assert run_trial(store, key, execute=True)["executed"] == 0
