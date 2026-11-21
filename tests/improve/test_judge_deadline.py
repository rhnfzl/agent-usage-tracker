import json
import signal
import sys

import pytest

from agent_usage_tracker.improve import judge, schedule
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.improve.trial import command


def frozen_store(tmp_path):
    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "Check it"},
            {"id": "r", "kind": "reply", "text": "Checked"}]})
    audit = store.put("audits", {"session_ids": [sid]})
    return store, audit


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX cooperative deadline")
def test_worker_setup_and_inference_share_the_enclosing_deadline(tmp_path):
    script = '''
import json,time,sys
from pathlib import Path
from agent_usage_tracker.improve import schedule,judge
from agent_usage_tracker.improve.store import Store
store=Store(Path(sys.argv[1]))
sid=store.put("sessions", {"agent":"codex", "origin":"interactive",
    "coverage":{"complete":True}, "events":[
    {"id":"p", "kind":"prompt", "text":"Check"},
    {"id":"r", "kind":"reply", "text":"Checked"}]})
audit=store.put("audits", {"session_ids":[sid]})
policy=store.put("cycle_policies", {"audit_id":audit,"budget_s":1,"case_limit":1,
    "max_calls":24,"models":["synthetic"],"explain_model":None})
judge.config_path=lambda:store.root/"config.toml"
judge.memory_guard=lambda:None
class Backend:
    model_id="synthetic"
    model_rev="one"
    def distribution(self, request):
        try:
            time.sleep(.65)
        except Exception as exc:
            raise RuntimeError("model load failed") from exc
        return {"pass":.99,"fail":.005,"unknown":.005}
def backend(name):
    time.sleep(.7)
    return Backend()
schedule.diagnose=lambda *a,**kw:judge.diagnose(*a,**kw,backend_for=backend)
import signal
before=signal.getsignal(signal.SIGALRM)
started=time.monotonic()
result=schedule._worker(store,policy,"synthetic")
assert signal.getsignal(signal.SIGALRM)==before
print(json.dumps({"report":result,"worker_elapsed_s":time.monotonic()-started}),flush=True)
'''
    result = command([sys.executable, "-c", script, str(tmp_path / "private")],
                     tmp_path, 5)
    assert result["accepted"], result
    output = json.loads(result["output"])
    report = output["report"]
    assert report["status"] == "deferred"
    assert report["diagnosis"]["reason"] == "budget"
    assert not report["diagnosis"]["complete"]
    assert output["worker_elapsed_s"] < 1.2


def test_audit_exhaustion_defers_without_starting_models(tmp_path, monkeypatch):
    store, audit = frozen_store(tmp_path)
    policy = store.put("cycle_policies", {"days": 7, "limit": 30, "budget_s": 10,
        "case_limit": 1, "max_calls": 24, "models": ["local"], "explain_model": None})
    now = [100.0]
    monkeypatch.setattr(schedule.time, "monotonic", lambda: now[0])

    def scan(*args, **kwargs):
        now[0] += 10
        return {"audit_id": audit}

    monkeypatch.setattr(schedule, "audit", scan)
    monkeypatch.setattr(schedule, "diagnose", lambda *a, **kw: pytest.fail("budget exhausted"))
    report = schedule._worker(store, policy, "synthetic")
    assert report["status"] == "deferred"
    assert report["diagnosis"]["reason"] == "budget"
    assert json.loads((store.root / "pending_cycle.json").read_text())["audit"] == {
        "audit_id": audit}


def test_setup_resolves_model_identity_before_reusing_cached_votes(tmp_path, monkeypatch):
    store, audit = frozen_store(tmp_path)
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    revision = ["first"]
    calls = []

    class Backend:
        model_id = "synthetic"
        model_rev = ""

        def prepare(self):
            self.model_rev = revision[0]

        def distribution(self, request):
            self.prepare()
            calls.append(self.model_rev)
            return {"pass": .99, "fail": .005, "unknown": .005}

    first = judge.diagnose(store, audit, models=("synthetic",), backend_for=lambda _: Backend())
    revision[0] = "second"
    second = judge.diagnose(store, audit, models=("synthetic",), backend_for=lambda _: Backend())
    assert first["complete"] and second["complete"]
    assert second["calls"] == 2
    assert set(first["judgment_ids"]).isdisjoint(second["judgment_ids"])
    assert calls == ["first", "first", "second", "second"]


def test_completed_review_is_reused_after_interrupted_judgment_write(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store, audit = frozen_store(tmp_path)
    monkeypatch.setattr(judge, "_rubrics", lambda first=judge._rubrics()[:1]: first)
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    monkeypatch.setattr(judge, "MlxLetter", lambda _: SimpleNamespace(
        model_rev="first", prepare=lambda: None, close=lambda: None))
    reviews = []

    def review(*args):
        reviews.append(True)
        return {"verdict": "fail", "cause": "model", "rationale": "Known repeated failure",
                "evidence_ids": ["p", "r"]}

    monkeypatch.setattr(judge, "_llm_review", review)

    class Backend:
        model_id = "synthetic"
        model_rev = "first"

        def distribution(self, request):
            return {"pass": .005, "fail": .99, "unknown": .005}

    put = store.put

    def interrupted(kind, value):
        if kind == "judgments":
            raise RuntimeError("interrupted persistence")
        return put(kind, value)

    monkeypatch.setattr(store, "put", interrupted)
    with pytest.raises(RuntimeError, match="interrupted persistence"):
        judge.diagnose(store, audit, models=("synthetic",), explain_model="local",
                       backend_for=lambda _: Backend())
    assert len(store.keys("review_attempts")) == 1
    monkeypatch.setattr(store, "put", put)
    result = judge.diagnose(store, audit, models=("synthetic",), explain_model="local",
                            backend_for=lambda _: Backend())
    assert result["complete"] and result["calls"] == 0
    assert len(reviews) == 1



def test_case_persistence_uses_the_original_diagnosis_budget(tmp_path, monkeypatch):
    store, audit = frozen_store(tmp_path)
    now = [100.0]
    monkeypatch.setattr(judge.time, "monotonic", lambda: now[0])
    original = store.put

    def persist(kind, value):
        if kind == "cases":
            now[0] += 10
        return original(kind, value)

    monkeypatch.setattr(store, "put", persist)

    class Backend:
        model_id = "synthetic"
        model_rev = "first"

        def distribution(self, request):
            pytest.fail("case persistence already exhausted the budget")

    result = judge.diagnose(store, audit, models=("synthetic",), budget_s=10,
                            backend_for=lambda _: Backend())
    assert not result["complete"] and result["reason"] == "budget"
    assert result["calls"] == 0


def test_completed_judgments_are_not_repeated_after_a_checkpoint_interruption(tmp_path, monkeypatch):
    store, audit = frozen_store(tmp_path)
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    calls = []

    class Backend:
        model_id = "synthetic"
        model_rev = "first"

        def distribution(self, request):
            calls.append(request.task)
            return {"pass": .99, "fail": .005, "unknown": .005}

    def interrupt(progress):
        if progress["finished"] == 1:
            raise RuntimeError("interrupted checkpoint")

    with pytest.raises(RuntimeError, match="interrupted checkpoint"):
        judge.diagnose(store, audit, models=("synthetic",), progress=interrupt,
                       backend_for=lambda _: Backend())
    previous = store.keys("judgments")
    assert len(previous) == 1
    result = judge.diagnose(store, audit, models=("synthetic",), backend_for=lambda _: Backend())
    assert result["complete"] and result["calls"] == 1
    assert len(calls) == 2 and len(set(calls)) == 2
    assert set(previous) <= set(result["judgment_ids"])
    assert len(store.keys("judgments")) == 2



@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX cooperative deadline")
def test_process_deadline_resumes_without_repeating_completed_judgments(tmp_path):
    store, audit = frozen_store(tmp_path)
    policy = store.put("cycle_policies", {"audit_id": audit, "budget_s": 180,
        "case_limit": 1, "max_calls": 24, "models": ["synthetic"], "explain_model": None})
    script = '''
import json,signal,sys
from pathlib import Path
from agent_usage_tracker.improve import schedule,judge,rubric
from agent_usage_tracker.improve.store import Store
store=Store(Path(sys.argv[1]))
judge.config_path=lambda:store.root/"config.toml"
judge._rubrics=lambda:[item for item in rubric.load() if item.blocking]
judge.memory_guard=lambda:None
class Backend:
    model_id="synthetic"
    model_rev="first"
    def distribution(self,request):
        with (store.root/"calls.txt").open("a") as stream:
            stream.write(request.task+"\\n")
        if sys.argv[3] == "first" and request.task == "improve_completion_supported":
            signal.raise_signal(signal.SIGALRM)
        return {"pass":.99,"fail":.005,"unknown":.005}
def backend(name):
    return Backend()
schedule.diagnose=lambda *a,**kw:judge.diagnose(*a,**kw,backend_for=backend)
schedule._worker(store,sys.argv[2],sys.argv[3])
'''
    reports = []
    previous = []
    for run_id in ("first", "second"):
        result = command([sys.executable, "-c", script, str(store.root), policy, run_id],
                         tmp_path, 30)
        assert result["accepted"], result
        reports.append(json.loads((store.root / "worker_result.json").read_text())["report"])
        if run_id == "first":
            previous = store.keys("judgments")
    assert reports[0]["status"] == "deferred"
    assert reports[0]["diagnosis"]["finished"] == 1
    assert reports[0]["diagnosis"]["calls"] == 2
    assert len(previous) == 1
    assert reports[1]["status"] == "ok"
    assert reports[1]["diagnosis"]["calls"] == 1
    assert set(previous) <= set(reports[1]["diagnosis"]["judgment_ids"])
    assert len(store.keys("judgments")) == 2
    calls = (store.root / "calls.txt").read_text().splitlines()
    assert calls.count("improve_avoidable_recovery") == 1
    assert calls.count("improve_completion_supported") == 2
