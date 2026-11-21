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
