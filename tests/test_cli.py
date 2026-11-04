"""cli.main dispatch for the ingest commands, on one synthetic Claude Code log and a temp state.

Run: `python tests/test_cli.py`  (or `pytest`).
Locks: exit codes, and that `backfill` forgets the selected agents' watermarks and nothing else.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import cli, push, scan  # noqa: E402
from agent_usage_tracker.config import load_config  # noqa: E402
from agent_usage_tracker.state import load_state  # noqa: E402

_TURN = {"type": "assistant", "uuid": "a1", "requestId": "req_1",
         "timestamp": "2026-07-18T10:00:01Z", "cwd": "/x/proj", "sessionId": "s1",
         "message": {"id": "msg_1", "model": "claude-opus-5", "usage": {
             "input_tokens": 10, "output_tokens": 20}}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A home with one log, a config over pg_dsn, and a state holding two agents' watermarks."""
    log = tmp_path / ".claude" / "projects" / "-p" / "s1.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps(_TURN) + "\n")
    conf = tmp_path / "config.toml"
    conf.write_text('machine = "t"\nagents = ["claude_code", "codex"]\n'
                    '[transport]\npg_dsn = "postgresql://x"\n')
    conf.chmod(0o600)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"collectors": {"claude_code": {"files": {}},
                                                "codex": {"files": {"keep": {"off": 7}}}}}))
    monkeypatch.setenv("AUT_CONFIG", str(conf))
    monkeypatch.setenv("AUT_STATE", str(state))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _recording_sender(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr(scan, "make_sender", lambda cfg: sent.extend)
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: None)
    return sent


@pytest.mark.parametrize("argv, out", [
    (["scan", "--dry-run"], "DRY-RUN"),
    (["--agents", "claude_code", "scan", "--dry-run"], "claude_code"),
    (["status"], "watermarks"),
])
def test_read_only_commands_exit_0_and_keep_state(world, monkeypatch, capsys, argv, out):
    monkeypatch.setattr(scan, "make_sender", lambda cfg: pytest.fail("a read-only command sent"))
    before = (world / "state.json").read_text()
    assert cli.main(argv) == 0
    assert out in capsys.readouterr().out
    assert (world / "state.json").read_text() == before


def test_scan_sends_and_saves_the_watermark(world, monkeypatch):
    sent = _recording_sender(monkeypatch)
    assert cli.main(["--agents", "claude_code", "scan"]) == 0
    assert [e.event_id for e in sent] and load_state()["collectors"]["claude_code"]["files"]


def test_backfill_drops_only_the_selected_agents_watermark(world, monkeypatch):
    sent = _recording_sender(monkeypatch)
    assert cli.main(["--agents", "claude_code", "backfill"]) == 0
    assert sent, "the log is read again from the start"
    assert load_state()["collectors"]["codex"] == {"files": {"keep": {"off": 7}}}


def test_backfill_with_no_selection_uses_the_configured_agents(world, monkeypatch):
    _recording_sender(monkeypatch)
    assert cli.main(["backfill"]) == 0
    assert load_state()["collectors"]["codex"].get("files", {}).get("keep") is None
    assert load_config().agents == ("claude_code", "codex")


def test_plain_backfill_refuses_dry_run_and_an_unknown_agent(world):
    before = (world / "state.json").read_text()
    for argv in (["backfill", "--dry-run"], ["--agents", "nope", "scan", "--dry-run"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
    assert (world / "state.json").read_text() == before


def test_export_writes_the_csv(world, capsys):
    out = world / "usage.csv"
    assert cli.main(["--agents", "claude_code", "export", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0].split(",") == list(push.COLUMNS) and len(lines) == 2
    assert "exported 1 events" in capsys.readouterr().err


def test_script_mode_runs_a_test_file_on_its_own():
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("test_state.py"))],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and " passed" in r.stdout, r.stdout + r.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
