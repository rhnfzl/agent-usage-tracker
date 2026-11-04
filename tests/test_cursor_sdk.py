"""Hermetic tests for the Cursor SDK collector, which reads run manifests.

Run: `python tests/test_cursor_sdk.py`  (or `pytest`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.collectors import cursor_sdk  # noqa: E402
from test_collectors import _cfg, _drain  # noqa: E402


def test_cursor_sdk() -> None:
    # Synthetic orchestrator manifest (real schema, invented numbers). Exercises: finished-with-usage
    # emitted, no-usage skipped, zero-total skipped, missing total_tokens -> summed fallback, and a
    # glob-matched non-manifest JSON ignored (schema-scoped).
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        mpath = home / "proj/.state/runs/TEST-1/manifest.json"
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps({
            "ticket": "TEST-1", "project": "myrepo", "status": "in_review",
            "runs": [
                {"index": 1, "phase": "readiness-1", "model": "composer-2.5", "status": "completed",
                 "started_at": "2026-07-18T10:00:00+00:00", "finished_at": "2026-07-18T10:00:42+00:00",
                 "run_id": "r1",
                 "usage": {"input_tokens": 100, "output_tokens": 20,
                           "total_tokens": 220, "cache_read_tokens": 100}},
                {"index": 2, "phase": "proposal-1", "model": "composer-2.5", "status": "running",
                 "started_at": "2026-07-18T10:01:00+00:00"},                      # no usage -> skipped
                {"index": 3, "phase": "judge", "model": "gpt-5.6", "status": "completed",
                 "finished_at": "2026-07-18T10:02:00+00:00",
                 "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}},   # zero -> skipped
                {"index": 4, "phase": "revise", "model": "composer-2.5", "status": "completed",
                 "finished_at": "2026-07-18T10:03:00+00:00", "run_id": "r4",
                 "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 5}},  # no total
                {"index": 5, "phase": "bad-model", "model": {"nested": "obj"},   # non-str -> None, no crash
                 "status": "completed", "finished_at": "2026-07-18T10:04:00+00:00", "run_id": "r5",
                 "usage": {"input_tokens": 9, "output_tokens": 1}},
            ],
        }))
        # a stray JSON matched by the same glob but NOT a manifest -> must be ignored, not crash
        stray = home / "proj/.state/runs/other/manifest.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(json.dumps({"foo": "bar"}))

        cfg = _cfg(home)
        cfg.cursor_sdk_manifests = str(home / "**/.state/runs/*/manifest.json")
        ev = _drain(cursor_sdk.collect, cfg)
        assert len(ev) == 3, ev                          # index 1, 4, 5; 2 (no usage) & 3 (zero) out
        e1 = next(e for e in ev if e.input_tokens == 100)
        assert e1.output_tokens == 20 and e1.cache_read_tokens == 100 and e1.cache_write_tokens == 0
        assert e1.total_tokens == 220                            # given total kept as-is
        assert e1.model == "composer-2.5" and e1.provider == "cursor" and e1.agent == "cursor_sdk"
        assert e1.project == "myrepo" and e1.ts == "2026-07-18T10:00:42+00:00"  # finished_at preferred
        e4 = next(e for e in ev if e.input_tokens == 7)
        assert e4.total_tokens == 15                             # 7 + 3 + 5, summed when total absent
        assert e1.event_id != e4.event_id
        e5 = next(e for e in ev if e.input_tokens == 9)
        assert e5.model is None                          # non-str model coerced to None (no crash)
    print("ok cursor_sdk")


def test_cursor_sdk_runs_without_index_or_run_id_stay_distinct() -> None:
    # two runs with NO index and NO run_id must still get distinct ids (append-order position)
    with tempfile.TemporaryDirectory() as d2:
        h2 = Path(d2)
        mp2 = h2 / "x/.state/runs/T/manifest.json"
        mp2.parent.mkdir(parents=True, exist_ok=True)
        mp2.write_text(json.dumps({"ticket": "T", "runs": [
            {"phase": "a", "model": "m", "finished_at": "2026-07-18T10:00:00+00:00",
             "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"phase": "b", "model": "m", "finished_at": "2026-07-18T10:00:01+00:00",
             "usage": {"input_tokens": 2, "output_tokens": 2}},
        ]}))
        cfg2 = _cfg(h2)
        cfg2.cursor_sdk_manifests = str(h2 / "**/.state/runs/*/manifest.json")
        ev2 = _drain(cursor_sdk.collect, cfg2)
        assert len(ev2) == 2 and ev2[0].event_id != ev2[1].event_id, ev2


def test_cursor_sdk_expands_a_home_glob(tmp_path, monkeypatch) -> None:
    # a documented "~/..."-style glob must expand the home dir (glob.glob does not do this itself)
    mp3 = tmp_path / "aut_cursor_sdk_hometest/.state/runs/H/manifest.json"
    mp3.parent.mkdir(parents=True, exist_ok=True)
    mp3.write_text(json.dumps({"ticket": "H", "runs": [
        {"index": 1, "model": "m", "finished_at": "2026-07-18T10:00:00+00:00",
         "usage": {"input_tokens": 4, "output_tokens": 1}}]}))
    # expanduser reads HOME on POSIX but USERPROFILE on Windows; set both so this runs everywhere
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg3 = _cfg(tmp_path)
    cfg3.cursor_sdk_manifests = "~/aut_cursor_sdk_hometest/.state/runs/*/manifest.json"
    ev3 = _drain(cursor_sdk.collect, cfg3)
    assert len(ev3) == 1 and ev3[0].input_tokens == 4, ev3   # ~ expanded and matched


def test_cursor_sdk_without_a_glob_is_off(tmp_path) -> None:
    # unset glob -> collector no-ops (opt-in)
    assert _drain(cursor_sdk.collect, _cfg(tmp_path)) == []


def test_cursor_sdk_says_a_manifest_was_unreadable(capsys):
    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "manifest.json").write_text("{cut off")
        cfg = _cfg(Path(t))
        cfg.cursor_sdk_manifests = str(Path(t) / "*.json")
        assert _drain(cursor_sdk.collect, cfg) == []
    assert "1 malformed record(s)" in capsys.readouterr().err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
