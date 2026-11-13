from agent_usage_tracker.observations import ingest, records, validate_record
import pytest


def usage():
    return dict(
        version=1,
        id="a",
        kind="usage",
        timestamp="2026-11-13T12:00:00Z",
        input_tokens=None,
        output_tokens=4,
        cache_read_tokens=None,
        cache_write_tokens=None,
        input_tokens_total=10,
    )


def test_dedup_conflict(tmp_path):
    db = tmp_path / "observations.sqlite3"
    assert ingest(db, [usage()], source="sdk", stream="one", role="primary")["accepted"] == 1
    assert ingest(db, [usage()], source="sdk", stream="one", role="primary")["duplicates"] == 1
    changed = usage()
    changed["output_tokens"] = 5
    assert ingest(db, [changed], source="sdk", stream="one", role="primary")["conflicts"] == 1
    assert records(db)[0]["input_tokens"] is None
    assert db.stat().st_mode & 0o777 == 0o600


def test_strict():
    for patch in ({"prompt": "private"}, {"output_tokens": True}, {"timestamp": "yesterday"}):
        value = usage()
        value.update(patch)
        with pytest.raises(ValueError):
            validate_record(value)


def test_primary_ownership_and_latest_quota(tmp_path):
    db = tmp_path / "db"
    ingest(db, [usage()], source="sdk", stream="one", role="primary")
    with pytest.raises(ValueError, match="overlapping primary"):
        ingest(db, [usage()], source="sdk", stream="two", role="primary")
    ingest(db, [usage()], source="sdk", stream="two", role="crosscheck")
    assert next(r for r in records(db) if r["role"] == "crosscheck")["additive"] is False
    quota = dict(
        version=1,
        kind="quota",
        id="q1",
        timestamp="2026-11-13T12:00:00Z",
        remaining=None,
        limit=100,
        unit="tokens",
        period="day",
        reset=None,
        fresh_until=None,
        scope="account",
    )
    ingest(db, [quota], source="sdk", stream="one", role="primary")
    quota.update(id="q2", timestamp="2026-11-13T13:00:00Z", remaining=40)
    ingest(db, [quota], source="sdk", stream="one", role="primary")
    quotas = [r for r in records(db) if r["kind"] == "quota"]
    assert [r["latest"] for r in quotas] == [False, True]
    assert quotas[0]["remaining"] is None


def test_limits_and_atomicity(tmp_path, monkeypatch):
    import agent_usage_tracker.observations as observations

    monkeypatch.setattr(observations, "MAX_BATCH_RECORDS", 1)
    with pytest.raises(ValueError, match="batch too large"):
        ingest(tmp_path / "db", [usage(), usage()], source="sdk", stream="one", role="primary")
    assert not (tmp_path / "db").exists()
    monkeypatch.setattr(observations, "MAX_FILE_BYTES", 2)
    path = tmp_path / "input"
    path.write_text("123")
    with pytest.raises(ValueError, match="file too large"):
        observations.import_file(
            tmp_path / "db", path, format="jsonl", source="sdk", stream="one", role="primary"
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": []},
        {"output_tokens": -1},
        {"output_tokens": float("inf")},
        {"version": True},
        {"id": "private text"},
        {"timestamp": "2026-11-13T12:00:00"},
    ],
)
def test_invalid_counts_and_identifiers(patch):
    value = usage()
    value.update(patch)
    with pytest.raises(ValueError):
        validate_record(value)


def test_disjoint_input_and_reasoning_subsets():
    value = usage()
    value.update(input_tokens=8, cache_read_tokens=8)
    with pytest.raises(ValueError, match="component"):
        validate_record(value)
    value = usage()
    value.update(reasoning_tokens=5)
    with pytest.raises(ValueError, match="reasoning"):
        validate_record(value)


def test_derived_components_and_reasoning(tmp_path):
    db = tmp_path / "db"
    for ident, stamp, total, fresh, read, output, reasoning in (
        ("a", "2026-11-13T12:00:00Z", 10, 4, 4, 10, 4),
        ("b", "2026-11-13T13:00:00Z", 15, 8, 8, 20, 8),
    ):
        row = dict(
            version=1,
            kind="usage",
            id=ident,
            timestamp=stamp,
            start="2026-11-13T11:00:00Z",
            temporality="cumulative",
            counter="one",
            input_tokens_total=total,
            input_tokens=fresh,
            cache_read_tokens=read,
            output_tokens=output,
            reasoning_tokens=reasoning,
        )
        if ident == "b":
            row.update(input_tokens_total=20)
        ingest(db, [row], source="sdk", stream="one", role="primary")
    rows = records(db)
    assert rows[1]["delta"]["reasoning_tokens"] == 4
    assert rows[1]["delta"]["output_tokens"] == 10

    db2 = tmp_path / "invalid_delta"
    for ident, stamp, total, fresh, read in (
        ("a", "2026-11-13T12:00:00Z", 10, 2, 2),
        ("b", "2026-11-13T13:00:00Z", 12, 4, 4),
    ):
        ingest(
            db2,
            [
                dict(
                    version=1,
                    kind="usage",
                    id=ident,
                    timestamp=stamp,
                    start="2026-11-13T11:00:00Z",
                    temporality="cumulative",
                    counter="one",
                    input_tokens_total=total,
                    input_tokens=fresh,
                    cache_read_tokens=read,
                )
            ],
            source="sdk",
            stream="one",
            role="primary",
        )
    assert records(db2)[1]["additive"] is False
    assert records(db2)[1]["coverage"] == "invalid_components"


def test_reasoning_delta_subset_and_ownership_atomicity(tmp_path):
    db = tmp_path / "db"
    for ident, stamp, output, reasoning in (
        ("first", "2026-11-13T12:00:00Z", 10, 4),
        ("second", "2026-11-13T13:00:00Z", 11, 6),
    ):
        row = dict(
            version=1,
            kind="usage",
            id=ident,
            timestamp=stamp,
            start="2026-11-13T11:00:00Z",
            temporality="cumulative",
            counter="one",
            output_tokens=output,
            reasoning_tokens=reasoning,
        )
        ingest(db, [row], source="sdk", stream="one", role="primary")
    assert records(db)[1]["coverage"] == "invalid_components"
    assert records(db)[1]["delta"] is None

    quota = dict(
        version=1,
        kind="quota",
        id="q",
        timestamp="2026-11-13T14:00:00Z",
        remaining=9,
        limit=10,
        unit="tokens",
        period="day",
        reset=None,
        fresh_until=None,
        scope="account",
    )
    with pytest.raises(ValueError, match="role"):
        ingest(db, [quota], source="sdk", stream="one", role="crosscheck")
    assert not any(r["kind"] == "quota" for r in records(db))


def test_cache_write_disjoint_when_fresh_known():
    value = usage()
    value.update(input_tokens=0, cache_read_tokens=8, cache_write_tokens=8)
    with pytest.raises(ValueError, match="component"):
        validate_record(value)
    value["input_tokens"] = None
    assert validate_record(value)["input_tokens"] is None


def test_conflict_does_not_pin_representation_or_counter(tmp_path):
    db = tmp_path / "db"
    tool = dict(version=1, kind="tool", id="a", timestamp="2026-11-13T12:00:00Z", status="ok")
    ingest(db, [tool], source="sdk", stream="one", role="primary")
    assert ingest(db, [usage()], source="sdk", stream="one", role="primary")["conflicts"] == 1
    counter = dict(
        version=1,
        kind="usage",
        id="c",
        timestamp="2026-11-13T13:00:00Z",
        start="2026-11-13T11:00:00Z",
        counter="one",
        temporality="cumulative",
        input_tokens_total=20,
    )
    assert ingest(db, [counter], source="sdk", stream="one", role="primary")["accepted"] == 1
    changed = dict(counter, id="a", timestamp="2026-11-13T15:00:00Z")
    assert ingest(db, [changed], source="sdk", stream="one", role="primary")["conflicts"] == 1
    later = dict(counter, id="d", timestamp="2026-11-13T14:00:00Z", input_tokens_total=25)
    assert ingest(db, [later], source="sdk", stream="one", role="primary")["accepted"] == 1
    assert records(db)[-1]["delta"]["input_tokens_total"] == 5
