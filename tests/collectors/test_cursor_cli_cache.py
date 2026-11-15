"""Closed chat caches skip repeated reads without acknowledging unseen rows."""

from __future__ import annotations

import copy
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_usage_tracker.collectors import cursor_cli
from agent_usage_tracker.diagnostics import capture
from agent_usage_tracker.event import CollectorUnavailable
from test_cursor_cli import C, _blob, _calls, _cfg, _collect, _prompt, _store


def chat(tmp_path, monkeypatch, **kwargs):
    path = _store(tmp_path, C, [[_prompt("request", "synthetic"),
                               _calls(("read", "Read", {}))]], **kwargs)
    opened = []
    connect = cursor_cli.connect_ro

    def observed(path):
        opened.append(path)
        return connect(path)

    monkeypatch.setattr(cursor_cli, "connect_ro", observed)
    return path, opened


def projected(watermark):
    value = copy.deepcopy(watermark)
    for entry in value.get("chats", {}).values():
        entry.pop("closed_source", None)
    return value


@pytest.mark.skipif(os.name == "nt", reason="cache requires POSIX metadata change time")
def test_acknowledged_unchanged_closed_chat_skips_sqlite(tmp_path, monkeypatch):
    _, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    first = _collect(tmp_path, watermark)
    assert sorted(row.kind for row in first) == ["human", "link", "steer", "turn"]
    before = copy.deepcopy(watermark)
    assert _collect(tmp_path, watermark) == []
    assert watermark == before
    assert len(opened) == 1


def test_unacknowledged_chat_replays_identical_rows(tmp_path, monkeypatch):
    _, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    first = [row for rows, _ in cursor_cli.collect(_cfg(tmp_path), watermark) for row in rows]
    assert watermark == {"chats": {}, "sdk": {}}
    assert _collect(tmp_path, watermark) == first
    assert len(opened) == 2


@pytest.mark.skipif(os.name == "nt", reason="cache requires POSIX metadata change time")
def test_legacy_closed_chat_seeds_cache_only_after_empty_batch_ack(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    watermark["chats"][str(path)].pop("closed_source", None)
    before = copy.deepcopy(watermark)
    [(rows, acknowledge)] = list(cursor_cli.collect(_cfg(tmp_path), watermark))
    assert rows == []
    assert watermark == before
    acknowledge()
    assert projected(watermark) == before
    assert "closed_source" in watermark["chats"][str(path)]
    assert _collect(tmp_path, watermark) == []
    assert len(opened) == 2


@pytest.mark.parametrize("sentinel", [None, [], "cached", {"main": 1}, [True, None]])
def test_missing_or_invalid_cache_signature_requires_read(tmp_path, monkeypatch, sentinel):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    watermark["chats"][str(path)]["closed_source"] = sentinel
    before = projected(watermark)
    assert _collect(tmp_path, watermark) == []
    assert len(opened) == 2
    assert projected(watermark) == before


def test_open_chat_never_caches_its_source(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch, mtime=time.time())
    watermark = {}
    assert _collect(tmp_path, watermark) == []
    assert _collect(tmp_path, watermark) == []
    assert watermark["chats"][str(path)]["open"] == 1
    assert "closed_source" not in watermark["chats"][str(path)]
    assert len(opened) == 2


def test_activity_errors_are_not_cached_away(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)

    def broken(*args):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(cursor_cli._Activity, "record", broken)
    watermark = {}
    _collect(tmp_path, watermark)
    with capture() as observed:
        assert _collect(tmp_path, watermark) == []
    assert observed.skipped == {"activity_errors": 1}
    assert "closed_source" not in watermark["chats"][str(path)]
    assert len(opened) == 2


def test_journal_presence_clears_cache_without_changing_rows(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    before = projected(watermark)
    Path(str(path) + "-journal").touch()
    assert _collect(tmp_path, watermark) == []
    assert len(opened) == 2
    assert "closed_source" not in watermark["chats"][str(path)]
    assert projected(watermark) == before


@pytest.mark.parametrize("change", ["replace", "mode", "wal_replace"])
def test_file_identity_and_mode_changes_require_fresh_read(tmp_path, monkeypatch, change):
    path, opened = chat(tmp_path, monkeypatch)
    if change == "wal_replace":
        Path(str(path) + "-wal").touch()
    watermark = {}
    _collect(tmp_path, watermark)
    before = projected(watermark)
    if change == "mode":
        path.chmod(0o640 if path.stat().st_mode & 0o777 != 0o640 else 0o600)
    else:
        changed = Path(str(path) + "-wal") if change == "wal_replace" else path
        replacement = changed.with_name("replacement")
        replacement.write_bytes(changed.read_bytes())
        old = changed.stat()
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(changed)
    assert _collect(tmp_path, watermark) == []
    assert len(opened) == 2
    assert projected(watermark) == before


@pytest.mark.parametrize("change", ["truncate", "restored_mtime", "missing_table"])
def test_changed_invalid_source_cannot_hide_behind_cache(tmp_path, monkeypatch, change):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    before = copy.deepcopy(watermark)
    if change == "missing_table":
        with sqlite3.connect(path) as writer:
            writer.execute("DROP TABLE blobs")
        writer.close()
    else:
        old = path.stat()
        path.write_bytes(bytes(old.st_size) if change == "restored_mtime" else b"invalid")
        if change == "restored_mtime":
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(CollectorUnavailable):
        _collect(tmp_path, watermark)
    assert len(opened) == 2
    assert watermark == before


def test_changed_root_emits_new_turn_with_existing_ids_stable(tmp_path, monkeypatch):
    _, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    first = _collect(tmp_path, watermark)
    _store(tmp_path, C, [[_prompt("request", "synthetic"), _calls(("read", "Read", {})),
                         _prompt("next", "synthetic")]])
    second = _collect(tmp_path, watermark)
    assert sorted(row.kind for row in second) == ["human", "link", "steer", "turn"]
    assert {row.event_id for row in first}.isdisjoint(row.event_id for row in second)
    assert len(opened) == 2


def test_wal_commit_and_checkpoint_force_reads(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE meta SET value=value WHERE key='0'")
        writer.commit()
        watermark = {}
        _collect(tmp_path, watermark)
        before = projected(watermark)
        writer.execute("CREATE TABLE sentinel(value TEXT)")
        writer.commit()
        assert _collect(tmp_path, watermark) == []
        assert len(opened) == 2
        assert projected(watermark) == before
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert _collect(tmp_path, watermark) == []
        assert len(opened) == 3
        assert projected(watermark) == before
    finally:
        writer.close()


@pytest.mark.skipif(os.name == "nt", reason="cache requires POSIX metadata change time")
def test_source_changed_during_read_cannot_seed_cache(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)
    read = cursor_cli._closed_turn_rows

    def changing(*args):
        result = read(*args)
        with sqlite3.connect(path) as writer:
            writer.execute("CREATE TABLE sentinel(value TEXT)")
        writer.close()
        return result

    monkeypatch.setattr(cursor_cli, "_closed_turn_rows", changing)
    watermark = {}
    _collect(tmp_path, watermark)
    assert "closed_source" not in watermark["chats"][str(path)]
    monkeypatch.setattr(cursor_cli, "_closed_turn_rows", read)
    assert _collect(tmp_path, watermark) == []
    assert len(opened) == 2
    assert "closed_source" in watermark["chats"][str(path)]


def test_missing_metadata_clears_stale_cache(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM meta")
    writer.close()
    before = projected(watermark)
    assert _collect(tmp_path, watermark) == []
    assert len(opened) == 2
    assert "closed_source" not in watermark["chats"][str(path)]
    assert projected(watermark) == before


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="requires POSIX file permission enforcement")
def test_permission_loss_and_recovery_require_fresh_reads(tmp_path, monkeypatch):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    before = copy.deepcopy(watermark)
    mode = path.stat().st_mode
    try:
        path.chmod(0)
        with pytest.raises(CollectorUnavailable):
            _collect(tmp_path, watermark)
        assert watermark == before
    finally:
        path.chmod(mode)
    assert _collect(tmp_path, watermark) == []
    assert projected(watermark) == projected(before)
    assert len(opened) == 3


@pytest.mark.parametrize("platform", ["native", "win32"])
@pytest.mark.parametrize("readable", [False, True])
def test_nonregular_wal_forces_fresh_reads_without_caching(
        tmp_path, monkeypatch, platform, readable):
    connect = cursor_cli.connect_ro
    if platform == "win32":
        monkeypatch.setattr(cursor_cli, "sys", SimpleNamespace(platform="win32"))
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    before = copy.deepcopy(watermark)
    readable_copy = tmp_path / "readable.db"
    readable_copy.write_bytes(path.read_bytes())
    Path(str(path) + "-wal").mkdir()

    def sqlite_boundary(source):
        opened.append(source)
        return connect(readable_copy) if readable else None

    monkeypatch.setattr(cursor_cli, "connect_ro", sqlite_boundary)
    for _ in range(2):
        assert cursor_cli._closed_source(str(path)) is None
        if readable:
            assert _collect(tmp_path, watermark) == []
            assert projected(watermark) == projected(before)
            assert "closed_source" not in watermark["chats"][str(path)]
        else:
            with pytest.raises(CollectorUnavailable):
                _collect(tmp_path, watermark)
            assert watermark == before
    assert len(opened) == 3


@pytest.mark.parametrize("readable", [False, True])
def test_windows_reads_unchanged_closed_store_instead_of_trusting_cache(
        tmp_path, monkeypatch, readable):
    path, opened = chat(tmp_path, monkeypatch)
    watermark = {}
    _collect(tmp_path, watermark)
    before = copy.deepcopy(watermark)
    metadata = path.stat()
    monkeypatch.setattr(cursor_cli, "sys", SimpleNamespace(platform="win32"), raising=False)
    if readable:
        assert _collect(tmp_path, watermark) == []
        assert projected(watermark) == projected(before)
    else:
        def unavailable(path):
            opened.append(path)
            return None

        monkeypatch.setattr(cursor_cli, "connect_ro", unavailable)
        with pytest.raises(CollectorUnavailable):
            _collect(tmp_path, watermark)
        assert watermark == before
    current = path.stat()
    assert (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
            current.st_ctime_ns, current.st_mode) == (
                metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
                metadata.st_ctime_ns, metadata.st_mode)
    assert len(opened) == 2


@pytest.mark.parametrize("gap,expected", [
    ("root_missing", {"missing_payloads": 1}),
    ("root_null", {"missing_payloads": 1}),
    ("message_missing", {"missing_payloads": 1}),
    ("message_null", {"missing_payloads": 1}),
    ("message_bad_json", {"malformed_records": 1}),
    ("message_truncated_protobuf", {"malformed_records": 1}),
    ("message_protobuf", {}),
])
def test_required_source_gaps_remain_visible_and_uncached(tmp_path, monkeypatch, gap, expected):
    path, opened = chat(tmp_path, monkeypatch)
    assistant = _blob(_calls(("read", "Read", {})))
    with sqlite3.connect(path) as writer:
        if gap == "root_missing":
            writer.execute("DELETE FROM blobs WHERE substr(data,1,2)=X'0A20'")
        elif gap == "root_null":
            writer.execute("UPDATE blobs SET data=NULL WHERE substr(data,1,2)=X'0A20'")
        elif gap == "message_missing":
            writer.execute("DELETE FROM blobs WHERE id=?", (assistant,))
        else:
            payload = {"message_null": None, "message_bad_json": b'{"role":',
                       "message_truncated_protobuf": b"\x12\x05o",
                       "message_protobuf": b"\x12\x05other"}[gap]
            writer.execute("UPDATE blobs SET data=? WHERE id=?", (payload, assistant))
    writer.close()
    old = time.time() - 7200
    os.utime(path, (old, old))
    watermark = {}
    with capture() as first:
        rows = _collect(tmp_path, watermark)
    assert any(row.kind == "human" for row in rows)
    assert first.skipped == expected
    if expected:
        assert "closed_source" not in watermark["chats"][str(path)]
        with capture() as second:
            assert _collect(tmp_path, watermark) == []
        assert second.skipped == expected
        assert len(opened) == 2
    elif os.name != "nt":
        assert "closed_source" in watermark["chats"][str(path)]


@pytest.mark.parametrize("payload", [9223372036854775807, 8])
@pytest.mark.parametrize("position", ["current", "previous"])
def test_integer_root_payloads_never_convert_to_byte_buffers(
        tmp_path, monkeypatch, payload, position):
    path, _ = chat(tmp_path, monkeypatch)
    watermark = {}
    with sqlite3.connect(path) as writer:
        if position == "current":
            writer.execute("UPDATE blobs SET data=? WHERE substr(data,1,2)=X'0A20'", (payload,))
        else:
            previous = "f" * 64
            writer.execute("INSERT INTO blobs VALUES (?, ?)", (previous, payload))
            watermark["chats"] = {str(path): {"root": previous, "open": 1, "snapshots": {}}}
    writer.close()
    old = time.time() - 7200
    os.utime(path, (old, old))

    if payload == 8:
        class BytesType(type):
            def __instancecheck__(cls, value):
                return isinstance(value, bytes)

        class GuardedBytes(metaclass=BytesType):
            fromhex = staticmethod(bytes.fromhex)

            def __new__(cls, value):
                assert not isinstance(value, int), "integer reached bytes conversion"
                return bytes(value)

        monkeypatch.setattr(cursor_cli, "bytes", GuardedBytes, raising=False)

    with capture() as observed:
        rows = _collect(tmp_path, watermark)
    assert any(row.kind == "human" for row in rows)
    if position == "current":
        assert observed.skipped == {"malformed_records": 1}
        assert "closed_source" not in watermark["chats"][str(path)]
        with capture() as repeated:
            assert _collect(tmp_path, watermark) == []
        assert repeated.skipped == {"malformed_records": 1}
    else:
        assert observed.skipped == {}
        assert sorted(row.kind for row in rows) == ["human", "link", "steer", "turn"]
