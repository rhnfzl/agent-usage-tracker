"""Retain verified commits across offline sources without storing raw Git identities."""

from datetime import date, datetime, timezone
import json

import pytest

from agent_usage_tracker.git_day_cache import Snapshot, empty_cache, load_cache, merge_sources

D1, D2 = date(2026, 11, 1), date(2026, 11, 2)
NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def test_disjoint_and_overlapping_partial_reads_accumulate_once():
    first, _ = merge_sources(empty_cache('a' * 64), [
        Snapshot('b' * 64, 'complete', {'d' * 64: D1}, 4),
        Snapshot('c' * 64, 'deferred', {}, 0)], D1, D1, NOW)
    assert first['commits'] == {'d' * 64: D1.isoformat()}
    second, complete = merge_sources(first, [
        Snapshot('b' * 64, 'failed', {}, 0),
        Snapshot('c' * 64, 'complete', {'d' * 64: D1, 'e' * 64: D1}, 3)], D1, D2, NOW)
    assert len(second['commits']) == 2
    assert complete == {D1: True, D2: False}
    assert first['sources']['c' * 64]['status'] == 'deferred'


def test_incomplete_read_keeps_verified_commits_but_not_proven_zero_coverage():
    cache, complete = merge_sources(empty_cache('a' * 64), [
        Snapshot('b' * 64, 'partial', {'d' * 64: D1}, 1)], D1, D2, NOW)
    assert cache['commits'] and complete == {D1: False, D2: False}


def test_cache_drift_or_conflicting_commit_day_fails_closed(tmp_path):
    path = tmp_path / 'cache.json'
    cache = empty_cache('a' * 64)
    path.write_text(json.dumps(cache))
    assert load_cache(path, 'a' * 64) == cache
    with pytest.raises(ValueError):
        load_cache(path, 'b' * 64)
    cache['commits']['d' * 64] = D1.isoformat()
    with pytest.raises(ValueError):
        merge_sources(cache, [Snapshot('b' * 64, 'complete', {'d' * 64: D2}, 1)], D1, D2, NOW)
    path.write_text('{broken')
    with pytest.raises(ValueError):
        load_cache(path, 'a' * 64)


def test_reset_requires_every_source_complete():
    with pytest.raises(ValueError):
        merge_sources(empty_cache('a' * 64), [Snapshot('b' * 64, 'failed', {}, 0)],
                      D1, D2, NOW, reset=True)


def test_disjoint_coverage_never_proves_the_gap():
    cache, _ = merge_sources(empty_cache('a' * 64), [
        Snapshot('b' * 64, 'complete', {}, 1)], D1, D1, NOW)
    later = date(2026, 11, 4)
    cache, _ = merge_sources(cache, [Snapshot('b' * 64, 'complete', {}, 1)], later, later, NOW)
    _, complete = merge_sources(cache, [Snapshot('b' * 64, 'deferred', {}, 0)], D1, later, NOW)
    assert not complete[D2]


def test_cache_rejects_boolean_version(tmp_path):
    cache = empty_cache('a' * 64)
    cache['version'] = True
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps(cache))
    with pytest.raises(ValueError):
        load_cache(path, 'a' * 64)
