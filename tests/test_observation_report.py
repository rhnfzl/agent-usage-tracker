from agent_usage_tracker.observation_report import build_report, public_draft, render_html
from agent_usage_tracker.observations import ingest


def test_partial_and_public(tmp_path):
    db = tmp_path / 'o.db'
    ingest(db, [dict(version=1, kind='usage', id='a', timestamp='2026-01-01T00:00:00Z', output_tokens=4)], source='private', stream='s', role='primary')
    report = build_report(db, now='2026-01-02T00:00:00Z')
    assert report['usage']['input_tokens']['total'] is None
    assert report['usage']['output_tokens']['total'] == 4
    assert 'private' not in str(public_draft(report))
    assert '&lt;script&gt;' in render_html({'value': '<script>'})


def test_counters_quota_and_crosscheck(tmp_path):
    db = tmp_path / 'o.db'
    base = dict(version=1, timestamp='2026-01-01T00:01:00Z')
    ingest(db, [dict(base, kind='usage', id='a', counter='c', temporality='cumulative', start='2026-01-01T00:00:00Z', output_tokens=10),
                dict(base, kind='quota', id='q', remaining=2, limit=10, scope='account', unit='requests', period='day', reset=None, fresh_until=None),
                dict(base, kind='tool', id='t', status='error')], source='desktop', stream='s', role='primary')
    ingest(db, [dict(base, kind='tool', id='t', status='error')], source='desktop', stream='other', role='crosscheck')
    ingest(db, [dict(base, timestamp='2026-01-01T00:02:00Z', kind='usage', id='b', counter='c', temporality='cumulative', start='2026-01-01T00:00:00Z', output_tokens=15)], source='desktop', stream='s', role='primary')
    report = build_report(db, now='2026-01-02T00:00:00Z')
    assert report['usage']['output_tokens']['total'] == 15
    assert report['quotas'][0]['freshness'] == 'unknown'
    assert len(report['tools']) == 2
    assert public_draft(report)['observed_tools']['other']['error'] == 1
    html = render_html(report)
    assert '<table>' in html and 'Remaining' in html and 'Freshness' in html


def test_tool_identity_collisions_and_public_operation_groups(tmp_path):
    db = tmp_path / 'o.db'
    row = dict(version=1, kind='tool', id='a', timestamp='2026-01-01T00:00:00Z', status='error')
    for source, stream, role in [('a/b', 'c', 'primary'), ('a', 'b/c', 'primary'),
                                 ('agent-desktop', 'commands', 'primary'),
                                 ('agent-desktop', 'mirror', 'crosscheck')]:
        ingest(db, [row], source=source, stream=stream, role=role)
    report = build_report(db)
    assert len(report['tools']) == 4
    public = public_draft(report)
    assert public['observed_tools'] == {
        'desktop': {'ok': 0, 'error': 1, 'unknown': 0},
        'other': {'ok': 0, 'error': 2, 'unknown': 0}}
