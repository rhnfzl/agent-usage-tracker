import json
import re
from contextlib import contextmanager
from datetime import datetime

import pytest

from agent_usage_tracker import cli, improve_publish as publish, push
from agent_usage_tracker.config import AutError, Config
from agent_usage_tracker.improve.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "private"
    store = Store(root)
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(root))
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    cfg = Config(machine="test", agents=(), pg_dsn="postgresql://localhost/scratch",
                 webhook_url=None, webhook_secret=None, home=tmp_path, ledger_timezone="Asia/Tokyo")
    sent, beats = [], []
    monkeypatch.setattr(publish, "write_many", lambda dsn, sql, rows: sent.append((sql, rows)))
    monkeypatch.setattr(publish, "check_ledger_zone", lambda *args: None)
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: beats.extend(rows))
    day = datetime.now(publish.ledger_zone(cfg)).date().isoformat()
    row = dict(day=day, rubric_id="avoidable_recovery", rubric_version=1, harness="codex",
               trigger="control", passed=2, failed=1, unknown=1, qualified=False)
    monkeypatch.setattr(publish.scores, "daily", lambda *a, **kw: [dict(row)])
    return cfg, store, row, sent, beats


def adopt(store, **extra):
    return store.put("adoptions", dict(title="Use the CI pytest entrypoint", kind="instruction",
        adopted_at="2026-11-14", rubric_ids=["avoidable_recovery"], pr=53,
        targets=["/private/AGENTS.md"], commit="a" * 40, note="private rationale",
        benefit_claim=False, **extra))


def test_dry_run_has_no_delivery_cache_or_store_mutation(setup, monkeypatch):
    cfg, store, row, sent, beats = setup
    adopt(store)
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    cfg.pg_dsn = None
    assert publish.run_publish(cfg, dry_run=True) == {"scores": 1, "changed": 1, "adoptions": 1}
    assert not sent and not beats
    assert not (store.root.parent / "improve_publish_cache.json").exists()
    assert before == {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


def test_changed_rows_adoption_noon_privacy_and_heartbeat(setup):
    cfg, store, row, sent, beats = setup
    key = adopt(store)
    publish.run_publish(cfg)
    score_sql, score_rows = sent[0]
    adopt_sql, adoption_rows = sent[1]
    assert score_sql == publish.SCORE_SQL and score_rows[0][5:9] == (2, 1, 1, False)
    assert "ON CONFLICT (adoption_id) DO NOTHING" in adopt_sql
    adoption = adoption_rows[0]
    assert adoption[0] == key[:16] and len(adoption) == 6
    assert adoption[1].hour == 12 and adoption[1].utcoffset().total_seconds() == 9 * 3600
    for record in score_rows + adoption_rows:
        for value in record:
            for text in value if isinstance(value, list) else [value]:
                if isinstance(text, str):
                    assert len(text) <= 120 and not re.search(r"[/\\]|[a-f0-9]{32,}", text)
    assert beats[0]["collector"] == "improve_publish" and beats[0]["status"] == "ok"
    assert publish.run_publish(cfg)["changed"] == 0
    assert sent[2][1] == []
    row["qualified"] = True
    assert publish.run_publish(cfg)["changed"] == 1


def test_removed_keys_zero_once_and_old_history_stays(setup, monkeypatch):
    cfg, store, row, sent, beats = setup
    publish.run_publish(cfg)
    monkeypatch.setattr(publish.scores, "daily", lambda *a, **kw: [])
    assert publish.run_publish(cfg)["changed"] == 1
    assert sent[2][1][0][5:8] == (0, 0, 0)
    assert publish.run_publish(cfg)["changed"] == 0
    cache_path = store.root.parent / "improve_publish_cache.json"
    cache = json.loads(cache_path.read_text())
    cache["rows"][0].update(day="2020-01-01", failed=9)
    cache_path.write_text(json.dumps(cache))
    assert publish.run_publish(cfg)["changed"] == 0
    assert json.loads(cache_path.read_text())["rows"][0]["failed"] == 9


def test_failed_adoption_write_keeps_cache_for_retry(setup, monkeypatch):
    cfg, store, row, sent, beats = setup
    adopt(store)
    original = publish.write_many
    def fail(dsn, sql, rows):
        if sql == publish.ADOPT_SQL:
            raise RuntimeError("offline")
        original(dsn, sql, rows)
    monkeypatch.setattr(publish, "write_many", fail)
    with pytest.raises(RuntimeError):
        publish.run_publish(cfg)
    assert not (store.root.parent / "improve_publish_cache.json").exists()
    assert beats[-1]["status"] == "error"
    monkeypatch.setattr(publish, "write_many", original)
    assert publish.run_publish(cfg)["changed"] == 1


@pytest.mark.parametrize("field,value", [
    ("title", "Read /private/file"), ("title", "Read C:\\private"),
    ("title", "a" * 121), ("title", "a" * 32), ("title", "text\ntranscript"),
    ("rubric_ids", ["/private/file"]), ("rubric_ids", ["a" * 64]),
    ("kind", "transcript"), ("pr", True), ("benefit_claim", True),
])
def test_boundary_rejects_unsafe_adoption_before_any_data_write(setup, field, value):
    cfg, store, row, sent, beats = setup
    key = adopt(store)
    bad = store.get("adoptions", key) | {field: value}
    store.put("adoptions", bad)
    with pytest.raises(AutError):
        publish.run_publish(cfg)
    assert sent == []
    assert beats[-1]["status"] == "error"


@pytest.mark.parametrize("field,value", [("harness", "/private"), ("trigger", "transcript"),
    ("rubric_id", "a" * 64), ("failed", -1), ("passed", True), ("qualified", "yes")])
def test_boundary_rejects_unsafe_scores(setup, field, value):
    cfg, store, row, sent, beats = setup
    row[field] = value
    with pytest.raises(AutError):
        publish.run_publish(cfg)
    assert not sent


def test_missing_store_fails_closed_and_cli_is_registered(setup, monkeypatch):
    cfg, store, row, sent, beats = setup
    root = store.root.parent / "absent"
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(root))
    with pytest.raises(AutError, match="no improvement store"):
        publish.run_publish(cfg)
    assert not root.exists() and not sent
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(store.root))
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["publish-improve", "--dry-run", "--days", "30"]) == 0


def test_cache_is_bound_to_sink_and_store(setup):
    cfg, store, row, sent, beats = setup
    publish.run_publish(cfg)
    cfg.pg_dsn = "postgresql://localhost/another"
    assert publish.run_publish(cfg)["changed"] == 1
    assert "another" not in (store.root.parent / "improve_publish_cache.json").read_text()


def test_push_timestamp_is_captured_after_acquiring_cache_lock(setup, monkeypatch):
    cfg, store, row, sent, beats = setup
    locked = False
    early = datetime.fromisoformat(row["day"] + "T00:00:00+00:00")
    late = early.replace(hour=1)
    class Clock(datetime):
        @classmethod
        def now(cls, zone):
            return late if locked else early
    @contextmanager
    def lock(path):
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False
    monkeypatch.setattr(publish, "datetime", Clock)
    monkeypatch.setattr(publish, "_locked", lock)
    publish.run_publish(cfg)
    assert sent[0][1][0][-1] == late
