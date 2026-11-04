"""Watermark store tests.

The property under test is mostly about NOISE, not correctness: losing the watermark is always
safe (the sink dedups on event_id) but never free, because the next scan re-reads and re-pushes
every log from byte 0. So an unusable state file has to say so on stderr instead of quietly
starting clean, which is what made a real full-history re-push hard to explain after the fact.
"""

from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from agent_usage_tracker import push
from agent_usage_tracker.config import AutError, Config, load_config
from agent_usage_tracker.state import (atomic_write_text, load_json_dict, load_state,
                                       save_state)


@contextlib.contextmanager
def _state_file(content: str | None):
    """Point AUT_STATE at a temp state.json holding `content` (None = no file at all)."""
    with tempfile.TemporaryDirectory() as d, pytest.MonkeyPatch.context() as mp:
        p = Path(d) / "state.json"
        if content is not None:
            p.write_text(content)
        mp.setenv("AUT_STATE", str(p))
        yield p


def _load_capturing_stderr() -> tuple[dict, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        state = load_state()
    return state, err.getvalue()


def test_missing_state_is_silent() -> None:
    # A first run on a new machine has no watermark and that is not worth warning about.
    with _state_file(None):
        state, err = _load_capturing_stderr()
        assert state == {"collectors": {}}
        assert err == ""


def test_valid_state_round_trips() -> None:
    good = {"collectors": {"codex": {"files": {"/x.jsonl": {"ino": 7, "off": 42}}}}}
    with _state_file(json.dumps(good)) as p:
        state, err = _load_capturing_stderr()
        assert state == good
        assert err == ""
        assert not p.with_suffix(".corrupt").exists()


def test_corrupt_json_warns_and_preserves() -> None:
    with _state_file('{"collectors": {"codex":') as p:      # truncated mid-write
        state, err = _load_capturing_stderr()
        assert state == {"collectors": {}}                  # still recovers by starting clean
        assert "unusable" in err and "re-pushes ALL history" in err
        # copied, not moved: save_state() overwrites the original at the end of the same run,
        # so the copy is the only thing that survives to be inspected.
        assert p.exists()
        assert p.with_suffix(".corrupt").read_text() == '{"collectors": {"codex":'


def test_wrong_shape_warns() -> None:
    for content in ("[1, 2, 3]", '{"collectors": "not-a-dict"}'):
        with _state_file(content) as p:
            state, err = _load_capturing_stderr()
            assert state == {"collectors": {}}
            assert "unusable" in err
            assert p.with_suffix(".corrupt").exists()


def test_atomic_write_keeps_the_old_file_on_failure(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "x.json"
        atomic_write_text(p, '{"a": 1}')
        assert load_json_dict(p) == {"a": 1}
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("no")))
        try:
            atomic_write_text(p, "new")
            raise AssertionError("the failed replace must raise")
        except OSError:
            pass
        assert p.read_text() == '{"a": 1}' and [f.name for f in p.parent.iterdir()] == ["x.json"]
        p.write_text("[1]")
        assert load_json_dict(p) == {} and load_json_dict(p.with_name("gone")) == {}


def test_config_errors_and_permission_warning(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.toml"
        monkeypatch.setenv("AUT_CONFIG", str(p))
        p.write_text("not = [valid")
        try:
            load_config()
            raise AssertionError("bad TOML must be an operator error")
        except AutError as exc:
            assert str(p) in str(exc) and "TOML" in str(exc), exc
        if os.name != "nt":
            monkeypatch.setenv("AUT_TEST_SET", "s")
            p.write_text('[transport]\nwebhook_secret = "env:AUT_TEST_SET"\n')
            p.chmod(0o644)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                load_config()
            assert err.getvalue() == "", "an env: reference holds no secret"
            p.write_text('[transport]\nwebhook_secret = "plain"\n')
            with contextlib.redirect_stderr(io.StringIO()) as err:
                load_config()
            assert "holds a secret" in err.getvalue(), err.getvalue()


def test_missing_psycopg_names_the_extra_and_spares_the_heartbeat(monkeypatch) -> None:
    cfg = Config(machine="m", agents=(), webhook_url=None, webhook_secret=None, pg_dsn="pg://x",
                 home=Path("."))
    row = dict.fromkeys(push.SCAN_COLUMNS)
    monkeypatch.setitem(sys.modules, "psycopg", None)   # makes `import psycopg` raise ImportError
    try:
        push.insert_pg("pg://x", [])
        raise AssertionError("a missing driver must be an operator error")
    except AutError as exc:
        assert "agent-usage-tracker[postgres]" in str(exc), exc
    with contextlib.redirect_stderr(io.StringIO()) as err:
        push.deliver_scan_rows(cfg, [row])      # best effort: returns
    assert "heartbeat not delivered" in err.getvalue()
    assert "agent-usage-tracker[postgres]" in err.getvalue(), err.getvalue()


def test_undecodable_state_recovers_like_any_corrupt_file() -> None:
    with _state_file(None) as p:
        p.write_bytes(b"\xff\xfe")
        state, err = _load_capturing_stderr()
        assert state == {"collectors": {}} and "unusable" in err
        assert p.with_suffix(".corrupt").read_bytes() == b"\xff\xfe"
        if os.name != "nt":
            assert p.with_suffix(".corrupt").stat().st_mode & 0o077 == 0


def test_a_save_never_rolls_back_another_process_watermark() -> None:
    with _state_file(json.dumps({"collectors": {"label": {"t": "old"}, "codex": {"off": 1}}})) as p:
        scan_copy, label_copy = load_state(), load_state()      # both started from the same file
        label_copy["collectors"]["label"]["t"] = "new"
        save_state(label_copy, ["label"])
        scan_copy["collectors"]["codex"]["off"] = 2
        save_state(scan_copy, ["codex"])
        assert json.loads(p.read_text())["collectors"] == {"label": {"t": "new"}, "codex": {"off": 2}}
        del scan_copy["collectors"]["codex"]
        save_state(scan_copy, ["codex"])                        # a name gone from state is dropped
        assert json.loads(p.read_text())["collectors"] == {"label": {"t": "new"}}
