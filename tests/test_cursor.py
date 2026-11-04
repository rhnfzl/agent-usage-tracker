"""Hermetic tests for the Cursor collector, which reads the local state database.

Run: `python tests/test_cursor.py`  (or `pytest`).
Locks the rule: Cursor joins bubbleId -> composerData for a timestamp and handles ISO + epoch-ms.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.collectors import cursor  # noqa: E402
from agent_usage_tracker.event import CollectorUnavailable  # noqa: E402
from test_collectors import _cfg, _drain  # noqa: E402


def test_cursor() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        db = home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("composerData:c1", json.dumps({"createdAt": 1700000000000})))
        # bubble WITHOUT its own timestamp -> must inherit from composerData
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b1", json.dumps(
                        {"bubbleId": "b1", "usageUuid": "u1",
                         "tokenCount": {"inputTokens": 100, "outputTokens": 20}})))
        # bubble WITH an ISO-string createdAt
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b2", json.dumps(
                        {"bubbleId": "b2", "createdAt": "2026-01-02T09:30:07.621Z",
                         "tokenCount": {"inputTokens": 5, "outputTokens": 5}})))
        # zero-token bubble -> ignored
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b3", json.dumps({"tokenCount": {"inputTokens": 0}})))
        con.commit()
        con.close()
        cfg = _cfg(home)
        cfg.cursor_db_path = str(db)   # platform-independent: point straight at the test db
        ev = _drain(cursor.collect, cfg)
        assert len(ev) == 2, ev
        e1 = next(e for e in ev if e.input_tokens == 100)
        assert e1.total_tokens == 120 and e1.ts is not None      # inherited composer ts
        e2 = next(e for e in ev if e.input_tokens == 5)
        assert e2.ts == "2026-01-02T09:30:07.621Z"               # ISO string kept as-is
        assert all(e.provider == "cursor" for e in ev)

        # cursor_human_only: keep the prompts, drop the token rows. This is what makes `cursor`
        # safe to run beside cursor_api -- the token rows are the ones that would double up.
        con = sqlite3.connect(db)
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b4", json.dumps({"bubbleId": "b4", "type": 1})))  # a prompt
        con.commit()
        con.close()
        both = _drain(cursor.collect, cfg)
        assert sorted(e.kind for e in both) == ["agent", "agent", "human"], both

        cfg.cursor_human_only = True
        only = _drain(cursor.collect, cfg)
        assert [e.kind for e in only] == ["human"], only
        assert only[0].total_tokens == 0 and only[0].ts is not None   # inherits composer ts
    print("ok cursor")


def test_cursor_composer_read_failure_degrades_and_keeps_the_rows() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.vscdb"
        real = sqlite3.connect(db)
        real.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        real.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:c1:b1", json.dumps(
            {"bubbleId": "b1", "createdAt": "2026-01-02T09:30:07.621Z",
             "tokenCount": {"inputTokens": 5, "outputTokens": 5}})))

        class Failing:
            def cursor(self):
                return self

            def execute(self, sql):
                if "composerData" in sql:
                    raise sqlite3.DatabaseError("malformed")
                return real.execute(sql)

            def close(self):
                real.close()

        cfg = _cfg(Path(d))
        cfg.cursor_db_path = str(db)
        saved, cursor.connect_ro = cursor.connect_ro, lambda path: Failing()
        rows: list = []
        try:
            for batch, _advance in cursor.collect(cfg, {}):
                rows += batch
            raise AssertionError("the failed read stayed silent")
        except CollectorUnavailable as exc:
            assert "malformed" in str(exc), exc
        finally:
            cursor.connect_ro = saved
        assert len(rows) == 1, rows
    print("ok cursor_composer_read_failure_degrades_and_keeps_the_rows")


def test_cursor_counts_malformed_bubbles() -> None:
    from agent_usage_tracker.collectors import cursor
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.vscdb"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        con.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:c:1', 'not json')")
        con.commit()
        con.close()
        cfg = _cfg(Path(d))
        cfg.cursor_db_path = str(db)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert _drain(cursor.collect, cfg) == []
        assert "1 malformed record(s) skipped" in err.getvalue(), err.getvalue()
    print("ok cursor_counts_malformed_bubbles")


def test_cursor_db_path() -> None:
    from agent_usage_tracker.collectors.cursor import cursor_db
    home = Path("/tmp/fakehome")
    override = _cfg(home)
    object.__setattr__(override, "cursor_db_path", "/custom/state.vscdb")
    # compare Paths, not strings: Windows renders the same path with backslashes
    assert cursor_db(override) == Path("/custom/state.vscdb")
    p = cursor_db(_cfg(home))
    assert p is not None
    assert p.parts[-4:] == ("Cursor", "User", "globalStorage", "state.vscdb")  # same subpath every OS
    assert str(home) in str(p) or "AppData" in str(p)
    print("ok cursor_db_path")


def test_cursor_counts_a_composer_with_bad_bytes(capsys):
    with tempfile.TemporaryDirectory() as t:
        db = Path(t) / "state.vscdb"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV (key TEXT, value BLOB)")
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:c1", b"\xff\xfe"))
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:c1:b1", json.dumps(
            {"createdAt": "2026-01-02T09:30:07Z",
             "tokenCount": {"inputTokens": 5, "outputTokens": 5}})))
        con.commit()
        con.close()
        cfg = _cfg(Path(t))
        cfg.cursor_db_path = str(db)
        assert [e.total_tokens for e in _drain(cursor.collect, cfg)] == [10]
    assert "1 malformed record(s)" in capsys.readouterr().err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
