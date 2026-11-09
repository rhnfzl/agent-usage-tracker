"""Parallel scoring retains recipe order, bounded memory and acknowledged delivery semantics."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
import time

import pytest

from agent_usage_tracker.label import local
from agent_usage_tracker.label.backends import BackendUnavailable
from test_label_local import recipe, request
from test_label_local import backend as fake_backend


def profile_file(tmp_path, row=None, **changes):
    row = row or recipe("stack")
    runtime = {
        "recipe_sha256": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
        "workers": 2,
        "group_size": 200,
        "benchmark_sha256": "a" * 64,
        "members": [
            {
                **{k: m[k] for k in ("backend", "model_id", "model_rev")},
                "peak_memory_mb": 100 + i * 100,
            }
            for i, m in enumerate(row["recipe"]["members"])
        ],
        **changes,
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"version": 1, "tasks": {"steer_intent": runtime}}))
    return path


def test_profile_binds_recipe_and_every_member_revision(tmp_path):
    from agent_usage_tracker.label import local_parallel as parallel

    row = recipe()
    path = profile_file(tmp_path, row)
    p = parallel.load_profile("steer_intent", row, path)
    assert p is not None and p.workers == 2 and p.group_size == 200
    assert p.peaks_mb == (100.0, 200.0, 300.0)
    assert parallel.load_profile("work_category", row, path) is None
    changed = {**row, "minimum_score": 0.8}
    assert parallel.load_profile("steer_intent", changed, path) is None
    raw = json.loads(path.read_text())
    raw["tasks"]["steer_intent"]["members"][0]["model_rev"] = "another-revision"
    path.write_text(json.dumps(raw))
    assert parallel.load_profile("steer_intent", row, path) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"workers": 3},
        {"workers": True},
        {"group_size": 201},
        {"group_size": 0},
        {"benchmark_sha256": "missing"},
        {"members": []},
    ],
)
def test_invalid_optional_profile_falls_back_without_rewriting_it(tmp_path, changes):
    from agent_usage_tracker.label import local_parallel as parallel

    row = recipe()
    path = profile_file(tmp_path, row, **changes)
    before = path.read_bytes()
    assert parallel.load_profile("steer_intent", row, path) is None
    assert path.read_bytes() == before


def test_memory_admission_preserves_reserve_and_exact_boundary():
    from agent_usage_tracker.label import local_parallel as parallel

    p = parallel.Profile(200, 2, (6100.0, 9200.0, 5600.0))
    total = 64 * 2**30
    needed = (6100 + 9200) * 1e6 * 1.5 + 2 * 2**30 + total * 0.15
    assert parallel.admits(p, total, needed)
    assert not parallel.admits(p, total, needed - 1)
    assert not parallel.admits(p, total, total * 0.15)


def test_oversized_integer_peak_declines_without_crashing(tmp_path):
    from agent_usage_tracker.label import local_parallel as parallel

    row = recipe()
    path = profile_file(tmp_path, row)
    raw = json.loads(path.read_text())
    raw["tasks"]["steer_intent"]["members"][0]["peak_memory_mb"] = 10**400
    path.write_text(json.dumps(raw))
    assert parallel.load_profile("steer_intent", row, path) is None
    assert not parallel.admits(parallel.Profile(200, 2, (10**400, 200, 300)), 1024, 1024)


class Threads:
    """Exercise completion ordering without model processes in the base-install test suite."""

    def __init__(self, max_workers, **kwargs):
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, *args):
        return self.pool.submit(*args)

    def shutdown(self, **kwargs):
        self.pool.shutdown(**kwargs)


def spawned_scores(index, member, requests, label_model, llama_server, deadline):
    """A picklable worker exercises the actual process boundary without model weights."""
    if label_model == "fail":
        raise ValueError("synthetic worker failure")
    return index, [{c["id"]: 1 / len(req.candidates) for c in req.candidates} for req in requests]


@pytest.mark.parametrize("fail", [False, True])
def test_spawned_workers_are_joined_on_success_and_failure(monkeypatch, fail):
    from agent_usage_tracker.label import local_parallel as parallel

    monkeypatch.setattr(parallel, "memory_headroom", lambda: (64 * 2**30, 64 * 2**30))
    children = {p.pid for p in multiprocessing.active_children()}
    args = (
        [request("one"), request("two")],
        recipe(),
        "fail" if fail else "ok",
        "server",
        parallel.Profile(200, 2, (100.0, 200.0, 300.0)),
        time.monotonic() + 30,
    )
    if fail:
        with pytest.raises(BackendUnavailable, match="unfinished batch retries"):
            parallel.score_parallel(*args, worker=spawned_scores)
    else:
        assert set(parallel.score_parallel(*args, worker=spawned_scores)) == {"one", "two"}
    assert {p.pid for p in multiprocessing.active_children()} <= children


def test_completion_order_cannot_reorder_stack_coefficients(monkeypatch):
    from agent_usage_tracker.label import local_parallel as parallel

    row = recipe("stack")
    labels = [c["id"] for c in request().candidates]
    row["recipe"]["stack"] = {"a": [1.0, 0.0, 0.0], "b": {}}
    p = parallel.Profile(200, 2, (100.0, 200.0, 300.0))
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", Threads)
    monkeypatch.setattr(parallel, "memory_headroom", lambda: (64 * 2**30, 64 * 2**30))

    def worker(index, member, requests, label_model, llama_server, deadline):
        time.sleep(0.03 * (3 - index))
        dist = {key: 0.1 for key in labels}
        dist[labels[index]] = 0.2
        total = sum(dist.values())
        return index, [{k: v / total for k, v in dist.items()} for r in requests]

    requests = [request("one"), request("two")]
    scores = parallel.score_parallel(
        requests, row, "model", "server", p, time.monotonic() + 5, worker=worker
    )
    assert set(scores) == {"one", "two"}
    assert all(max(d, key=d.get) == labels[0] for d in scores.values())


def test_budget_expiry_prevents_member_creation(monkeypatch):
    made = []
    with pytest.raises(BackendUnavailable, match="budget"):
        local.score_member(
            recipe()["recipe"]["members"][0],
            [request()],
            time.monotonic() - 1,
            lambda m: made.append(m),
        )
    assert made == []


def test_bad_member_result_still_closes_the_model(monkeypatch):
    monkeypatch.setattr(local, "memory_guard", lambda: None)
    closed = []

    class Member:
        model_rev = "r1"

        def distribution(self, req):
            return {"wrong-label": 1.0}

        def close(self):
            closed.append(True)

    with pytest.raises(BackendUnavailable, match="invalid distribution"):
        local.score_member(
            recipe()["recipe"]["members"][0], [request()], time.monotonic() + 5, lambda m: Member()
        )
    assert closed == [True]


def test_cancelled_member_does_not_create_a_model(monkeypatch):
    made = []
    with pytest.raises(BackendUnavailable):
        local.score_member(
            recipe()["recipe"]["members"][0],
            [request()],
            time.monotonic() + 5,
            lambda m: made.append(m),
            lambda: True,
        )
    assert made == []


def test_low_memory_falls_back_to_bounded_serial_groups(tmp_path, monkeypatch):
    from agent_usage_tracker.label import local_parallel as parallel

    b, closed = fake_backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "instruction", "r3": "stop"}
    )
    b.runtime = parallel.Profile(200, 2, (100.0, 200.0, 300.0))

    def refused(*args, **kwargs):
        raise parallel.MemoryAdmissionError("not enough memory")

    monkeypatch.setattr(parallel, "score_parallel", refused)
    requests = [request(f"u{i}") for i in range(60)]
    b.prepare_requests(requests, time.monotonic() + 60)
    assert all(b.distribution(r)["instruction"] == pytest.approx(2 / 3) for r in requests)
    assert closed == ["r1", "r2", "r3"] * 2


def test_busy_coordinator_never_starts_the_fallback_model(tmp_path, monkeypatch):
    from agent_usage_tracker.state import LockBusy

    b, closed = fake_backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "instruction", "r3": "stop"}
    )

    def busy(*args, **kwargs):
        raise LockBusy("another scorer")

    monkeypatch.setattr(local, "_locked", busy)
    req = request()
    b.prepare_requests([req], time.monotonic() + 60)
    with pytest.raises(BackendUnavailable, match="already running"):
        b.distribution(req)
    assert closed == []


@pytest.mark.parametrize("bad", [{"wrong-label": 1.0}, {"instruction": float("nan")}])
def test_rejected_parallel_scores_cannot_poison_a_later_retry(tmp_path, monkeypatch, bad):
    from agent_usage_tracker.label import local_parallel as parallel

    b, _closed = fake_backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "instruction", "r3": "stop"}
    )
    b.runtime = parallel.Profile(200, 2, (100.0, 200.0, 300.0))
    req = request()
    calls = []

    def corrupt(*args, **kwargs):
        calls.append(True)
        return {req.unit_id: bad}

    monkeypatch.setattr(parallel, "score_parallel", corrupt)
    b.prepare_requests([req], time.monotonic() + 5)
    for _ in range(2):
        with pytest.raises(BackendUnavailable, match="invalid scores"):
            b.distribution(req)
    assert calls == [True, True]
