import io
import json
from dataclasses import asdict
from pathlib import Path

from agent_usage_tracker.config import Config
from agent_usage_tracker.improve.case_view import write_case_view
from agent_usage_tracker.improve.evidence import read_session
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker import scan


def native_case(tmp_path, *, repeated=True):
    trace = tmp_path / "home/.claude/projects/sample/session.jsonl"
    trace.parent.mkdir(parents=True)
    sensitive = str(tmp_path / "private-path/marker.txt")
    secret_prompt = "fixture user prompt"
    ts = "2026-06-15T12:00:00Z"
    common = {"sessionId": "sample", "cwd": sensitive, "timestamp": ts, "entrypoint": "cli"}
    records = [
        {**common, "uuid": "user", "type": "user",
         "message": {"role": "user", "content": secret_prompt}},
    ]
    for index in range(2 if repeated else 1):
        records.extend([
            {**common, "type": "assistant", "uuid": f"call-{index}", "message": {
                "id": f"message-{index}", "role": "assistant", "model": "sample",
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "content": [{"type": "tool_use", "id": f"read-{index}", "name": "Read",
                             "input": {"file_path": sensitive, "offset": 4}}],
            }},
            {**common, "type": "user", "uuid": f"result-{index}", "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": f"read-{index}",
                                            "content": "é" * (12 if repeated else 7000)}],
            }},
        ])
    if repeated:
        records.append({**common, "type": "system", "subtype": "compact_boundary"})
    records.append({**common, "type": "system", "subtype": "turn_duration"})
    trace.write_text("".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8")
    session = read_session(trace, "claude_code")
    assert session is not None
    return trace, sensitive, secret_prompt, session


def render_case(tmp_path, session):
    store = Store(tmp_path / "private-store")
    revision = store.put("sessions", session)
    key = store.put("cases", {"session_snapshot": revision, "coverage": session["coverage"],
                             "events": session["events"]})
    receipt = write_case_view(store, key)
    return Path(receipt["path"]).read_text(encoding="utf-8"), revision


def test_case_context_links_native_evidence_and_keeps_rows_private(tmp_path, monkeypatch):
    trace, path, prompt, session = native_case(tmp_path)
    page, revision = render_case(tmp_path, session)
    for value in ("Context growth", "UTF-8 bytes", "Tool output", "Repeated file reads",
                  "Explicit compactions", "1 observed", "48", revision, str(trace), prompt):
        assert value in page
    for index in (0, 1, 2, 3, 4, 5):
        assert f'href="#event-{index}"' in page and f'id="event-{index}"' in page

    home = trace.parents[3]
    cfg = Config(machine="sample", agents=("claude_code",), webhook_url=None, webhook_secret=None,
                 pg_dsn=None, home=home, repo_key_salt="sample-only")
    out = io.StringIO()
    assert scan.run_export(cfg, cfg.agents, out) == (3, 0, set())
    rows = []
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state" / "state.json"))
    monkeypatch.setattr(scan, "make_sender", lambda cfg: rows.extend)
    scan.run_scan(cfg, cfg.agents, dry_run=False)
    assert any(getattr(row, "kind", "") == "steer" for row in rows)
    sink_payload = json.dumps([asdict(row) for row in rows], ensure_ascii=False)
    for raw in (path, str(trace), prompt, "file_path", "offset", "é"):
        assert raw not in out.getvalue()
        assert raw not in sink_payload


def test_case_context_does_not_infer_repeats_or_compactions_from_clipped_evidence(tmp_path):
    _, _, _, session = native_case(tmp_path, repeated=False)
    session["agent"] = "t3"  # This evidence format does not record explicit compaction events.
    session["coverage"]["prefix_omitted"] = True
    page, _ = render_case(tmp_path, session)
    assert "No repeated explicit file read was observed" in page
    assert "Compaction capture is unsupported for this evidence stream" in page
    assert "Clipped text" in page and "Source capture is incomplete" in page
    assert "Actual model context tokens are unknown" in page


def test_inspect_links_interleaved_retry_and_its_result(tmp_path):
    trace, _, _, _ = native_case(tmp_path)
    prompt, call, result, _, _, _, boundary = [
        json.loads(line) for line in trace.read_text().splitlines()
    ]
    records = [prompt]
    for index, (tool, args, output, failed) in enumerate([
        ("Edit", {"file_path": "sample.txt", "old_string": "old", "new_string": "new"},
         "Read the target in this context before editing.", True),
        ("Read", {"file_path": "sample.txt"}, "old", False),
        ("Edit", {"file_path": "sample.txt", "old_string": "old", "new_string": "new"},
         "Edit applied", False),
    ]):
        identifier = f"step-{index}"
        records.extend([
            {**call, "uuid": f"call-{index}", "message": {
                **call["message"], "id": identifier, "content": [
                    {"type": "tool_use", "id": identifier, "name": tool, "input": args}]}},
            {**result, "uuid": f"result-{index}", "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier,
                                             "content": output, "is_error": failed}]}},
        ])
    trace.write_text("".join(json.dumps(row) + "\n" for row in [*records, boundary]))
    session = read_session(trace, "claude_code")
    page, _ = render_case(tmp_path, session)
    section = page.split("Requests repeated after a tool error</h2>")[1].split("</section>")[0]
    for index, label in [(1, "Edit"), (2, "Error"), (5, "Repeated request"), (6, "success")]:
        assert f'<a href="#event-{index}">{label}</a>' in section
    assert "#event-3" not in section


def test_retry_requires_observed_failure_before_distinct_matching_call(tmp_path):
    from agent_usage_tracker.improve.case_view import _repeated_after_error

    _, _, _, session = native_case(tmp_path)
    first, failure, retry, success = session["events"][1:5]
    failure["status"] = "error"
    for sequence in (
        [first, retry, failure, success],
        [first, dict(failure, status="unknown"), retry],
        [first, failure, dict(retry, source_sha256="d" * 64)],
        [first, failure, dict(retry, run_id="other")],
        [first, failure, dict(retry, call_id=first["call_id"])],
        [first, failure, dict(retry, truncated=True)],
        [first, failure, dict(failure, id="duplicate"), retry],
    ):
        assert not _repeated_after_error(sequence)
    assert _repeated_after_error([first, failure, retry]) == [(0, 1, 2, None)]
