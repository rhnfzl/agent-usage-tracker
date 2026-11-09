"""Covered turn snapshots grow without changing identity or replacing a newer copy."""

import json
from dataclasses import asdict, replace

import pytest

from agent_usage_tracker import push
from fake_psycopg import _fake_psycopg
from test_sink import _act


class Cursor:
    def __init__(self, existing, can_update=True):
        self.existing = existing
        self.can_update = can_update
        self.privilege_checks = 0
        self.updates = []
        self.inserts = []
        self.locks = []

    def execute(self, sql, params):
        if not self.can_update and ("FOR UPDATE" in sql or sql.startswith("UPDATE")):
            raise AssertionError("INSERT+SELECT role cannot execute privileged SQL")
        if "advisory" in sql:
            self.locks.append(params)
        elif sql.startswith("UPDATE"):
            self.updates.append((sql, params))
        elif "has_column_privilege" in sql:
            self.privilege_checks += 1

    def fetchall(self):
        return self.existing

    def fetchone(self):
        return (self.can_update,)

    def executemany(self, sql, params):
        self.inserts.extend(params)


@pytest.mark.parametrize("old_sources,new_sources,old_calls,new_calls,allowed", [
    (["a"], ["a", "b"], 1, 2, True),
    (["a", "b"], ["a"], 2, 1, False),
    (["a"], ["b"], 1, 2, False),
    (["a", "b"], ["a", "b"], 2, 1, False),
    (None, ["a", "b"], 1, 2, True),
    (None, ["a"], 2, 1, False),
])
def test_snapshot_coverage_gates_updates(old_sources, new_sources, old_calls, new_calls, allowed):
    attrs = {"tool_calls": old_calls, "tool_errors": 0}
    if old_sources is not None:
        attrs["revision_sources"] = old_sources
    cur = Cursor([("legacy-end-ts", "fake", "turn", "test", attrs)])
    row = replace(_act(), kind="turn", attrs={"tool_calls": new_calls, "tool_errors": 0,
                                               "revision_sources": new_sources})
    push._write_activities(cur, [asdict(row)])
    assert bool(cur.updates) == allowed
    assert not cur.inserts
    assert len(cur.locks) == 1
    if allowed:
        assert "legacy-end-ts" in cur.updates[0][1]
        assert json.loads(cur.updates[0][1][0]) == row.attrs


def test_snapshot_identity_collision_fails_closed():
    cur = Cursor([("ts", "another-agent", "turn", "test", {})])
    row = replace(_act(), kind="turn", attrs={"revision_sources": ["a"]})
    with pytest.raises(push.DeliveryError, match="identity"):
        push._write_activities(cur, [asdict(row)])


def test_plain_activity_stays_insert_only():
    cur = Cursor([])
    push._write_activities(cur, [asdict(_act())])
    assert len(cur.inserts) == 1
    assert not cur.locks and not cur.updates


def test_copied_session_can_extend_a_snapshot_without_changing_its_machine():
    cur = Cursor([("ts", "fake", "turn", "old-machine", {
        "tool_calls": 1, "tool_errors": 0, "revision_sources": ["a"]})])
    row = replace(_act(), kind="turn", machine="new-machine", attrs={
        "tool_calls": 2, "tool_errors": 0, "revision_sources": ["a", "b"]})
    push._write_activities(cur, [asdict(row)])
    assert len(cur.updates) == 1
    assert "SET attrs=" in cur.updates[0][0]
    assert not cur.inserts


def test_insert_only_role_accepts_initial_snapshot_without_privilege_query():
    cur = Cursor([], can_update=False)
    row = replace(_act(), kind="turn", attrs={"revision_sources": ["a"]})
    assert push._write_activities(cur, [asdict(row)]) == []
    assert len(cur.inserts) == 1
    assert cur.privilege_checks == 0


def test_missing_update_privilege_defers_then_replays_without_starving_other_rows(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    cur = Cursor([("legacy-end-ts", "fake", "turn", "test",
                   {"tool_calls": 1, "revision_sources": ["a"]})], can_update=False)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            class Context:
                def __enter__(self):
                    return cur

                def __exit__(self, *args):
                    pass
            return Context()

        def commit(self):
            pass

    monkeypatch.setattr(push, "connect", lambda *args: Connection())
    full = replace(_act(), kind="turn", attrs={"tool_calls": 2,
                                               "revision_sources": ["a", "b"]})
    push.send_activity_rows("postgresql://x", [full, replace(_act(), event_id="ordinary")])
    assert push.outbox_status() == {"activity": 1}
    assert not cur.updates
    assert len(cur.inserts) == 1
    assert "UPDATE(attrs)" in capsys.readouterr().err
    # A blocked row at the front must not prevent later batches from reaching the sink.
    push._queue_rest([replace(_act(), event_id=f"later{i}") for i in range(501)])
    from test_sink import _cfg
    assert push.replay_outbox(_cfg(webhook=None)) == 501
    assert push.outbox_status() == {"activity": 1}
    assert len(cur.inserts) == 502
    cur.can_update = True
    assert push.replay_outbox(_cfg(webhook=None)) == 1
    assert push.outbox_status() == {}
    assert len(cur.updates) == 1


def test_deferral_storage_failure_never_acknowledges_snapshot(monkeypatch):
    row = replace(_act(), kind="turn", attrs={"revision_sources": ["a"]})
    monkeypatch.setattr(push, "_write_activities", lambda cur, rows: rows)

    def fail(batch):
        raise push.DeliveryError("outbox storage failure")

    monkeypatch.setattr(push, "_queue_rest", fail)
    log = []
    with _fake_psycopg(log), pytest.raises(push.DeliveryError, match="storage failure"):
        push.send_activity_rows("postgresql://x", [row])
    assert log[-1] == "commit"
