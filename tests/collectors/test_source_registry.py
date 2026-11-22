"""Explicit native roots reach all existing readers without changing native evidence."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_usage_tracker.collectors import claude_code, codex, pi
from agent_usage_tracker.event import CollectorUnavailable, UsageEvent
from codex_pi_helpers import T, _collect, _hdr, _meta, _pa, _tok, _write_jsonl


def registry(home, **values):
    path = home / '.config/agent-usage-tracker/sources.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 1, **values}))
    return path


def source(root, engine='codex', id='custom'):
    return {'id': id, 'harness': 'unknown-wrapper', 'engine': engine,
            'location': 'local', 'home': str(root)}


def test_registry_keeps_unknown_declarations_and_distinct_session_associations(tmp_path):
    from agent_usage_tracker.sources import load_registry

    edge = {'source_id': 'custom', 'native_session_id': 'native', 'harness_thread_id': 'thread'}
    registry(tmp_path, sources=[source(tmp_path / 'custom', engine='new-engine')],
             links=[edge, edge, {**edge, 'request_id': 'request'}],
             publications=[{'id': 'ledger', 'path': str(tmp_path / 'receipt.json'),
                            'timezone': 'Europe/Amsterdam'}])
    result = load_registry(tmp_path)
    assert result.sources[0].engine == 'new-engine'
    assert result.sources[0].home == tmp_path / 'custom'
    assert result.links == (edge, {**edge, 'request_id': 'request'})
    assert result.publications[0]['timezone'] == 'Europe/Amsterdam'


@pytest.mark.parametrize('values', [
    {'version': True}, {'version': 2}, {'text': 'PRIVATE'},
    {'sources': [source(Path('PRIVATE-relative'))]},
    {'sources': [source(Path('/a')), source(Path('/b'))]},
    {'sources': [{**source(Path('/a')), 'text': 'PRIVATE'}]},
    {'sources': [{**source(Path('/a')), 'transport': 'PRIVATE-invalid'}]},
    {'sources': [{**source(Path('/a')), 'location': 'remote'}]},
    {'links': [{'source_id': 'missing', 'native_session_id': 's', 'harness_thread_id': 'h'}]},
    {'publications': [{'id': 'pub', 'path': '/tmp/receipt', 'timezone': 'PRIVATE-invalid'}]},
    {'sources': [source(Path('/a'), id=f's{i}') for i in range(129)]},
    {'links': [{}] * 10001}, {'publications': [{}] * 33},
])
def test_invalid_registry_is_explicit_without_echoing_content(tmp_path, values):
    from agent_usage_tracker.sources import SourceError, load_registry

    registry(tmp_path, **values)
    with pytest.raises(SourceError) as error:
        load_registry(tmp_path)
    assert 'PRIVATE' not in str(error.value)
    with pytest.raises(CollectorUnavailable):
        codex.session_files(tmp_path)


def test_registry_missing_empty_and_bounded_reads(tmp_path):
    from agent_usage_tracker.sources import Registry, SourceError, load_registry

    assert load_registry(tmp_path) == Registry((), (), ())
    path = registry(tmp_path)
    assert load_registry(tmp_path) == Registry((), (), ())
    for content in ('{', ' ' * (1024 * 1024 + 1)):
        path.write_text(content)
        with pytest.raises(SourceError):
            load_registry(tmp_path)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO needs POSIX')
@pytest.mark.parametrize('relative', ['.config/agent-usage-tracker/sources.json',
                                     '.t3/userdata/settings.json'])
def test_config_special_files_fail_without_waiting_for_a_writer(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path; from agent_usage_tracker.sources import engine_homes, SourceError; '
        '\ntry: engine_homes(Path(__import__("sys").argv[1]), "claude_code")'
        '\nexcept SourceError: raise SystemExit(0)'
        '\nraise SystemExit(1)', str(tmp_path)], capture_output=True, timeout=2,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')})
    assert result.returncode == 0, result.stderr.decode()


def test_live_overrides_do_not_leak_into_alternate_home(tmp_path, monkeypatch):
    from agent_usage_tracker.sources import engine_homes, registry_path

    live, other = tmp_path / 'live', tmp_path / 'other'
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: live))
    monkeypatch.setenv('AUT_CONFIG', str(tmp_path / 'config/config.toml'))
    monkeypatch.setenv('AUT_SOURCES', str(tmp_path / 'override.json'))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-env'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-env'))
    assert registry_path(live) == tmp_path / 'override.json'
    assert registry_path(other) == other / '.config/agent-usage-tracker/sources.json'
    assert engine_homes(live, 'codex') == [live / '.codex', tmp_path / 'codex-env']
    assert engine_homes(live, 'claude_code') == [live / '.claude', tmp_path / 'claude-env']
    assert engine_homes(other, 'codex') == [other / '.codex']
    monkeypatch.delenv('AUT_SOURCES')
    assert registry_path(live) == tmp_path / 'config/sources.json'
    monkeypatch.setenv('AUT_SOURCES', 'PRIVATE-relative')
    from agent_usage_tracker.sources import SourceError
    with pytest.raises(SourceError):
        registry_path(live)


@pytest.mark.parametrize('engine,mod', [('claude_code', claude_code), ('codex', codex), ('pi', pi)])
def test_registered_roots_reach_collection_and_context_once(tmp_path, engine, mod):
    from agent_usage_tracker.label.context import session_paths

    home, root = tmp_path / 'home', tmp_path / 'relocated'
    root.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    registry(home, sources=[source(root, engine), source(alias, engine, id='alias')])
    if engine == 'claude_code':
        path = root / 'projects/p/native.jsonl'
        records = [{'type': 'assistant', 'uuid': 'a', 'sessionId': 'native',
                    'timestamp': T(2), 'entrypoint': 'sdk-py', 'message': {
                        'id': 'm', 'model': 'claude', 'usage': {'input_tokens': 2}}}]
    elif engine == 'codex':
        path, records = root / 'sessions/rollout-native.jsonl', [_meta('native'), _tok(T(2))]
    else:
        path, records = root / 'sessions/native.jsonl', [_hdr('native', '/x'), _pa('a', T(2), 'stop', tokens=7)]
    _write_jsonl(path, records)
    rows = [r for r in _collect(mod, home) if isinstance(r, UsageEvent)]
    assert len(rows) == 1
    assert rows[0].session.session_id == 'native'
    assert session_paths(home, engine) == {'native': [(0, str(path))]}
    if engine == 'claude_code':
        assert rows[0].session.run_origin == 'automated'


def test_registered_codex_imports_exclude_imported_prefix(tmp_path):
    home, root = tmp_path / 'home', tmp_path / 'codex-profile'
    registry(home, sources=[source(root)])
    _write_jsonl(root / 'sessions/rollout-native.jsonl', [
        _meta('native'), _tok(T(1)), _tok(T(3))])
    bound = int(datetime(2026, 7, 18, 10, 0, 1, tzinfo=timezone.utc).timestamp())
    (root / 'external_agent_session_imports.json').write_text(json.dumps({
        'records': [{'imported_thread_id': 'native', 'imported_at': bound}]}))
    assert codex.imported_until(home)['native'].isoformat() == '2026-07-18T10:00:02+00:00'
    assert len([r for r in _collect(codex, home) if isinstance(r, UsageEvent)]) == 1


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO needs POSIX')
def test_registered_codex_import_pipe_fails_without_waiting(tmp_path):
    home, root = tmp_path / 'home', tmp_path / 'PRIVATE-codex-profile'
    registry(home, sources=[source(root)])
    root.mkdir()
    os.mkfifo(root / 'external_agent_session_imports.json')
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path; from agent_usage_tracker.collectors.codex import imported_until; '
        'from agent_usage_tracker.event import CollectorUnavailable; '
        '\ntry: imported_until(Path(__import__("sys").argv[1]))'
        '\nexcept CollectorUnavailable as exc:'
        '\n assert "PRIVATE" not in str(exc); raise SystemExit(0)'
        '\nraise SystemExit(1)', str(home)], capture_output=True, timeout=2,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')})
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize('content', [
    '{"records": []}' + ' ' * (1024 * 1024), '[' * 10000 + ']' * 10000])
def test_registered_codex_import_metadata_is_bounded(tmp_path, content):
    home, root = tmp_path / 'home', tmp_path / 'PRIVATE-codex-profile'
    registry(home, sources=[source(root)])
    assert codex.imported_until(home) == {}
    root.mkdir()
    (root / 'external_agent_session_imports.json').write_text(content)
    with pytest.raises(CollectorUnavailable, match='import index') as error:
        codex.imported_until(home)
    assert 'PRIVATE' not in str(error.value)


def test_each_codex_profile_keeps_its_import_index(tmp_path):
    home, root = tmp_path / 'home', tmp_path / 'codex-profile'
    registry(home, sources=[source(root)])
    for profile, seconds in ((home / '.codex', 1), (root, 3)):
        profile.mkdir(parents=True, exist_ok=True)
        (profile / 'external_agent_session_imports.json').write_text(json.dumps({'records': [
            {'imported_thread_id': 'shared', 'imported_at': seconds},
            {'imported_thread_id': str(seconds), 'imported_at': seconds}]}))
    assert codex.imported_until(home) == {
        'shared': datetime(1970, 1, 1, 0, 0, 4, tzinfo=timezone.utc),
        '1': datetime(1970, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
        '3': datetime(1970, 1, 1, 0, 0, 4, tzinfo=timezone.utc)}
    (root / 'external_agent_session_imports.json').write_text('{')
    with pytest.raises(CollectorUnavailable, match='import index'):
        codex.imported_until(home)


def test_registered_codex_names_and_private_inspection_share_discovery(tmp_path):
    from agent_usage_tracker.improve.store import Store, audit

    home, root = tmp_path / 'home', tmp_path / 'codex-profile'
    registry(home, sources=[source(root)])
    log = root / 'sessions/rollout-native.jsonl'
    _write_jsonl(log, [_meta('native'), _tok(T(2)),
        {'type': 'event_msg', 'timestamp': T(3),
         'payload': {'type': 'user_message', 'message': 'Fix the parser'}}])
    os.utime(log, (1_000_000_000, 1_000_000_000))
    _write_jsonl(root / 'session_index.jsonl', [{'id': 'native', 'thread_name': 'A title'}])
    assert codex.thread_names(home) == {'native': 'A title'}
    result = audit(Store(tmp_path / 'private'), home, idle_s=0, since_epoch=0)
    assert result['sessions'] == 1


@pytest.mark.parametrize('engine,mod,label', [
    ('claude_code', claude_code, 'sdk-py'), ('codex', codex, 'T3 Code')])
def test_client_metadata_observation_does_not_promote_automated_origin(tmp_path, engine, mod, label):
    from agent_usage_tracker.diagnostics import capture

    if engine == 'claude_code':
        path = tmp_path / '.claude/projects/p/native.jsonl'
        records = [{'type': 'assistant', 'uuid': 'a', 'sessionId': 'native',
                    'timestamp': T(2), 'entrypoint': label, 'message': {
                        'id': 'm', 'usage': {'input_tokens': 2}}}]
    else:
        path = tmp_path / '.codex/sessions/rollout-native.jsonl'
        records = [_meta('native', originator=label, source='exec'), _tok(T(2))]
    _write_jsonl(path, records)
    with capture() as observed:
        rows = [r for r in _collect(mod, tmp_path) if isinstance(r, UsageEvent)]
    assert observed.snapshot()['clients'] == [label.lower()]
    assert rows[0].session.run_origin == 'automated'


def test_alias_to_default_keeps_existing_watermark_path(tmp_path):
    from agent_usage_tracker.sources import engine_homes

    root = tmp_path / '.codex'
    root.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    registry(tmp_path, sources=[source(alias)])
    assert engine_homes(tmp_path, 'codex') == [root]


def test_claude_profiles_sharing_projects_keep_default_watermark_path(tmp_path):
    root = tmp_path / '.claude/projects'
    root.mkdir(parents=True)
    profile = tmp_path / 'claude-profile'
    profile.mkdir()
    (profile / 'projects').symlink_to(root, target_is_directory=True)
    registry(tmp_path, sources=[source(profile, 'claude_code')])
    assert claude_code.projects_dirs(tmp_path) == [root]


def test_same_harness_declares_each_transport_without_parsing_api_or_pty(tmp_path):
    from agent_usage_tracker.sources import engine_homes, load_registry

    native, cli, api, pty = [tmp_path / name for name in ('native', 'cli', 'api', 'pty')]
    registry(tmp_path, sources=[source(native, id='native'),
        {**source(cli, id='cli'), 'transport': 'structured_cli'},
        {**source(api, id='api'), 'transport': 'app_server'},
        {**source(pty, id='pty'), 'transport': 'pty'},
        {'id': 'remote', 'engine': 'codex', 'harness': 'unknown-wrapper', 'location': 'remote'}])
    result = load_registry(tmp_path)
    assert [s.transport for s in result.sources] == [
        'native_files', 'structured_cli', 'app_server', 'pty', 'unknown']
    assert engine_homes(tmp_path, 'codex') == [tmp_path / '.codex', native, cli]
