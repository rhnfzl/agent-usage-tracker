"""Read T3's local projections in one read-only SQLite snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..collectors.activity_common import result_status
from ..event import parse_ts
from ..redact import redact
from .evidence import TEXT_LIMIT, VERSION, _text

MAX_ROWS = 20000
HUMAN_SOURCES = {"web", "mobile"}
# Item states after which T3 will add no output.
TERMINAL = {"completed", "failed", "error", "interrupted", "cancelled"}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshots(home: Path, *, since: float, limit: int, idle_s: int,
              max_bytes: int) -> tuple[list[dict[str, Any]], Counter[str]]:
    path = home / ".t3/userdata/statev2.sqlite"
    counts: Counter[str] = Counter()
    if not path.exists():
        return [], counts
    rows: list[dict[str, Any]] = []
    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            threads = db.execute("""SELECT t.thread_id, t.updated_at,
                    CASE WHEN length(CAST(t.payload_json AS BLOB))<=? THEN t.payload_json END,
                    p.workspace_root FROM orchestration_v2_projection_threads t
                LEFT JOIN projection_projects p ON p.project_id=t.project_id
                WHERE julianday(t.updated_at)>=julianday(?)
                ORDER BY EXISTS(SELECT 1 FROM orchestration_v2_projection_messages m
                    WHERE m.thread_id=t.thread_id AND m.role='user'
                    AND json_extract(m.payload_json,'$.createdBy')='user') DESC,
                    t.updated_at DESC, t.thread_id LIMIT ?""", (max_bytes, cutoff, limit + 1))
            for index, (thread, updated, payload, cwd) in enumerate(threads):
                if index >= limit:
                    counts['t3_unread_threads'] += 1
                    break
                if payload is None:
                    counts['t3_byte_capped_threads'] += 1
                    continue
                stamp = parse_ts(updated)
                if not stamp or not stamp.tzinfo:
                    counts['t3_invalid_threads'] += 1
                    continue
                if stamp.timestamp() > time.time() - idle_s:
                    counts['t3_active_threads'] += 1
                    continue
                meta = json.loads(payload)
                if not isinstance(meta, dict):
                    raise ValueError('invalid thread metadata')
                metadata_bytes = len(payload.encode())
                result = _thread(db, path, thread, meta.get('worktreePath') or cwd or '',
                                 since, max_bytes - metadata_bytes, counts)
                if result:
                    result['source']['bytes'] += metadata_bytes
                    result['source']['metadata_sha256'] = _hash(meta)
                    rows.append(result)
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        counts['t3_unreadable_sources'] += 1
    return rows, counts


def _thread(db: sqlite3.Connection, path: Path, thread: str, cwd: str, since: float,
            max_bytes: int, totals: Counter[str]) -> dict[str, Any] | None:
    coverage: Counter[str] = Counter()
    captured: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    used = 0

    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()

    def records(table: str, columns: str, order: str, where: str = '',
                params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        nonlocal used
        # Table and column names are internal constants; record bodies share one byte budget.
        rows = db.execute(f'SELECT {columns},substr(payload_json,1,?) FROM {table} '
                          f'WHERE thread_id=? {where} ORDER BY {order} LIMIT ?',
                          (max_bytes + 1, thread, *params, MAX_ROWS + 1))
        result = []
        for index, row in enumerate(rows):
            raw = row[-1]
            if index >= MAX_ROWS or used + len(raw.encode()) > max_bytes:
                coverage['byte_cap_reached'] = 1
                break
            used += len(raw.encode())
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('invalid projection record')
            record = {'table': table, 'key': row[0], 'columns': list(row[:-1]), 'value': value}
            captured.append(record)
            result.append(record)
        return result

    recent = ('SELECT run_id FROM orchestration_v2_projection_runs '
              'WHERE thread_id=? AND julianday(requested_at)>=julianday(?)')
    recent_providers = recent.replace('SELECT run_id', 'SELECT provider_thread_id')
    proven_human = ("EXISTS(SELECT 1 FROM orchestration_v2_projection_messages m "
        "WHERE m.message_id=json_extract(orchestration_v2_projection_runs.payload_json,"
        "'$.userMessageId') AND m.run_id=orchestration_v2_projection_runs.run_id "
        "AND m.role='user' AND json_extract(m.payload_json,'$.createdBy')='user' "
        "AND json_extract(m.payload_json,'$.creationSource') IN ('web','mobile'))")
    runs = records('orchestration_v2_projection_runs',
                   'run_id,provider,provider_thread_id,status,requested_at,completed_at,'
                   + proven_human, 'ordinal,run_id',
                   'AND julianday(requested_at)>=julianday(?)', (cutoff,))
    messages = records('orchestration_v2_projection_messages',
                       'message_id,run_id,role,created_at', 'created_at,message_id',
                       f'AND run_id IN ({recent}) AND role=\'user\' '
                       "AND json_extract(payload_json,'$.createdBy')='user' "
                       "AND json_extract(payload_json,'$.creationSource') IN ('web','mobile')",
                       (thread, cutoff))
    providers = records('orchestration_v2_projection_provider_threads',
                        'provider_thread_id,provider', 'provider_thread_id',
                        f'AND provider_thread_id IN ({recent_providers})', (thread, cutoff))
    items = records('orchestration_v2_projection_turn_items',
                    'turn_item_id,run_id,type,status,updated_at', 'updated_at DESC,ordinal DESC',
                    f'AND run_id IN ({recent}) AND type<>\'reasoning\'', (thread, cutoff))
    provider_map = {r['key']: r['value'] for r in providers}
    human = {r['key']: r for r in messages if r['columns'][2] == 'user'
             and r['value'].get('createdBy') == 'user'
             and r['value'].get('creationSource') in HUMAN_SOURCES}
    native: set[tuple[str, str]] = set()
    seen_providers: set[str] = set()
    run_coverage: dict[str, dict[str, Any]] = {}

    def emit(kind: str, value: Any, record: dict[str, Any], timestamp: Any,
             run: str, provider: str, *, call: str = '', tool: str = '',
             status: str = 'unknown') -> None:
        stamp = parse_ts(timestamp)
        if not stamp or not stamp.tzinfo:
            coverage['missing_timestamps'] += 1
            return
        raw = _text(value)
        text = redact(raw)
        clipped = len(text) > TEXT_LIMIT
        coverage['truncated_events'] += clipped
        events.append({'id': _hash([thread, run, record['key'], kind])[:32],
                       'kind': kind, 'line': None, 'byte_offset': None,
                       'record_id': record['key'], 'table': record['table'],
                       'timestamp': stamp.isoformat(), 'text': text[:TEXT_LIMIT],
                       'truncated': clipped, 'source_sha256': _hash(raw),
                       'call_id': call, 'tool': tool, 'status': status,
                       'run_id': run, 'provider': provider})

    for run in runs:
        before = coverage.copy()
        key, provider, provider_thread, status, requested, completed, proven = run['columns']
        prompt = human.get(run['value'].get('userMessageId'))
        if not proven:
            totals['t3_automated_runs'] += 1
            continue
        if not prompt or prompt['columns'][1] != key:
            totals['t3_uncaptured_runs'] += 1
            continue
        stamp = parse_ts(requested)
        if not stamp or not stamp.tzinfo:
            coverage['missing_timestamps'] += 1
            continue
        if stamp.timestamp() < since:
            continue
        seen_providers.add(provider)
        ref = provider_map.get(provider_thread, {}).get('nativeThreadRef') or {}
        if not isinstance(ref, dict):
            ref = {}
        if ref.get('strength') == 'strong' and isinstance(ref.get('nativeId'), str):
            agent = {'codex': 'codex', 'claudeAgent': 'claude_code'}.get(str(ref.get('driver')))
            if agent:
                native.add((agent, ref['nativeId']))
        for message in human.values():
            if message['columns'][1] == key:
                emit('prompt', message['value'].get('text', ''), message,
                     message['columns'][3], key, provider)
                if message['value'].get('attachments') or message['value'].get('context'):
                    coverage['projection_gaps'] += 1
        run_items = [item for item in items if item['columns'][1] == key]
        if not run_items:
            coverage['projection_gaps'] += 1
        for item in run_items:
            value = item['value']
            kind = value.get('type')
            at = value.get('startedAt') or item['columns'][4]
            end = value.get('completedAt') or item['columns'][4]
            if kind in {'command_execution', 'dynamic_tool', 'file_change', 'error'}:
                name = value.get('toolName') or str(kind)
                call_id = str(item['key'])
                body = ({'file': value.get('fileName'), 'diff': value.get('diffStr')}
                        if kind == 'file_change' else value.get('input', ''))
                emit('call', body, item, at, key, provider, call=call_id, tool=name)
                item_state = value.get('status')
                if kind in {'file_change', 'error'} or 'output' in value or item_state in TERMINAL:
                    # T3 omits the output key when a finished command printed nothing.
                    output = (value.get('failure') if kind == 'error' else
                              'File change reported ' + str(item_state)
                              if kind == 'file_change' else value.get('output', ''))
                    failed = item_state in {'failed', 'error'}
                    if isinstance(output, dict):
                        failed = failed or output.get('isError') is True
                    if kind in {'file_change', 'error'} or 'output' in value:
                        result = result_status(_text(output), failed)
                    elif type(value.get('exitCode')) is int:
                        result = result_status({'exit_code': value['exitCode']}, failed)
                    else:
                        result = ('error' if failed else
                                  'success' if item_state == 'completed' else 'unknown')
                    emit('result', output, item, end, key, provider, call=call_id,
                         status=result)
            elif kind == 'assistant_message':
                emit('reply', value.get('text', ''), item, end, key, provider)
                if value.get('streaming'):
                    coverage['projection_gaps'] += 1
            elif kind in {'compaction', 'handoff'}:
                emit('compaction', 'Context changed: ' + kind, item, at, key, provider)
            elif kind not in {'reasoning', 'user_message', 'checkpoint'}:
                # Secret and approval payloads are deliberately never normalized into text.
                coverage['projection_gaps'] += 1
        if status == 'completed' and completed:
            emit('boundary', 'turn_complete', run, completed, key, provider)
        else:
            coverage['incomplete_runs'] += 1
        run_coverage[key] = {'source_incomplete': bool(coverage['byte_cap_reached']) or any(
            coverage[name] > before[name] for name in (
                'projection_gaps', 'incomplete_runs', 'missing_timestamps')),
                'missing_timestamps': coverage['missing_timestamps'] - before['missing_timestamps']}
    totals['t3_byte_capped_threads'] += bool(coverage['byte_cap_reached'])
    if not events:
        return None
    # Keep each app run together even when provider clocks overlap during a handoff.
    order = {r['key']: index for index, r in enumerate(runs)}
    events.sort(key=lambda e: (order[e['run_id']], e['timestamp'],
                              e['kind'] == 'boundary', e['kind'] == 'reply'))
    calls = {e['call_id'] for e in events if e['kind'] == 'call'}
    results = {e['call_id'] for e in events if e['kind'] == 'result'}
    state: dict[str, Any] = dict(coverage)
    state['run_coverage'] = run_coverage
    state.update(calls=len(calls), matched_results=len(calls & results),
                 unmatched_calls=len(calls - results), orphan_results=len(results - calls),
                 truncated_events=coverage['truncated_events'],
                 byte_cap_reached=bool(coverage['byte_cap_reached']),
                 source_incomplete=bool(coverage['projection_gaps'] or coverage['incomplete_runs']
                                        or coverage['byte_cap_reached']))
    state['complete'] = not any(state.get(k) for k in (
        'unmatched_calls', 'orphan_results', 'truncated_events', 'byte_cap_reached',
        'source_incomplete', 'missing_timestamps'))
    return {'version': VERSION, 'agent': 't3', 'session_id': 't3:' + thread,
            'cwd': cwd, 'origin': 'interactive', 'model': None,
            'providers': sorted(seen_providers), 'native_sessions': sorted(native),
            'instruction_exposure': {'status': 'unknown',
                'reason': 'Project files alone do not prove past instruction delivery.'},
            'events': events, 'coverage': state,
            'source': {'path': str(path), 'format': 't3_projection_v2',
                       'hash_scope': 'selected logical projection rows, not database file bytes',
                       'sha256': _hash(captured), 'bytes': used, 'ranges': []}}
