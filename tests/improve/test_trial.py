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


def test_command_acceptance_hash_changed_before_execution_runs_nothing(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest
    baseline, candidate = tmp_path / 'baseline', tmp_path / 'candidate'
    baseline.mkdir()
    candidate.mkdir()
    oracle = tmp_path / 'oracle.py'
    oracle.write_text('pass\n')
    store = Store(tmp_path / 'private')
    spec = store.put('trial_specs', {'version': 1, 'kind': 'command_check',
        'directories': {'baseline': str(baseline), 'candidate': str(candidate)}, 'repeats': 1,
        'cases': [{'id': 'protected', 'argv': [sys.executable, str(oracle)], 'timeout_s': 2,
                   'acceptance_files': {str(oracle): file_digest(oracle)}}]})
    oracle.write_text('raise RuntimeError("changed")\n')
    with pytest.raises(AutError, match='changed'):
        run_trial(store, spec, execute=True)
    assert store.keys('trial_results') == []


def test_command_acceptance_mutation_keeps_rejected_attempt_receipt(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest
    baseline, candidate = tmp_path / 'baseline', tmp_path / 'candidate'
    baseline.mkdir()
    candidate.mkdir()
    oracle = tmp_path / 'oracle.py'
    oracle.write_text("from pathlib import Path\nPath(__file__).write_text('pass\\n')\n")
    store = Store(tmp_path / 'private')
    spec = store.put('trial_specs', {'version': 1, 'kind': 'command_check',
        'directories': {'baseline': str(baseline), 'candidate': str(candidate)}, 'repeats': 1,
        'cases': [{'id': 'mutates', 'argv': [sys.executable, str(oracle)], 'timeout_s': 2,
                   'acceptance_files': {str(oracle): file_digest(oracle)}}]})
    with pytest.raises(AutError, match='changed'):
        run_trial(store, spec, execute=True)
    rows = [store.get('trial_results', key) for key in store.keys('trial_results')]
    assert len(rows) == 1
    assert rows[0]['exit_code'] == 0
    assert rows[0]['accepted'] is False
    assert 'changed' in rows[0]['validation_error']


def test_protected_trial_rejects_a_command_that_rewrites_its_fixture(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest

    roots = {name: tmp_path / name for name in ("baseline", "candidate")}
    for root in roots.values():
        root.mkdir()
        (root / "setup").write_text("frozen")
    oracle = tmp_path / "oracle.py"
    oracle.write_text("from pathlib import Path\nPath('setup').write_text('changed')\n")
    store = Store(tmp_path / "private")
    spec = store.put("trial_specs", {"version": 1, "kind": "command_check",
        "directories": {name: str(root) for name, root in roots.items()}, "repeats": 1,
        "files": {name: {"setup": file_digest(root / "setup")} for name, root in roots.items()},
        "cases": [{"id": "protected", "argv": [sys.executable, str(oracle)], "timeout_s": 2,
                   "acceptance_files": {str(oracle): file_digest(oracle)}}]})
    with pytest.raises(AutError, match="protected command acceptance changed"):
        run_trial(store, spec, execute=True)
    [row] = [store.get("trial_results", key) for key in store.keys("trial_results")]
    assert not row["accepted"]
    assert "trial input changed" in row["validation_error"]


def test_protected_command_changed_after_prospective_study_is_rejected(tmp_path):
    from .test_studies import fixture
    from agent_usage_tracker.improve.proposals import file_digest
    from agent_usage_tracker.improve.studies import create
    store, policy = fixture(tmp_path)
    oracle = tmp_path / 'frozen_acceptance.py'
    oracle.write_text('pass\n')
    trial = store.get('trial_specs', policy['trial_spec_id'])
    trial['cases'][0]['argv'] = [sys.executable, str(oracle)]
    trial['cases'][0]['acceptance_files'] = {str(oracle): file_digest(oracle)}
    policy['trial_spec_id'] = store.put('trial_specs', trial)
    create(store, policy)
    oracle.write_text('print("changed after policy")\n')
    with pytest.raises(AutError, match='changed'):
        run_trial(store, policy['trial_spec_id'], execute=True)
    assert store.keys('trial_results') == []


def test_command_bounds_reaping_after_native_child_does_not_exit(tmp_path, monkeypatch):
    from io import BytesIO
    import signal
    from agent_usage_tracker.improve import trial

    clock = [0.0]
    waits = []
    stopped = []

    class Process:
        pid = 424242
        returncode = None
        stdout = BytesIO(b"fixture native worker output")

        def wait(self, timeout=None):
            assert timeout is not None, "native child cleanup must not wait without a deadline"
            waits.append(timeout)
            clock[0] += timeout
            raise trial.subprocess.TimeoutExpired("fixture", timeout)

        def poll(self):
            return None

        def kill(self):
            stopped.append(self.pid)

    monkeypatch.setattr(trial.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(trial.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(trial.os, "killpg", lambda pid, sig: stopped.append(pid), raising=False)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        result = command(["fixture-native-worker"], tmp_path, 2)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert result["timeout"] and not result["accepted"]
    assert result["cleanup_timeout"] and result["exit_code"] is None
    assert result["pid"] == 424242 and stopped
    assert result["elapsed_s"] <= 3
    assert waits[1] <= 1 and waits[-1] == 0


def test_spawn_failure_restores_signal_handler_without_cleanup(tmp_path, monkeypatch):
    import signal
    from agent_usage_tracker.improve import trial

    previous = signal.getsignal(signal.SIGTERM)

    def unavailable(*args, **kwargs):
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(trial.subprocess, "Popen", unavailable)
    with pytest.raises(OSError, match="fixture spawn failure"):
        command(["fixture-native-worker"], tmp_path, 2)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_process_startup_is_charged_to_the_command_deadline(tmp_path, monkeypatch):
    from io import BytesIO
    from agent_usage_tracker.improve import trial

    clock = [0.0]
    waits = []

    class Process:
        pid = 424242
        returncode = 0
        stdout = BytesIO(b"fixture")

        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

        def poll(self):
            return 0

    def start(*args, **kwargs):
        clock[0] = 1.5
        return Process()

    monkeypatch.setattr(trial.subprocess, "Popen", start)
    monkeypatch.setattr(trial.time, "monotonic", lambda: clock[0])
    assert command(["fixture"], tmp_path, 2)["accepted"]
    assert waits == [.5]


def test_trial_does_not_accept_an_unreaped_child_with_unknown_exit_code(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import trial

    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    store = Store(tmp_path / "private")
    spec = store.put("trial_specs", {"version": 1, "kind": "command_check",
        "directories": {"baseline": str(baseline), "candidate": str(candidate)},
        "cases": [{"id": "fixture", "argv": ["fixture"], "timeout_s": 2}],
        "repeats": 1, "seed": 7})
    failure = {"accepted": False, "timeout": True, "exit_code": None,
               "cleanup_timeout": True, "pid": 424242, "elapsed_s": 3,
               "output": "", "output_truncated": False}
    monkeypatch.setattr(trial, "command", lambda *a, **kw: failure)
    report = run_trial(store, spec, execute=True)
    assert report["accepted"] == {"baseline": 0, "candidate": 0}
    assert report["timeouts"] == 2 and report["agent_behaviour_verified"] is False


def test_agent_trial_rejects_unknown_execution_and_acceptance_exit_codes(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import trial

    spec, _, _ = agent_spec(tmp_path)
    store = Store(tmp_path / "private")
    failure = {"accepted": False, "timeout": True, "exit_code": None,
               "cleanup_timeout": True, "pid": 424242, "elapsed_s": 3,
               "output": "", "output_truncated": False}
    monkeypatch.setattr(trial, "command", lambda *a, **kw: failure)
    report = run_trial(store, store.put("trial_specs", spec), execute=True)
    assert report["accepted"] == {"baseline": 0, "candidate": 0}
    rows = [store.get("trial_results", key) for key in store.keys("trial_results")]
    assert rows and all(row["exit_code"] is None and not row["accepted"] for row in rows)


def test_reference_oracle_rejects_an_unknown_exit_code(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import validation
    from .test_validation import setup_reference

    store, spec, job, _ = setup_reference(tmp_path)
    failure = {"accepted": False, "timeout": True, "exit_code": None,
               "cleanup_timeout": True, "pid": 424242, "elapsed_s": 3,
               "output": "", "output_truncated": False}
    monkeypatch.setattr(validation, "command", lambda *a, **kw: failure)
    with pytest.raises(AutError, match="reference oracle failed"):
        validation.validate(store, spec, job, execute=True)
    row = store.get("reference_oracles", store.keys("reference_oracles")[0])
    assert row["exit_code"] is None and row["cleanup_timeout"] and not row["accepted"]


@pytest.mark.parametrize("startup_s", [2., 3.])
def test_completed_spawn_at_or_after_deadline_is_not_accepted(tmp_path, monkeypatch, startup_s):
    from io import BytesIO
    from agent_usage_tracker.improve import trial

    clock = [0.0]
    waits = []

    class Process:
        pid = 424242
        returncode = 0
        stdout = BytesIO(b"fixture")

        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

        def poll(self):
            return 0

    def start(*args, **kwargs):
        clock[0] = startup_s
        return Process()

    monkeypatch.setattr(trial.subprocess, "Popen", start)
    monkeypatch.setattr(trial.time, "monotonic", lambda: clock[0])
    result = command(["fixture"], tmp_path, 2)
    assert result["timeout"] and not result["accepted"]
    assert result["exit_code"] == 0 and result["elapsed_s"] == startup_s
    assert waits == [0.]
