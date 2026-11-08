"""Keep verified commits when a machine is offline, using private salted identities."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Literal, TypedDict, cast

Status = Literal['complete', 'partial', 'failed', 'deferred']
STATUSES = {'complete', 'partial', 'failed', 'deferred'}


class SourceState(TypedDict):
    complete_from: str | None
    complete_until: str | None
    stores: list[int]
    status: str
    attempted_at: str


class Cache(TypedDict):
    version: int
    key_id: str
    commits: dict[str, str]
    sources: dict[str, SourceState]
    baseline_sources: list[str]


@dataclass
class Snapshot:
    source_id: str
    status: Status
    commits: dict[str, date]
    stores: int


def _key(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def empty_cache(key_id: str) -> Cache:
    if not _key(key_id):
        raise ValueError('Invalid Git cache identity')
    return {'version': 1, 'key_id': key_id, 'commits': {}, 'sources': {}, 'baseline_sources': []}


def _require(valid: bool) -> None:
    if not valid:
        raise ValueError('Invalid Git cache structure')


def load_cache(path: Path, key_id: str, *, reset: bool = False) -> Cache:
    if reset or not path.exists():
        return empty_cache(key_id)
    try:
        raw = json.loads(path.read_text())
        _require(isinstance(raw, dict) and type(raw['version']) is int
                 and raw['version'] == 1 and raw['key_id'] == key_id)
        _require(isinstance(raw['commits'], dict) and isinstance(raw['sources'], dict))
        _require(isinstance(raw['baseline_sources'], list)
                 and all(_key(value) for value in raw['baseline_sources'])
                 and len(set(raw['baseline_sources'])) == len(raw['baseline_sources']))
        for key, value in raw['commits'].items():
            _require(_key(key) and isinstance(value, str))
            date.fromisoformat(value)
        for key, source in raw['sources'].items():
            _require(_key(key) and isinstance(source, dict) and source['status'] in STATUSES)
            _require(isinstance(source['stores'], list) and len(source['stores']) <= 5)
            _require(all(type(n) is int and n > 0 for n in source['stores']))
            first, last = source['complete_from'], source['complete_until']
            _require((first is None) == (last is None))
            if first is not None:
                _require(date.fromisoformat(first) <= date.fromisoformat(last))
            _require(datetime.fromisoformat(source['attempted_at']).utcoffset() is not None)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('Git identity cache is unreadable, corrupt, or uses another salt/policy') from exc
    return cast(Cache, raw)


def merge_sources(cache: Cache, snapshots: list[Snapshot], since: date, until: date,
                  now: datetime, *, reset: bool = False) -> tuple[Cache, dict[date, bool]]:
    if not snapshots or len({s.source_id for s in snapshots}) != len(snapshots):
        raise ValueError('Git sources must be nonempty and unique')
    if now.utcoffset() is None:
        raise ValueError('Git source observations need an aware timestamp')
    if reset and any(s.status != 'complete' for s in snapshots):
        raise ValueError('A Git identity reset needs every source complete')
    updated = empty_cache(cache['key_id']) if reset else copy.deepcopy(cache)
    for snapshot in snapshots:
        if (not _key(snapshot.source_id) or snapshot.status not in STATUSES
                or type(snapshot.stores) is not int or snapshot.stores < 0
                or (snapshot.status == 'complete' and not snapshot.stores)
                or (snapshot.status in ('failed', 'deferred') and snapshot.commits)):
            raise ValueError('Invalid Git source observation')
        source = updated['sources'].setdefault(snapshot.source_id, {
            'complete_from': None, 'complete_until': None, 'stores': [],
            'status': snapshot.status, 'attempted_at': now.isoformat()})
        source['status'], source['attempted_at'] = snapshot.status, now.isoformat()
        for key, day in snapshot.commits.items():
            if not _key(key) or type(day) is not date:
                raise ValueError('Invalid salted Git commit observation')
            if not since <= day <= until:
                continue
            previous = updated['commits'].get(key)
            if previous is not None and previous != day.isoformat():
                raise ValueError('One Git commit has conflicting author days')
            updated['commits'][key] = day.isoformat()
        if snapshot.status == 'complete':
            source['stores'] = source['stores'][-4:] + [snapshot.stores]
            if since <= until:
                first, last = source['complete_from'], source['complete_until']
                if (first is None or last is None
                        or since > date.fromisoformat(last) + timedelta(days=1)
                        or until < date.fromisoformat(first) - timedelta(days=1)):
                    source['complete_from'], source['complete_until'] = since.isoformat(), until.isoformat()
                else:
                    source['complete_from'] = min(first, since.isoformat())
                    source['complete_until'] = max(last, until.isoformat())
    days = [since + timedelta(days=i) for i in range(max(0, (until - since).days + 1))]
    complete = {day: all(
        (source := updated['sources'][snapshot.source_id])['complete_from'] is not None
        and str(source['complete_from']) <= day.isoformat() <= str(source['complete_until'])
        for snapshot in snapshots) for day in days}
    return updated, complete
