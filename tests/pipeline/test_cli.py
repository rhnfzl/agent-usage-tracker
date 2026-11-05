"""cli.main dispatch for the ingest commands, on one synthetic Claude Code log and a temp state.

Locks: exit codes, and that `backfill` forgets the selected agents' watermarks and nothing else.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent_usage_tracker import cli, episode_store, inspector, local_page, manual_time, push, scan
from agent_usage_tracker.collectors import Collector
from agent_usage_tracker.event import CollectorUnavailable
from agent_usage_tracker.config import load_config
from agent_usage_tracker.state import load_state

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
    (["backfill", "--sessions-only", "--dry-run"], "sessions-only backfill DRY-RUN (nothing sent): 1 rows"),
    (["backfill", "--activity-only", "--dry-run"], "activity-only backfill DRY-RUN (nothing sent)"),
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
    log = world / ".claude" / "projects" / "-p" / "s1.jsonl"
    state = json.loads((world / "state.json").read_text())
    state["collectors"]["claude_code"]["files"][str(log)] = {"ino": log.stat().st_ino,
                                                             "off": log.stat().st_size}
    (world / "state.json").write_text(json.dumps(state))
    assert cli.main(["--agents", "claude_code", "scan"]) == 0
    assert not sent, "the watermark holds: a scan has nothing new"
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


def test_episodes_review_runs_the_real_page(world, monkeypatch, capsys):
    t0 = datetime(2026, 7, 2, tzinfo=timezone.utc)
    ep = {"id": "e1", "first_ts": t0, "last_ts": t0, "n_sessions": 1, "n_seed_sessions": 1,
          "n_commits": 1, "n_prs": 0, "outcome": "shipped",
          "members": [{"session_id": "s1", "role": "seed", "repo_keys": []}]}
    monkeypatch.setattr(episode_store, "load_month", lambda dsn, start, end: [ep])
    monkeypatch.setattr(episode_store, "answered_sessions", lambda dsn, sessions: set())
    monkeypatch.setattr(local_page.webbrowser, "open", lambda url: True)
    with patch("http.server.HTTPServer.serve_forever", side_effect=KeyboardInterrupt):
        assert cli.main(["episodes", "review", "--month", "2026-07"]) == 0
    assert "episodes review 2026-07: 0 of 1 episodes" in capsys.readouterr().out


@pytest.mark.parametrize("line", ['transport = 5', 'activitywatch_title_days = "x"',
                                  'label_precision = "x"'])
def test_a_config_value_of_the_wrong_type_is_an_operator_error(world, capsys, line):
    (world / "config.toml").write_text(line + "\n")
    assert cli.main(["status"]) == 1
    assert "wrong type" in capsys.readouterr().err


def test_export_writes_the_csv(world, capsys):
    out = world / "usage.csv"
    assert cli.main(["--agents", "claude_code", "export", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0].split(",") == list(push.COLUMNS) and len(lines) == 2
    assert "exported 1 events" in capsys.readouterr().err


def _psycopg_error() -> Exception:
    return type("OperationalError", (Exception,), {"__module__": "psycopg"})(
        "connection to postgresql://aut:hunter2@db.example/agents failed")


def test_a_sink_outage_is_one_redacted_line_and_exit_1(world, monkeypatch, capsys):
    def down(*a, **kw):
        raise _psycopg_error()
    monkeypatch.setattr(manual_time, "insert_pg", down)
    assert cli.main(["time", "add", "--minutes", "5"]) == 1
    err = capsys.readouterr().err
    assert "OperationalError" in err and "hunter2" not in err and "Traceback" not in err


def test_a_bug_keeps_its_traceback(world, monkeypatch):
    def bug(*a, **kw):
        raise KeyError("bug")
    monkeypatch.setattr(manual_time, "insert_pg", bug)
    with pytest.raises(KeyError):
        cli.main(["time", "add", "--minutes", "5"])


@pytest.mark.parametrize("bug", [RecursionError(), NotImplementedError(),
                                 RuntimeError("dictionary changed size during iteration")])
def test_a_runtime_error_that_is_no_delivery_failure_keeps_its_traceback(world, monkeypatch, bug):
    def fail(*a, **kw):
        raise bug
    monkeypatch.setattr(manual_time, "insert_pg", fail)
    with pytest.raises(type(bug)):
        cli.main(["time", "add", "--minutes", "5"])


def test_a_webhook_with_no_fallback_is_one_line_and_exit_1(world, monkeypatch, capsys):
    (world / "config.toml").write_text('machine = "t"\nagents = ["claude_code"]\n[transport]\n'
                                       'webhook_url = "https://hook.example.test/x"\n'
                                       'webhook_secret = "s"\n')
    def down(*a, **kw):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(push, "post_webhook", down)
    assert cli.main(["scan"]) == 1
    assert "aut: DeliveryError: webhook delivery failed" in capsys.readouterr().err


def test_a_refused_time_add_returns_1_from_main(world, capsys):
    assert cli.main(["time", "add", "--minutes", "5", "--start", "2026-01-01 09:00"]) == 1
    assert "not both" in capsys.readouterr().err


def test_a_partial_export_exits_1_after_writing_the_csv(world, monkeypatch, capsys):
    def partial(cfg, wm):
        return
        yield

    def unavailable(cfg, wm):
        raise CollectorUnavailable("flaky: locked")
        yield
    monkeypatch.setitem(scan.REGISTRY, "claude_code", Collector(partial))
    monkeypatch.setitem(scan.REGISTRY, "codex", Collector(unavailable))
    out = world / "usage.csv"
    assert cli.main(["--agents", "claude_code,codex", "export", "--out", str(out)]) == 1
    assert out.exists()
    err = capsys.readouterr().err
    assert "exported 0 events" in err and "export: partial" in err and "codex" in err
    assert cli.main(["--agents", "claude_code", "export", "--out", str(out)]) == 0
    assert cli.main(["--agents", "codex", "backfill", "--sessions-only", "--dry-run"]) == 1


def test_a_stopped_activitywatch_does_not_fail_a_complete_csv(world, capsys):
    (world / "config.toml").write_text('machine = "t"\nagents = ["activitywatch"]\n'
                                       'activitywatch_url = "http://127.0.0.1:9"\n')
    assert cli.main(["export", "--out", str(world / "usage.csv")]) == 0
    assert "not usable" in capsys.readouterr().err      # the reason still shows
    assert cli.main(["backfill", "--sessions-only", "--dry-run"]) == 0


def test_a_failed_collector_counts_only_where_its_rows_are_kept(world, monkeypatch):
    def unavailable(cfg, wm):
        raise CollectorUnavailable("down")
        yield
    monkeypatch.setitem(scan.REGISTRY, "cursor_api",
                        replace(scan.REGISTRY["cursor_api"], collect=unavailable))
    monkeypatch.setitem(scan.REGISTRY, "codex", replace(scan.REGISTRY["codex"], collect=unavailable))
    out = str(world / "usage.csv")
    assert cli.main(["--agents", "cursor_api", "backfill", "--activity-only", "--dry-run"]) == 0
    assert cli.main(["--agents", "cursor_api", "export", "--out", out]) == 1
    assert cli.main(["--agents", "codex", "backfill", "--activity-only", "--dry-run"]) == 1


def test_agents_is_refused_where_it_would_be_ignored_and_taken_on_both_sides(world, capsys):
    for argv in (["--agents", "codex", "outcomes"], ["--agents", "codex", "label", "run"],
                 ["--agents", "codex", "sync-prices"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
    for argv in (["scan", "--dry-run", "--agents", "codex"], ["status", "--agents", "codex"],
                 ["export", "--agents", "codex"]):
        assert cli.main(argv) == 0
    assert "agents        : codex" in capsys.readouterr().out


def test_status_survives_a_non_dict_watermark(world, capsys):
    (world / "state.json").write_text(json.dumps({"collectors": {"codex": 5}}))
    assert cli.main(["status"]) == 0
    assert "never scanned" in capsys.readouterr().out


@pytest.mark.parametrize("line, key", [('machine = 5', "machine"), ('cursor_db_path = 7', "cursor_db_path"),
                                       ('repo_key_salt = 9', "repo_key_salt"),
                                       ('[transport]\npg_dsn = 5', "pg_dsn"),
                                       ('[transport]\nwebhook_url = 5', "webhook_url")])
def test_a_non_string_setting_is_refused_by_name(world, capsys, line, key):
    (world / "config.toml").write_text(line + "\n")
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "wrong type" in err and key in err


def test_a_bad_activitywatch_url_is_refused_at_load(world, capsys):
    (world / "config.toml").write_text('activitywatch_url = "ftp://secret-host"\n')
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "activitywatch_url" in err and "secret-host" not in err


def test_an_unset_env_credential_warns_by_name(world, monkeypatch, capsys):
    monkeypatch.delenv("AUT_TEST_UNSET", raising=False)
    monkeypatch.setenv("AUT_TEST_SET", "hunter2")
    (world / "config.toml").write_text('repo_key_salt = "env:AUT_TEST_UNSET"\n'
                                       '[transport]\npg_dsn = "env:AUT_TEST_SET"\n')
    cfg = load_config()
    assert cfg.repo_key_salt is None and cfg.pg_dsn == "hunter2"
    err = capsys.readouterr().err
    assert "repo_key_salt" in err and "AUT_TEST_UNSET" in err
    assert "pg_dsn" not in err and "hunter2" not in err


def test_print_scan_shows_an_activity_only_collector(world, capsys):
    report = {"cursor_cli": {**scan._blank_agent_tally(), "activity": {"turn": 7}}}
    scan.print_scan(load_config(), report, dry_run=True)
    out = capsys.readouterr().out
    assert '{"turn": 7}' in out and "no new usage" not in out


def test_print_scan_shows_active_time(world, capsys):
    report = {"activitywatch": {**scan._blank_agent_tally(), "intervals": 2, "relevant_s": 60.0,
                                "time": {"editor": 60.0, "web": 120.0}}}
    scan.print_scan(load_config(), report, dry_run=True)
    out = capsys.readouterr().out
    assert "2 intervals  3.0 min active (1.0 min work-relevant)" in out
    assert out.index("web") < out.index("editor"), "the largest category first"


@pytest.mark.parametrize("flag, kind", [("--episode", "episode"), ("--day", "day"),
                                        ("--label", "label")])
def test_inspect_passes_the_chosen_selector(world, monkeypatch, capsys, flag, kind):
    seen = []
    monkeypatch.setattr(inspector, "run_inspect",
                        lambda cfg, k, v: seen.append((k, v)) or {"saved": 2})
    assert cli.main(["inspect", flag, "x"]) == 0
    assert seen == [(kind, "x")]
    assert "inspect: 2 corrections saved" in capsys.readouterr().out


def test_status_says_scan_will_refuse_a_remote_http_webhook(world, capsys, monkeypatch):
    (world / "config.toml").write_text('agents = ["codex"]\n[transport]\n'
                                       'webhook_url = "http://ingest.example.com/hook"\n'
                                       'webhook_secret = "env:AUT_TEST_SECRET"\n')
    monkeypatch.setenv("AUT_TEST_SECRET", "s")
    assert cli.main(["status"]) == 0
    assert "scan will refuse" in capsys.readouterr().out
