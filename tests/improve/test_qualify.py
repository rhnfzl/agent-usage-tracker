"""Human review stays local, versioned, append-only and advisory."""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace

import pytest

from agent_usage_tracker import cli
from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import judge, qualify, rubric, validation
from agent_usage_tracker.improve.store import Store, digest

RUBRIC = next(r for r in rubric.load() if r.id == "completion_supported")


def case(store, name, trigger="error_result", **changes):
    events = [{"id": name + "p", "kind": "prompt", "text": "Check " + name},
              {"id": name + "a", "kind": "reply", "text": "Checked " + name}]
    sid = store.put("sessions", {"events": events, "agent": "codex", "session_id": name,
                                "source": {}})
    row = {"session_snapshot": sid, "trigger": trigger, "events": events,
           "coverage": {"complete": True}, "omitted_task_events": 0, **changes}
    key = store.put("cases", row)
    return key


def judgment(store, key, verdict="pass", *, item=RUBRIC, legacy=False, votes=None):
    snap = rubric.snapshot(store, item)
    rubrics = {item.id: snap}
    if legacy:
        rubrics = {item.id: [item.question, item.definition]}
    policy = {"cases": [key], "rubrics": rubrics,
              "choice_descriptions": {item.id: item.labels}}
    job = store.put("diagnoses", policy)
    row = {"case_id": key, "rubric": item.id, "verdict": verdict,
           "evaluation_key": judge.evaluation_key(judge.evaluator(policy, item.id), key, item.id),
           "job_id": job, "votes": votes or [verdict]}
    if not legacy:
        row.update(rubric_version=item.version, rubric_sha256=item.sha256, rubric_snapshot=snap)
    jid = store.put("judgments", row)
    store.append("diagnosis", judgment_ids=[jid])
    return jid


def test_queue_is_fail_rich_inserts_controls_and_deduplicates_evidence(tmp_path):
    store = Store(tmp_path / "store")
    first = case(store, "fail")
    judgment(store, first, "fail", legacy=True)
    split = case(store, "split")
    judgment(store, split, "unknown", votes=["pass", "fail"])
    errors = [case(store, "error" + str(i)) for i in range(6)]
    controls = [case(store, "control" + str(i), "control") for i in range(3)]
    case(store, "incomplete", coverage={"complete": False})
    case(store, "omitted", omitted_task_events=1)
    case(store, "clipped", events=[{"id": "clip", "kind": "reply", "truncated": True}])
    case(store, "merge", "merge")
    duplicate = store.put("cases", {**store.get("cases", first), "copied": True})
    keys = [row["case_id"] for row in qualify.queue(store, RUBRIC)]
    assert keys[:2] == [first, split]
    assert keys[4] in controls and keys[9] in controls
    assert set(keys) == {first, split, *errors, *controls}
    assert duplicate not in keys
    qualify.save(store, RUBRIC, first, "fail")
    assert all(validation.evidence_key(row) != validation.evidence_key(store.get("cases", first))
               for row in qualify.queue(store, RUBRIC))


def test_old_labels_and_judgments_do_not_apply_to_a_new_version(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    key = case(store, "versioned")
    judgment(store, key, "fail")
    qualify.save(store, RUBRIC, key, "fail")
    newer = replace(RUBRIC, version=2)
    monkeypatch.setattr(judge, "_rubrics", lambda: [newer])
    assert qualify.labels(store, newer) == {}
    assert qualify.judgments(store, newer) == []
    assert [r["case_id"] for r in qualify.queue(store, newer)] == [key]
    qualify.save(store, newer, key, "pass")
    report = qualify.run(store, newer.id)
    assert report["counts"] == {"missing": 1} and not report["qualified"]
    assert judge.queued(store) == [key]
    judgment(store, key, "pass", item=newer)
    assert qualify.run(store, newer.id)["false_alarm_rate"] == 0
    assert judge.queued(store) == []


def test_undo_is_append_only_and_allows_relabelling_without_unknown_counts(tmp_path):
    store = Store(tmp_path / "store")
    key = case(store, "undo")
    first = qualify.save(store, RUBRIC, key, "fail")
    assert judge.queued(store) == [key]
    tombstone = qualify.save(store, RUBRIC, key, None)
    assert store.get("human_labels", first)["label"] == "fail"
    assert store.get("human_labels", tombstone)["label"] is None
    assert len(store.keys("human_labels")) == 2
    assert not qualify.labels(store, RUBRIC)
    assert judge.queued(store) == []
    report = qualify.run(store, RUBRIC.id)
    assert report["counts"] == {} and report["fail_labels"] == 0
    assert len(qualify.queue(store, RUBRIC)) == 1
    qualify.save(store, RUBRIC, key, "unknown")
    judgment(store, key, "unknown")
    report = qualify.run(store, RUBRIC.id)
    assert report["counts"]["reference_unknown"] == 1
    assert report["abstention_rate"] == 1


def test_undo_keeps_queue_entry_needed_by_another_rubric(tmp_path):
    store = Store(tmp_path / "store")
    key = case(store, "shared")
    other = next(r for r in rubric.load() if r.id == "avoidable_recovery")
    qualify.save(store, RUBRIC, key, "fail")
    qualify.save(store, other, key, "pass")
    qualify.save(store, RUBRIC, key, None)
    assert judge.queued(store) == [key]


def test_repeated_answers_and_undo_survive_a_clock_with_no_tick(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    key = case(store, "same-clock")
    monkeypatch.setattr(qualify, "_now", lambda: "2026-11-21T12:00:00+00:00")
    qualify.save(store, RUBRIC, key, "fail")
    qualify.save(store, RUBRIC, key, None)
    qualify.save(store, RUBRIC, key, "fail")
    assert len(store.keys("human_labels")) == 3
    assert next(iter(qualify.labels(store, RUBRIC).values()))["label"] == "fail"


def test_queue_failure_leaves_answer_unsaved_and_retryable(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    key = case(store, "queue-write-error")

    def broken(*args, **kwargs):
        raise OSError("fixture queue write failure")

    with monkeypatch.context() as patch:
        patch.setattr(judge, "update_queue", broken)
        with pytest.raises(OSError, match="queue write failure"):
            qualify.save(store, RUBRIC, key, "fail")
        assert not store.keys("human_labels")
        assert qualify.labels(store, RUBRIC) == {}
    qualify.save(store, RUBRIC, key, "fail")
    assert len(store.keys("human_labels")) == 1


def test_journal_failure_after_commit_keeps_saved_answer_acknowledged(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "store")
    key = case(store, "journal-write-error")

    def broken(*args, **kwargs):
        raise OSError("fixture journal write failure")

    monkeypatch.setattr(store, "append", broken)
    qualify.save(store, RUBRIC, key, "fail")
    assert next(iter(qualify.labels(store, RUBRIC).values()))["label"] == "fail"
    assert "label saved" in capsys.readouterr().err
    qualify.save(store, RUBRIC, key, None)
    assert not qualify.labels(store, RUBRIC) and judge.queued(store) == []


def test_reconciliation_cannot_remove_a_concurrently_saved_answer(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    key = case(store, "concurrent-answer")
    snapshot, attempted, saved = threading.Event(), threading.Event(), threading.Event()
    original = qualify.labels

    def pause_after_snapshot(*args):
        rows = original(*args)
        if threading.current_thread() is sync_thread and args[1].id == RUBRIC.id \
                and not snapshot.is_set():
            snapshot.set()
            assert attempted.wait(5)
            saved.wait(.2)
        return rows

    def answer():
        attempted.set()
        qualify.save(store, RUBRIC, key, "fail")
        saved.set()

    monkeypatch.setattr(qualify, "labels", pause_after_snapshot)
    sync_thread = threading.Thread(target=lambda: qualify._sync_queue(store), daemon=True)
    save_thread = threading.Thread(target=answer, daemon=True)
    sync_thread.start()
    assert snapshot.wait(5)
    save_thread.start()
    sync_thread.join(5)
    save_thread.join(5)
    assert not sync_thread.is_alive() and not save_thread.is_alive() and saved.is_set()
    assert len(qualify.labels(store, RUBRIC)) == 1
    assert judge.queued(store) == [key]


def test_qualification_policy_and_shared_score_are_exact(tmp_path):
    store = Store(tmp_path / "store")
    pairs = [("fail", "fail")] * 15 + [("fail", "unknown")] * 5 \
        + [("pass", "fail")] + [("pass", "pass")] * 9
    ids = []
    for i, (human, model) in enumerate(pairs):
        key = case(store, str(i))
        ids.append(key)
        judgment(store, key, model)
        qualify.save(store, RUBRIC, key, human)
    report = qualify.run(store, RUBRIC.id)
    shared = validation.score(pairs)
    assert {k: report[k] for k in shared} == shared
    assert report["failure_recall"] == .75 and report["false_alarm_rate"] == .1
    assert report["abstention_rate"] == 5 / 30 and report["qualified"]
    assert report["policy"] == RUBRIC.qualify and report["fail_labels"] == 20
    qualify.save(store, RUBRIC, ids[0], None)
    assert not qualify.run(store, RUBRIC.id)["qualified"]


def test_no_pass_reference_or_missing_judgment_cannot_qualify(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    item = replace(RUBRIC, qualify={**RUBRIC.qualify, "min_fail_labels": 1})
    monkeypatch.setattr(judge, "_rubrics", lambda: [item])
    key = case(store, "fail")
    judgment(store, key, "fail", item=item)
    qualify.save(store, item, key, "fail")
    assert not qualify.run(store, item.id)["qualified"]
    missing = case(store, "missing", "control")
    qualify.save(store, item, missing, "pass")
    report = qualify.run(store, item.id)
    assert report["counts"]["missing"] == 1 and report["failure_recall"] is None


def test_newest_judgments_control_recall_and_false_alarm_gates(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    item = replace(RUBRIC, qualify={**RUBRIC.qualify, "min_fail_labels": 1})
    monkeypatch.setattr(judge, "_rubrics", lambda: [item])
    fail = case(store, "failure")
    control = case(store, "control", "control")
    qualify.save(store, item, fail, "fail")
    qualify.save(store, item, control, "pass")
    judgment(store, fail, "fail", item=item)
    judgment(store, control, "pass", item=item)
    assert qualify.run(store, item.id)["qualified"]
    judgment(store, fail, "pass", item=item)
    report = qualify.run(store, item.id)
    assert report["failure_recall"] == 0 and not report["qualified"]
    judgment(store, fail, "fail", item=item)
    judgment(store, control, "fail", item=item)
    report = qualify.run(store, item.id)
    assert report["false_alarm_rate"] == 1 and not report["qualified"]


def test_mismatched_judgment_snapshot_cannot_qualify(tmp_path):
    store = Store(tmp_path / "store")
    key = case(store, "wrong-version")
    jid = judgment(store, key, "fail")
    store.put("judgments", {**store.get("judgments", jid), "rubric_version": True})
    with pytest.raises(AutError, match="versioned judgment"):
        qualify.run(store, RUBRIC.id)


def test_current_snapshot_cannot_use_original_job_bound_key(tmp_path):
    store = Store(tmp_path / "store")
    key = case(store, "current")
    jid = judgment(store, key)
    row = store.get("judgments", jid)
    store.put("judgments", {**row, "evaluation_key": digest({
        "job": row["job_id"], "case": key, "rubric": RUBRIC.id})})
    with pytest.raises(AutError, match="versioned judgment"):
        qualify.run(store, RUBRIC.id)


@pytest.mark.parametrize("field,value", [("labeller", "model"), ("label", "invalid"),
    ("label", []), ("rubric_version", True), ("evidence_sha256", "f" * 64),
    ("labelled_at", "2026-11-21"), ("note", 2)])
def test_invalid_human_records_fail_closed(tmp_path, field, value):
    store = Store(tmp_path / "store")
    key = case(store, "bad")
    saved = qualify.save(store, RUBRIC, key, "pass")
    store.put("human_labels", {**store.get("human_labels", saved), field: value})
    with pytest.raises(AutError, match="human label"):
        qualify.labels(store, RUBRIC)


@pytest.mark.parametrize("policy", [{}, {"min_fail_labels": True, "min_failure_recall": .7,
    "max_false_alarm_rate": .2}, {"min_fail_labels": 20, "min_failure_recall": float("nan"),
    "max_false_alarm_rate": .2}])
def test_bad_policy_is_rejected_before_serving(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(judge, "_rubrics", lambda: [replace(RUBRIC, qualify=policy)])
    with pytest.raises(AutError, match="policy"):
        qualify.review(Store(tmp_path / "store"), RUBRIC.id)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, form=None):
    data = None if form is None else urllib.parse.urlencode(form).encode()
    try:
        with urllib.request.build_opener(NoRedirect).open(url, data=data, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_real_http_page_saves_undoes_and_serves_only_token_protected_cases(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    key = case(store, "<script>bad</script>")
    judgment(store, key, "fail")
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("Label command loaded sink"))
    ready, box = threading.Event(), {}

    def on_ready(url):
        box["url"] = url
        ready.set()

    def serve():
        box["result"] = qualify.review(store, RUBRIC.id, open_browser=False, on_ready=on_ready)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(5)
    url = box["url"]
    try:
        assert url.startswith("http://127.0.0.1:")
        code, page = request(url)
        assert code == 200 and 'data-key="1"' in page and 'data-key="u"' in page
        assert re.search(r'name=u value="([a-f0-9]+)"', page)[1] == key
        assert '<script>bad</script>' not in page
        assert "/case/" + key in page and "you said" not in page
        base = url.rsplit("/", 1)[0]
        for route in (base + "/case/" + key, url + "/case/" + "0" * 64,
                      url + "/case/../cases", url + "x"):
            assert request(route)[0] == 404
        code, evidence = request(url + "/case/" + key)
        assert code == 200 and "Private case evidence" in evidence
        assert "&lt;script&gt;bad&lt;/script&gt;" in evidence
        assert request(url, {"a": "fail", "u": "stale"})[0] == 303
        assert not store.keys("human_labels")
        assert request(url, {"a": "fail", "u": key})[0] == 303
        assert len(store.keys("human_labels")) == 1
        assert request(url, {"a": ":undo"})[0] == 303
        assert len(store.keys("human_labels")) == 2
        assert qualify.labels(store, RUBRIC) == {}
        assert request(url, {"a": "pass", "u": key})[0] == 303
        assert next(iter(qualify.labels(store, RUBRIC).values()))["label"] == "pass"
    finally:
        request(url, {"a": ":quit"})
        thread.join(5)
    assert not thread.is_alive() and box["result"]["answered"] == 1
    assert len(store.keys("qualifications")) == 1
    assert all("Checked" not in store.path("human_labels", k).read_text()
               for k in store.keys("human_labels"))


def test_label_cli_registration_without_sink_or_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("sink loaded"))
    monkeypatch.setattr(qualify, "review", lambda store, rubric_id, limit: {
        "rubric_id": rubric_id, "limit": limit, "private": True})
    assert cli.main(["improve", "--root", str(tmp_path / "store"), "label",
                     "--rubric", RUBRIC.id, "--limit", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "rubric_id": RUBRIC.id, "limit": 7, "private": True}


def test_saving_one_label_does_not_scan_other_unlabelled_cases(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    chosen = case(store, "chosen")
    unrelated = case(store, "unrelated")
    get = store.get

    def read(kind, key):
        if kind == "cases" and key == unrelated:
            raise AssertionError("saving a label must not load unrelated unlabelled cases")
        return get(kind, key)

    monkeypatch.setattr(store, "get", read)
    label = qualify.save(store, RUBRIC, chosen, "fail")
    assert get("human_labels", label)["case_id"] == chosen
