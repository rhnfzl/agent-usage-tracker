from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from agent_usage_tracker import cli
from agent_usage_tracker import history_steers as hs
from agent_usage_tracker.config import AutError
from agent_usage_tracker.event import make_event_id
from label_helpers import _cfg


def _ms(d: str) -> int:
    return int(datetime.fromisoformat(d).timestamp() * 1000)


def _write(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")


def test_only_typed_inputs_inside_the_gap_count(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hs, "send_activity_rows", lambda dsn, rows: sent.extend(rows))
    monkeypatch.setattr(hs, "counted_days", lambda dsn: {("codex", date(2026, 7, 6))})
    claude = [{"display": t, "timestamp": _ms("2026-04-08T10:00")} for t in
              ("fix it", "/clear", "!ls", "   ", "<task-notification>x", "again")]
    claude += [{"display": "edge", "timestamp": _ms(d)} for d in
               ("2026-03-01T23:00", "2026-03-02T00:30", "2026-04-19T23:30", "2026-04-20T00:30")]
    _write(tmp_path / ".claude/history.jsonl", claude)
    _write(tmp_path / ".codex/history.jsonl", [
        {"text": "go", "ts": _ms("2026-04-08T10:00") // 1000},
        {"text": "/model", "ts": _ms("2026-04-08T10:00") // 1000},
        {"text": "Automation: daily\nAutomation ID: 1", "ts": _ms("2026-04-08T10:00") // 1000},
        {"text": "after the gap", "ts": _ms("2026-06-01T10:00") // 1000},
        {"text": "counted day", "ts": _ms("2026-07-06T10:00") // 1000},
        {"text": "too late", "ts": _ms("2026-10-03T10:00") // 1000}])
    before = (tmp_path / ".claude/history.jsonl").read_bytes()
    cfg = _cfg(tmp_path)

    assert hs.run(cfg, dry_run=True) == {"claude_code": 4, "codex": 2} and not sent
    assert hs.run(cfg, dry_run=False) == {"claude_code": 4, "codex": 2}
    by = {(r.agent, r.ts[:10]): r for r in sent}
    assert by["claude_code", "2026-04-08"].attrs == {"count": 2, "source": "history"}
    assert {k[1] for k in by if k[0] == "claude_code"} == {"2026-03-02", "2026-04-08", "2026-04-19"}
    assert all(r.kind == "steer_estimate" and r.session_id is None for r in sent)
    assert "fix it" not in json.dumps([r.attrs for r in sent])
    assert (tmp_path / ".claude/history.jsonl").read_bytes() == before
    assert {k[1] for k in by if k[0] == "codex"} == {"2026-04-08", "2026-06-01"}
    assert hs.estimates(cfg, set())[0].event_id == sent[0].event_id   # a rerun is a no-op at the sink


def test_a_missing_file_is_no_rows(tmp_path):
    assert not hs.daily_counts(tmp_path / "none.jsonl", "text", "ts", 1, date(2026, 1, 1),
                               date(2026, 12, 31))


def test_event_ids_stay_what_the_sink_already_holds(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/history.jsonl").write_text(
        json.dumps({"display": "go", "timestamp": _ms("2026-04-08T10:00")}) + "\n")
    row = hs.estimates(_cfg(tmp_path), set())[0]
    assert row.event_id == make_event_id("claude_code", "steer_estimate", date(2026, 4, 8))
    # the id of rows already in the sink: this literal must never change
    assert row.event_id == "103116a8516d39df494a0e6d45ee27e7"


def test_it_needs_the_sink(tmp_path):
    with pytest.raises(AutError, match="history-steers needs transport.pg_dsn"):
        hs.run(_cfg(tmp_path, pg_dsn=None), dry_run=True)


def test_cli_prints_the_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(hs, "run", lambda cfg, dry: {"codex": 2 if dry else 3})
    assert cli.main(["history-steers", "--dry-run"]) == 0
    assert capsys.readouterr().out == 'history-steers DRY-RUN (nothing written): {"codex": 2}\n'
    assert cli.main(["history-steers"]) == 0
    assert capsys.readouterr().out == 'history-steers written: {"codex": 3}\n'
