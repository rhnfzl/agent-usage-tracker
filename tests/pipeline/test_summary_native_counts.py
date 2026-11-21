"""Keep absent native counts distinct from measured zero in the local summary."""

import csv
from dataclasses import asdict
from datetime import date
import io
import json

from agent_usage_tracker.collectors import cursor_sdk
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import UsageEvent, make_event_id
from agent_usage_tracker.push import COLUMNS
from agent_usage_tracker.scan import run_export
from agent_usage_tracker.summary import build_summary

_DAY = date(2026, 6, 15)
_UNTIL = date(2026, 6, 16)
_COMPONENTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _config(home, agent, **options):
    return Config("fixture", (agent,), None, None, None, home, ledger_timezone="UTC",
                  repo_key_salt="fixture-only", **options)


def _export(cfg):
    out = io.StringIO()
    assert run_export(cfg, cfg.agents, out) == (3, 0, set())
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def test_usage_snapshot_fingerprint_input_keeps_its_original_fields():
    # Cursor CLI hashes serializable human rows directly to detect new activity.
    row = UsageEvent("fixture", None, "cursor_cli", None, None, kind="human")
    assert set(asdict(row)) == {*COLUMNS, "session"}


def test_codex_reasoning_presence_preserves_zero_and_the_event_seed(tmp_path):
    path = tmp_path / ".codex/sessions/rollout-reasoning.jsonl"
    path.parent.mkdir(parents=True)
    records = [{"type": "session_meta", "payload": {"id": "session"}}]
    stamps = []
    for index, reasoning in enumerate(({}, {"reasoning_output_tokens": None},
                                       {"reasoning_output_tokens": 0})):
        stamp = f"2026-06-15T12:00:0{index}Z"
        stamps.append(stamp)
        records.append({"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 10, "output_tokens": 5, **reasoning,
            }},
        }})
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    cfg = _config(tmp_path, "codex")
    summary = build_summary(cfg, since=_DAY, until=_UNTIL, prices_path=tmp_path / "missing")
    assert summary["totals"]["tokens"]["reasoning_tokens"] == 0
    assert summary["totals"]["unknown_events"]["reasoning_tokens"] == 2
    exported = _export(cfg)
    assert [row["reasoning_tokens"] for row in exported] == ["", "", "0"]
    assert [row["event_id"] for row in exported] == [
        make_event_id("codex", "session", stamp, 10, 0, 5, 0, 15, "") for stamp in stamps
    ]


def test_cursor_sdk_total_only_manifest_does_not_report_measured_splits(tmp_path):
    path = tmp_path / "manifest.json"
    cases = [
        ("total-only", {"total_tokens": 55}),
        ("partial", {"total_tokens": 6, "input_tokens": None, "output_tokens": 6}),
        ("measured", {"input_tokens": 0, "output_tokens": 4, "cache_read_tokens": 2,
                      "cache_write_tokens": 0}),
    ]
    path.write_text(json.dumps({"ticket": "fixture", "runs": [
        {"model": model, "finished_at": "2026-06-15T12:00:00Z", "usage": usage}
        for model, usage in cases
    ]}), encoding="utf-8")
    cfg = _config(tmp_path, "cursor_sdk", cursor_sdk_manifests=str(path))
    report = build_summary(cfg, since=_DAY, until=_UNTIL, group_by="model",
                           prices_path=tmp_path / "missing")
    groups = {group["key"]: group for group in report["groups"]}
    assert groups["total-only"]["tokens"]["total_tokens"] == 55
    assert {groups["total-only"]["tokens"][key] for key in _COMPONENTS} == {None}
    assert {groups["total-only"]["unknown_events"][key] for key in _COMPONENTS} == {1}
    assert groups["partial"]["tokens"]["input_tokens"] is None
    assert groups["partial"]["tokens"]["output_tokens"] == 6
    assert groups["measured"]["tokens"]["input_tokens"] == 0
    assert groups["measured"]["unknown_events"]["input_tokens"] == 0
    rows, _ = next(cursor_sdk.collect(cfg, {}))
    assert set(rows[0].to_dict()) == set(COLUMNS)
    exported = _export(cfg)
    assert [row["event_id"] for row in exported] == [
        make_event_id("cursor_sdk", "fixture", str(position), "") for position in range(3)
    ]
    assert [exported[0][key] for key in _COMPONENTS] == ["0", "0", "0", "0"]
