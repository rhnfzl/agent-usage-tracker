"""Private reports and a fixed aggregate-only local publication draft."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
from typing import Any

from .observations import COUNTS, records, timestamp


def build_report(db: Path, *, improve_root: Path | None = None,
                 now: str | None = None) -> dict[str, Any]:
    clock = timestamp(now) if now else datetime.now(timezone.utc)
    rows = records(db)
    usage = [r.get('delta') or r for r in rows if r['additive']]
    sums = {}
    for key in COUNTS:
        known = [r[key] for r in usage if r[key] is not None]
        missing = len(usage) - len(known)
        sums[key] = {'total': sum(known) if usage and not missing else None,
                     'known_sum': sum(known) if known else None, 'unknown_count': missing}
    quotas: dict[tuple[str, ...], dict[str, Any]] = {}
    tools: dict[str, Any] = {}
    for r in rows:
        if r['kind'] == 'quota':
            quota_key = tuple(r[k] for k in ('source', 'stream', 'scope', 'unit', 'period'))
            if quota_key not in quotas or timestamp(r['timestamp']) > timestamp(quotas[quota_key]['timestamp']):
                quotas[quota_key] = dict(r)
        if r['kind'] == 'tool':
            key = json.dumps([r['source'], r['stream'], r['role']], separators=(',', ':'))
            group = tools.setdefault(key, {'source': r['source'], 'stream': r['stream'],
                'role': r['role'], 'ok': 0, 'error': 0, 'unknown': 0,
                'operation_time_ms_known_sum': None, 'duration_unknown_count': 0})
            group[r['status']] += 1
            if r.get('duration_ms') is None:
                group['duration_unknown_count'] += 1
            else:
                group['operation_time_ms_known_sum'] = (group['operation_time_ms_known_sum'] or 0) + r['duration_ms']
    for r in quotas.values():
        r['freshness'] = ('unknown' if r['fresh_until'] is None else
                          'current' if timestamp(r['fresh_until']) >= clock else 'stale')
    studies = []
    desktop = []
    if improve_root is not None:
        from .improve.store import Store
        from .improve.studies import assess
        store = Store(improve_root)
        studies = [assess(store, key) for key in store.keys('studies')]
        desktop = [store.get('desktop_imports', key) for key in store.keys('desktop_imports')]
    return {'schema_version': 1, 'generated_at': clock.astimezone(timezone.utc).isoformat(),
        'data_through': max(timestamp(r['timestamp']) for r in rows).astimezone(timezone.utc).isoformat() if rows else None,
        'usage': sums, 'quotas': list(quotas.values()), 'tools': tools,
        'coverage': {'observations': len(rows), 'additive_usage': len(usage),
            'crosschecks': sum(r['role'] == 'crosscheck' for r in rows),
            'nonadditive_usage': sum(r['kind'] == 'usage' and not r['additive'] for r in rows),
            'reasons': dict(Counter(r['coverage'] for r in rows if 'coverage' in r)),
            'completeness': 'unknown'},
        'studies': studies, 'desktop_imports': desktop,
        'scope': 'Observed operations and bounded study evidence; no general productivity claim.'}


def public_draft(report: dict[str, Any]) -> dict[str, Any]:
    # Fixed count projection never copies identifiers or arbitrary report attributes.
    tools = [value for value in report['tools'].values() if value['role'] == 'primary']
    groups = {name: [r for r in tools if (r['source'] == 'agent-desktop') ==
                    (name == 'desktop')] for name in ('desktop', 'other')}
    return {'schema_version': 1, 'publication_status': 'local_draft',
        'generated_at': report['generated_at'], 'data_through': report['data_through'],
        'observed_tools': {name: {key: sum(r[key] for r in selected) if selected else None
                            for key in ('ok', 'error', 'unknown')}
                           for name, selected in groups.items()},
        'study_decisions': {key: sum(r['scoped_decision'] == key for r in report['studies'])
                            for key in ('supported', 'inconclusive', 'rejected')},
        'coverage': {'completeness': 'unknown',
                     'observations': report['coverage']['observations']}}


def render_html(report: dict[str, Any]) -> str:
    def table(rows: list[list[Any]], headings: list[str]) -> str:
        def cells(values: list[Any], tag: str) -> str:
            return ''.join('<' + tag + '>' + escape(
                'unavailable' if v is None else str(v)) + '</' + tag + '>' for v in values)
        return ('<div class="scroll"><table><thead><tr>' + cells(headings, 'th') +
                '</tr></thead><tbody>' + ''.join('<tr>' + cells(row, 'td') + '</tr>'
                for row in rows) + '</tbody></table></div>')

    body = '<h2>Usage</h2>' + table([[key, value['total'], value['known_sum'],
        value['unknown_count']] for key, value in report.get('usage', {}).items()],
        ['Component', 'Complete total', 'Known sum (lower bound)', 'Unknown records'])
    body += '<h2>Quota</h2>' + table([[r.get(k) for k in (
        'source', 'stream', 'role', 'scope', 'remaining', 'limit', 'unit', 'period',
        'reset', 'fresh_until', 'freshness')] for r in report.get('quotas', [])],
        ['Source', 'Stream', 'Role', 'Scope', 'Remaining', 'Limit', 'Unit', 'Period',
         'Reset', 'Fresh until', 'Freshness'])
    body += '<h2>Observed tools</h2>' + table([[r['source'], r['stream'], r['role'], r['ok'], r['error'], r['unknown'],
        r['operation_time_ms_known_sum'], r['duration_unknown_count']]
        for key, r in report.get('tools', {}).items()],
        ['Source', 'Stream', 'Role', 'OK', 'Error', 'Unknown', 'Known operation time (ms)', 'Unknown durations'])
    body += '<h2>Study evidence</h2>' + table([[r['study_id'], r['scoped_decision'],
        r['primary_metric'], json.dumps(r['observed_effects']), json.dumps(r['cost_inputs']),
        json.dumps(r['observed_cost'])] for r in report.get('studies', [])],
        ['Study', 'Scoped decision', 'Metric', 'Effects', 'Cost inputs', 'Observed cost'])
    body += '<h2>Coverage</h2><pre>' + escape(json.dumps(report.get('coverage'), indent=2)) + '</pre>'
    body += '<details><summary>Private report details</summary><pre>' + escape(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)) + '</pre></details>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>Private observations</title>'
        '<style>body{font:1rem system-ui;margin:2rem;max-width:80rem}.scroll,pre{overflow:auto}'
        'td,th{padding:.5rem;text-align:left;border:1px solid #aaa}table{border-collapse:collapse}'
        'pre{padding:1rem;background:#eee}h1{font-size:1.5rem}</style><main>'
        '<h1>Private observations</h1><p>Unavailable values are unknown. Known sums may be lower bounds. '
        'Operation time is not elapsed task time or human activity. Crosschecks remain separate. '
        'Study decisions apply only to bounded observations.</p>' + body + '</main></html>')
