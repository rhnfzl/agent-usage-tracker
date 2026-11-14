"""Private evidence views preserve captured context and its limits."""

import json
import os
from pathlib import Path

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.case_view import write_case_view
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker import cli


def evidence(tmp_path, *, clipped=False):
    store = Store(tmp_path / "private")

    def event(key, kind, text, call="", **values):
        return {"id": key, "kind": kind, "text": text, "call_id": call,
                "status": "unknown", "line": 7, "byte_offset": None,
                "truncated": False, **values}

    events = [
        event("older", "prompt", "Unrelated earlier request"),
        event("old-end", "boundary", "turn_complete"),
        event("request", "prompt", "Check the result"),
        event("call-1", "call", "first command", "one"),
        event("result-1", "result", "failure details", "one", status="error"),
        event("recovery", "reply", "Correcting the setup"),
        event("call-2", "call", "corrected command", "two"),
        event("result-2", "result", "independent check passed", "two", status="success"),
        event("final", "reply", "The check now passes", truncated=clipped),
        event("end", "boundary", "turn_complete"),
        event("next", "prompt", "Unrelated next request"),
    ]
    session = {"version": 7, "agent": "codex", "session_id": "source",
               "origin": "interactive", "coverage": {"complete": not clipped},
               "source": {"path": str(tmp_path / "original.jsonl"), "sha256": "a" * 64,
                          "ranges": [[0, 900]]}, "events": events}
    sid = store.put("sessions", session)
    case = {"version": 8, "session_snapshot": sid,
            "coverage": {"complete": not clipped}, "omitted_task_events": 4,
            "events": [events[i] for i in (2, 3, 4, 8)]}
    key = store.put("cases", case)
    return store, key, case, session


def test_view_recovers_captured_attempt_without_neighboring_turns(tmp_path):
    store, key, _, _ = evidence(tmp_path)
    report = write_case_view(store, key)
    page = Path(report["path"]).read_text()
    assert report["captured_events"] == 8
    assert report["model_events"] == 4
    assert report["omitted_model_events"] == 4
    assert report["eligible"] is False
    for text in ("Check the result", "Correcting the setup", "corrected command",
                 "independent check passed", "The check now passes", "original.jsonl"):
        assert text in page
    assert "Unrelated earlier request" not in page
    assert "Unrelated next request" not in page
    assert "Model input remains incomplete" in page
    assert "file:" in page
    assert "line 7" in page
    if os.name != "nt":
        assert Path(report["path"]).stat().st_mode & 0o777 == 0o600


def test_view_keeps_clipping_visible(tmp_path):
    store, key, _, _ = evidence(tmp_path, clipped=True)
    report = write_case_view(store, key)
    assert report["clipped_events"] == 1
    assert report["eligible"] is False
    assert "clipped" in Path(report["path"]).read_text().lower()


@pytest.mark.parametrize("change", ["unknown_id", "changed_body", "neighboring_turn"])
def test_view_rejects_case_that_does_not_match_its_parent_attempt(tmp_path, change):
    store, _, case, session = evidence(tmp_path)
    case = json.loads(json.dumps(case))
    if change == "unknown_id":
        case["events"][0]["id"] = "not-in-source"
    elif change == "changed_body":
        case["events"][0]["text"] = "altered request"
    else:
        case["events"].append(session["events"][-1])
    key = store.put("cases", case)
    with pytest.raises(AutError, match="source|snapshot|attempt"):
        write_case_view(store, key)
    assert not list((store.root / "case_views").glob("*.html"))


def test_case_text_and_source_path_cannot_inject_active_html(tmp_path):
    store, _, case, session = evidence(tmp_path)
    attack = '<script>alert("trace")</script>'
    session["events"][2]["text"] = attack
    session["source"]["path"] = 'javascript:alert("path")'
    case["events"][0] = session["events"][2]
    case["session_snapshot"] = store.put("sessions", session)
    key = store.put("cases", case)
    report = write_case_view(store, key)
    page = Path(report["path"]).read_text()
    assert attack not in page
    assert "&lt;script&gt;" in page
    assert 'href="javascript:' not in page


def test_case_view_cli_never_loads_sink_or_prints_trace_bodies(tmp_path, monkeypatch, capsys):
    store, key, _, _ = evidence(tmp_path)

    def forbidden():
        raise AssertionError("Case inspection must stay local")

    monkeypatch.setattr(cli, "load_config", forbidden)
    assert cli.main(["improve", "--root", str(store.root), "inspect", "--case", key]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert Path(report["path"]).is_file()
    assert report["case_id"] == key
    assert "Check the result" not in output
    assert "Correcting the setup" not in output


def test_case_view_rejects_output_directory_outside_private_store(tmp_path):
    store, key, _, _ = evidence(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (store.root / "case_views").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    with pytest.raises(AutError, match="private|scope|path"):
        write_case_view(store, key)
    assert list(outside.iterdir()) == []


def test_case_view_reports_malformed_source_metadata_without_writing(tmp_path):
    store, _, case, session = evidence(tmp_path)
    session["source"] = None
    case["session_snapshot"] = store.put("sessions", session)
    key = store.put("cases", case)
    with pytest.raises(AutError, match="source"):
        write_case_view(store, key)
    assert not list((store.root / "case_views").glob("*.html"))
