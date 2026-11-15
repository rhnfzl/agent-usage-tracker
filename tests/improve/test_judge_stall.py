import json
import os
import subprocess
import sys

import pytest

from agent_usage_tracker.improve import judge
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.improve.trial import command


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group containment")
def test_native_stall_is_killed_with_descendant_and_preserves_its_call_and_judgment(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "Fixture check"},
            {"id": "r", "kind": "reply", "text": "Checked"}]})
    audit = store.put("audits", {"session_ids": [sid]})
    policy = store.put("cycle_policies", {"audit_id": audit, "budget_s": 180, "case_limit": 1,
        "max_calls": 24, "models": ["synthetic"], "explain_model": None})
    script = '''
import hashlib,subprocess,sys
from pathlib import Path
from agent_usage_tracker.improve import schedule,judge
from agent_usage_tracker.improve.store import Store
store=Store(Path(sys.argv[1]))
judge.config_path=lambda:store.root/"config.toml"
judge.memory_guard=lambda:None
class Backend:
    model_id="synthetic"
    model_rev="fixture"
    def distribution(self,request):
        if request.task.endswith("completion_supported"):
            child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])
            (store.root/"owned-child.pid").write_text(str(child.pid))
            hashlib.pbkdf2_hmac("sha256",b"fixture",b"fixture",100000000)
        return {"pass":.99,"fail":.005,"unknown":.005}
schedule.diagnose=lambda *a,**kw:judge.diagnose(*a,**kw,backend_for=lambda _:Backend())
schedule._worker(store,sys.argv[2],"a"*32)
'''
    result = command([sys.executable, "-I", "-c", script, str(store.root), policy], tmp_path, 2)
    assert result["timeout"] and not result["accepted"] and result["exit_code"] != 0
    assert not result["cleanup_timeout"] and result["elapsed_s"] < 5
    saved = json.loads((store.root / "worker_result.json").read_text())
    progress = saved["report"]["diagnosis"]
    assert progress["finished"] == 1 and progress["calls"] == 2
    unfinished = progress["unfinished_call"]
    assert unfinished["kind"] == "decision" and unfinished["model"] == "synthetic"
    assert unfinished["model_rev"] == "fixture"
    assert unfinished["rubric"] == "completion_supported"
    assert store.get("cases", unfinished["case_id"])["session_snapshot"] == sid
    descendant = int((store.root / "owned-child.pid").read_text())
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                           capture_output=True, text=True, check=False).stdout.strip()
    assert not state or state.startswith("Z"), "owned descendant is still running"

    class Backend:
        model_id = "synthetic"
        model_rev = "fixture"

        def distribution(self, request):
            return {"pass": .99, "fail": .005, "unknown": .005}

    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    resumed = judge.diagnose(store, audit, models=("synthetic",), backend_for=lambda _: Backend())
    assert resumed["complete"] and resumed["calls"] == 1
    assert "unfinished_call" not in resumed
    assert set(progress["judgment_ids"]) <= set(resumed["judgment_ids"])
    assert len(store.keys("judgments")) == 2
