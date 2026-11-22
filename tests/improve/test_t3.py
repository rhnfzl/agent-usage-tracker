"""T3 app requests stay linked when the provider changes."""

import base64
import hashlib
import json
import os
import sqlite3
import subprocess

import pytest

from agent_usage_tracker.improve.access import Evidence
from agent_usage_tracker.improve.judge import cases, freeze_case
from agent_usage_tracker.improve.store import Store, audit
from agent_usage_tracker.improve.study_observe import _sample


TABLES = {
    'projection_projects': 'project_id TEXT, workspace_root TEXT',
    'orchestration_v2_projection_threads': 'thread_id TEXT, project_id TEXT, updated_at TEXT, payload_json TEXT',
    'orchestration_v2_projection_runs': 'run_id TEXT, thread_id TEXT, provider TEXT, provider_thread_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT, ordinal INTEGER, payload_json TEXT',
    'orchestration_v2_projection_messages': 'message_id TEXT, thread_id TEXT, run_id TEXT, role TEXT, created_at TEXT, payload_json TEXT',
    'orchestration_v2_projection_turn_items': 'turn_item_id TEXT, thread_id TEXT, run_id TEXT, ordinal INTEGER, type TEXT, status TEXT, updated_at TEXT, payload_json TEXT',
    'orchestration_v2_projection_provider_threads': 'provider_thread_id TEXT, thread_id TEXT, provider TEXT, payload_json TEXT',
}
START = '2026-01-01T10:00:00Z'
END = '2026-01-01T10:00:10Z'


def fixture_db(home):
    path = home / '.t3/userdata/statev2.sqlite'
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    for name, columns in TABLES.items():
        db.execute(f'CREATE TABLE {name} ({columns})')
    db.execute('INSERT INTO projection_projects VALUES (?,?)', ('p', '/example/project'))
    db.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?)',
               ('app', 'p', END, json.dumps({'worktreePath': None})))
    return db, path


def add_run(db, run='r1', provider='claudeAgent', ordinal=1, human=True):
    message = {'id': 'm'+run, 'text': 'Fix the checks', 'createdBy': 'user' if human else 'agent',
               'creationSource': 'web' if human else 'mcp', 'attachments': []}
    db.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)',
               ('m'+run, 'app', run, 'user', START, json.dumps(message)))
    db.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)',
               (run, 'app', provider, 'pt'+run, 'completed', START, END, ordinal,
                json.dumps({'userMessageId': 'm'+run, 'modelSelection': {'model': 'model'}})))
    db.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)',
               ('pt'+run, 'app', provider, json.dumps({'nativeThreadRef': {
                   'driver': 'codex' if provider == 'codex' else 'claudeAgent',
                   'nativeId': 'native'+run, 'strength': 'strong'}})))
    item(db, run, 'cmd'+run, 'command_execution', 1,
         input='pytest', output='Process exited with code 1', status='failed')
    item(db, run, 'reply'+run, 'assistant_message', 2, text='The checks failed.')


def item(db, run, key, kind, ordinal, **fields):
    value = {'id': key, 'runId': run, 'threadId': 'app', 'type': kind,
             'startedAt': START, 'completedAt': END, 'status': 'completed', **fields}
    db.execute('INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?,?,?)',
               (key, 'app', run, ordinal, kind, value['status'], END, json.dumps(value)))


def capture(tmp_path, home, **kwargs):
    store = Store(tmp_path / 'private')
    result = audit(store, home, idle_s=0, since_epoch=0, **kwargs)
    manifest = store.get('audits', result['audit_id'])
    return store, result, [store.get('sessions', key) for key in manifest['session_ids']]


def test_t3_human_work_and_provider_switch_are_one_session(tmp_path):
    home = tmp_path / 'home'
    db, path = fixture_db(home)
    add_run(db)
    add_run(db, 'r2', 'codex', 2)
    db.commit()
    before = path.read_bytes()
    store, result, sessions = capture(tmp_path, home)
    assert result['sessions'] == 1
    session = sessions[0]
    assert session['agent'] == 't3'
    assert session['session_id'] == 't3:app'
    assert session['origin'] == 'interactive'
    assert session['cwd'] == '/example/project'
    assert session['providers'] == ['claudeAgent', 'codex']
    assert len([e for e in session['events'] if e['kind'] == 'prompt']) == 2
    assert session['coverage']['calls'] == session['coverage']['matched_results'] == 2
    assert session['coverage']['complete']
    assert any(e['status'] == 'error' for e in session['events'])
    assert session['instruction_exposure']['status'] == 'unknown'
    assert cases(store, result['audit_id'])
    assert path.read_bytes() == before
    db.close()


@pytest.mark.parametrize('use_linked_worktree', [False, True])
def test_t3_project_filter_precedes_thread_limit(tmp_path, use_linked_worktree):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    project = tmp_path / 'project'
    worktree = tmp_path / 'linked'
    outside = tmp_path / 'outside'
    for folder in (project, outside):
        subprocess.run(['git', 'init', '-q', str(folder)], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.test', 'commit', '--allow-empty', '-qm', 'start'],
                   check=True)
    subprocess.run(['git', '-C', str(project), 'worktree', 'add', '--detach', '-q',
                    str(worktree)], check=True)

    with db:
        add_run(db)
        db.execute('UPDATE projection_projects SET workspace_root=?', (str(project),))
        thread_path = str(worktree) if use_linked_worktree else None
        db.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?',
                   (json.dumps({'worktreePath': thread_path}),))
        db.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?)',
                   ('other', 'p', '2026-01-02T10:00:00Z',
                    json.dumps({'worktreePath': str(outside)})))
        db.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)',
                   ('outside-prompt', 'other', 'outside-run', 'user', START,
                    json.dumps({'createdBy': 'user'})))
    db.close()

    _, result, sessions = capture(tmp_path, home, project=project, limit=1)

    assert [session['session_id'] for session in sessions] == ['t3:app']
    assert sessions[0]['cwd'] == (thread_path or str(project))
    assert result['other_projects'] == 1
    assert 't3_unread_threads' not in result


@pytest.mark.parametrize('limit', [1, 30])
def test_t3_native_mirror_is_not_another_observation(tmp_path, limit):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db, provider='codex')
    db.commit()
    folder = home / '.codex/sessions'
    folder.mkdir(parents=True)
    raw = [{'type': 'session_meta', 'payload': {'id': 'nativer1', 'source': 'vscode',
                                             'originator': 'T3 Code'}},
           {'type': 'event_msg', 'timestamp': '2020-01-01T00:00:00Z',
            'payload': {'type': 'user_message', 'message': 'native-only check'}}]
    (folder / 'rollout-native.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in raw))
    os.utime(folder / 'rollout-native.jsonl', (1_000_000_000, 1_000_000_000))
    store, result, sessions = capture(tmp_path, home, limit=limit)
    assert result['sessions'] == 1
    assert result['t3_native_mirrors'] == 1
    assert sessions[0]['session_id'] == 't3:app'
    assert len(sessions[0]['supplemental_sources']) == 1
    supplement = store.get('sessions', sessions[0]['supplemental_sources'][0])
    assert supplement['session_id'] == 'nativer1'
    assert any(e['text'] == 'native-only check' for e in supplement['events'])
    db.close()


def test_t3_identical_linked_native_files_share_one_supplement(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db, provider='codex')
    db.commit()
    folder = home / '.codex/sessions'
    folder.mkdir(parents=True)
    raw = json.dumps({'type': 'session_meta', 'payload': {'id': 'nativer1', 'source': 'cli'}})+'\n'
    for name in ['rollout-one.jsonl', 'rollout-two.jsonl']:
        (folder / name).write_text(raw)
        # Keep fixtures idle when filesystem timestamps lead the wall clock.
        os.utime(folder / name, (1_000_000_000, 1_000_000_000))
    _, result, sessions = capture(tmp_path, home, limit=1)
    assert len(sessions[0]['supplemental_sources']) == 1
    assert result['duplicates'] == 1
    db.close()


@pytest.mark.parametrize('mode', [
    'linked', 'one_item', 'split_turns', 'two_copies', 'reused_turn', 'wrong_prompt', 'late_native', 'steered',
    'native_steered',
])
def test_t3_turn_context_requires_distinct_native_items_in_one_completed_turn(tmp_path, mode):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db, provider='codex')
    ref = {'driver': 'codex', 'strength': 'strong'}
    item(db, 'r1', 'one', 'assistant_message', 3, text='first',
         nativeItemRef={**ref, 'nativeId': 'first'})
    item(db, 'r1', 'two', 'assistant_message', 4, text='second',
         nativeItemRef={**ref, 'nativeId': 'second',
                        'strength': 'weak' if mode == 'one_item' else 'strong'})
    if mode == 'steered':
        db.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)',
            ('steer', 'app', 'r1', 'user', '2026-01-01T10:00:01Z',
             json.dumps({'text': 'Change the task', 'createdBy': 'user', 'creationSource': 'web'})))
    if mode == 'reused_turn':
        add_run(db, 'r2', 'codex', 2)
        db.execute("""UPDATE orchestration_v2_projection_provider_threads SET payload_json=?
                      WHERE provider_thread_id='ptr2'""",
                   (json.dumps({'nativeThreadRef': {**ref, 'nativeId': 'nativer1'}}),))
        for name in ('first', 'second'):
            item(db, 'r2', name, 'assistant_message', 3, text=name,
                 nativeItemRef={**ref, 'nativeId': name})
    db.commit()
    folder = home / '.codex/sessions'
    folder.mkdir(parents=True)
    rows = [('session_meta', {'id': 'nativer1', 'source': 'cli'})]

    def native_turn(turn_id, names):
        rows.extend([
            ('event_msg', {'type': 'task_started', 'turn_id': turn_id}),
            ('turn_context', {'turn_id': turn_id, 'model': 'actual-model', 'cwd': str(tmp_path)}),
            ('event_msg', {'type': 'user_message',
                           'message': 'Another request' if mode == 'wrong_prompt' else 'Fix the checks'}),
            ('response_item', {'type': 'function_call', 'call_id': 'tool-'+turn_id, 'name': 'exec_command'}),
            ('response_item', {'type': 'function_call_output', 'call_id': 'tool-'+turn_id, 'output': 'result'}),
        ])
        for name in names:
            rows.append(('event_msg', {'type': 'item_completed',
                         'item': {'type': 'AgentMessage', 'id': name, 'text': name}}))
        if mode == 'native_steered':
            rows.append(('event_msg', {'type': 'user_message', 'message': 'Change the task'}))
        rows.append(('event_msg', {'type': 'task_complete', 'turn_id': turn_id}))

    if mode == 'split_turns':
        native_turn('native-turn', ['first'])
        native_turn('other-turn', ['second'])
    else:
        native_turn('native-turn', ['first', 'second'])
    stamp = '2026-01-01T10:00:11Z' if mode == 'late_native' else END
    raw = ''.join(json.dumps({'type': kind, 'timestamp': stamp, 'payload': payload}) + '\n'
                  for kind, payload in rows)
    (folder / 'rollout-native.jsonl').write_text(raw)
    os.utime(folder / 'rollout-native.jsonl', (1_000_000_000, 1_000_000_000))
    if mode == 'two_copies':
        (folder / 'rollout-other.jsonl').write_text(raw.replace('actual-model', 'different-model'))
        os.utime(folder / 'rollout-other.jsonl', (1_000_000_000, 1_000_000_000))
    store, result, [session] = capture(tmp_path, home)
    audit_id = result['audit_id']
    key, = store.get('audits', audit_id)['session_ids']
    boundary = next(event for event in session['events'] if event['kind'] == 'boundary')
    sample = _sample(store, freeze_case(store, audit_id, key, boundary['id']), use_native_model_calls=True)
    if mode in {'linked', 'wrong_prompt', 'late_native', 'steered', 'native_steered'}:
        assert sample['context_status'] == 'observed'
        assert sample['model'] == 'actual-model'
        assert sample['cwd'] == str(tmp_path)
        assert sample['tool_events_complete'] == (mode != 'steered')
        view = Evidence('metadata', store=store, audit_id=audit_id)
        context = view.event(key, boundary['id'])['turn_context']
        assert context['source_session'] == session['supplemental_sources'][0]
        assert context['matching_items'] == 2
        assert 'model' not in context and 'cwd' not in context
        results = view.ordinary_work(metric='codex_model_calls', require_turn_context=True)
        if mode == 'linked':
            assert sample['codex_model_calls'] == 1
            receipt = results['items'][0]['metric_evidence']
            assert receipt == {'session_id': context['source_session'], 'event_id': context['source_event']}
            assert results['items'][0]['metric_value'] == 1
            assert view.event(receipt['session_id'], receipt['event_id'])['codex_model_call_receipt']['count'] == 1
            restricted = store.put('audits', {'session_ids': [key], 'coverage': {}})
            without_source = Evidence('metadata', store=store, audit_id=restricted)
            assert without_source.ordinary_work(metric='codex_model_calls', require_turn_context=True)['items'] == []
        else:
            assert sample['codex_model_calls'] is None
            assert results['items'] == []
        view = Evidence('redacted', store=store, audit_id=audit_id)
        for name, snapshots in [('single', [key]), ('with-source', [key, context['source_session']])]:
            bundle = tmp_path / (name + '.json')
            view.export(bundle, snapshots)
            copied = Evidence('redacted', bundle=bundle).event(key, boundary['id'])['turn_context']
            if name == 'single':
                assert copied == {'status': 'missing'}
            else:
                assert copied['source_event'] == context['source_event']
                assert copied['model'] == 'actual-model'
    else:
        assert sample['context_status'] != 'observed'
        assert sample['model'] is None
    db.close()


def test_t3_automation_does_not_become_human_work(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db, human=False)
    db.commit()
    _, result, sessions = capture(tmp_path, home)
    assert sessions == []
    assert result['t3_automated_runs'] == 1


@pytest.mark.parametrize('mode', [
    'valid', 'pre_run', 'wrong_scope', 'wrong_ordinal', 'missing_ref', 'wrong_run', 'wrong_ref',
])
def test_t3_freezes_only_its_ready_end_of_run_git_checkpoint(tmp_path, mode):
    home, repo = tmp_path / 'home', tmp_path / 'project'
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Test',
                                       '-c', 'user.email=test@example.test', '-c', 'commit.gpgsign=false',
                                       '-c', 'core.hooksPath=/dev/null', *args], text=True).strip()

    git('init', '-q')
    git('commit', '-qm', 'checkpoint contents', '--allow-empty')
    commit = git('rev-parse', 'HEAD')
    scope_hash = hashlib.sha256(b'other' if mode == 'wrong_ref' else b'scope').hexdigest()[:32]
    key = base64.urlsafe_b64encode(scope_hash.encode()).decode().rstrip('=')
    ref = f'refs/t3/orchestration-v2/checkpoints/{key}/ordinal/1'
    if mode != 'missing_ref':
        git('update-ref', ref, commit)
    db, _ = fixture_db(home)
    db.execute('UPDATE projection_projects SET workspace_root=?', (str(repo),))
    add_run(db)
    payload = db.execute('SELECT payload_json FROM orchestration_v2_projection_runs').fetchone()[0]
    db.execute('UPDATE orchestration_v2_projection_runs SET payload_json=?',
               (json.dumps({**json.loads(payload), 'checkpointId': 'checkpoint', 'ordinal': 1}),))
    db.execute('''CREATE TABLE orchestration_v2_projection_checkpoint_scopes
                  (scope_id TEXT, thread_id TEXT, kind TEXT, payload_json TEXT)''')
    db.execute('''CREATE TABLE orchestration_v2_projection_checkpoints
                  (checkpoint_id TEXT, thread_id TEXT, run_id TEXT, scope_id TEXT,
                   app_run_ordinal INTEGER, ordinal_within_scope INTEGER,
                   status TEXT, captured_at TEXT, payload_json TEXT)''')
    db.execute('INSERT INTO orchestration_v2_projection_checkpoint_scopes VALUES (?,?,?,?)',
               ('scope', 'app', 'root_run',
                json.dumps({'cwd': str(tmp_path if mode == 'wrong_scope' else repo)})))
    db.execute('INSERT INTO orchestration_v2_projection_checkpoints VALUES (?,?,?,?,?,?,?,?,?)',
               ('checkpoint', 'app', 'other' if mode == 'wrong_run' else 'r1', 'scope', 1,
                2 if mode == 'wrong_ordinal' else 1, 'ready', START if mode == 'pre_run' else END,
                json.dumps({'ref': ref})))
    db.commit()
    db.close()

    store, capture_result, [session] = capture(tmp_path, home, project=repo)
    audit_id = capture_result['audit_id']
    snapshot, = store.get('audits', audit_id)['session_ids']
    boundary = next(event for event in session['events'] if event['kind'] == 'boundary')
    if mode != 'valid':
        assert 'workspace_checkpoint' not in boundary
        assert not capture_result.get('t3_workspace_checkpoints')
        return
    assert capture_result['t3_workspace_checkpoints'] == 1
    view = Evidence('redacted', store=store, audit_id=audit_id)
    checkpoint = view.event(snapshot, boundary['id'])['workspace_checkpoint']
    assert checkpoint == {'commit': commit, 'captured_at': '2026-01-01T10:00:10+00:00', 'ref': ref}
    metadata = Evidence('metadata', store=store, audit_id=audit_id)
    assert metadata.describe()['coverage']['t3_workspace_checkpoints'] == 1
    assert metadata.event(snapshot, boundary['id'])['workspace_checkpoint'] == {
        'captured_at': checkpoint['captured_at'],
    }
    bundle = tmp_path / 'selected.json'
    metadata.export(bundle, [snapshot])
    assert commit not in bundle.read_text()
    assert Evidence('full', bundle=bundle).event(snapshot, boundary['id'])['workspace_checkpoint'] == {
        'captured_at': checkpoint['captured_at'],
    }
    git('commit', '-qm', 'another commit', '--allow-empty')
    git('update-ref', ref, git('rev-parse', 'HEAD'))
    assert view.event(snapshot, boundary['id'])['workspace_checkpoint'] == checkpoint
    assert _sample(store, freeze_case(store, audit_id, snapshot, boundary['id']))['setup_revision'] is None
    db.close()


def test_t3_reads_committed_wal_rows(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    db.commit()
    db.execute('PRAGMA journal_mode=WAL')
    add_run(db)
    db.commit()
    assert capture(tmp_path, home)[1]['sessions'] == 1
    db.close()


@pytest.mark.parametrize('output', [{}, {'output': 'partial output'}])
def test_t3_missing_output_and_projection_loss_remain_incomplete(tmp_path, output):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    db.execute("DELETE FROM orchestration_v2_projection_turn_items WHERE type='command_execution'")
    item(db, 'r1', 'incomplete', 'command_execution', 1,
         input='pytest', status='running', **output)
    db.commit()
    store, result, sessions = capture(tmp_path, home)
    assert sessions[0]['coverage']['unmatched_calls'] == 1
    assert not sessions[0]['coverage']['complete']
    assert not any(row['coverage']['complete'] for row in cases(store, result['audit_id']))
    db.close()


@pytest.mark.parametrize('kind,fields,expected', [
    ('command_execution', {'status': 'completed', 'exitCode': 0}, 'success'),
    ('command_execution', {'status': 'failed', 'exitCode': 2}, 'error'),
    ('command_execution', {'status': 'failed', 'exitCode': 0}, 'error'),
    ('dynamic_tool', {'status': 'completed'}, 'success'),
    ('command_execution', {'status': 'interrupted'}, 'unknown'),
    ('command_execution', {'status': 'cancelled', 'output': 'partial output'}, 'unknown'),
])
def test_t3_finished_item_without_output_still_has_a_result(tmp_path, kind, fields, expected):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    db.execute("DELETE FROM orchestration_v2_projection_turn_items WHERE type='command_execution'")
    item(db, 'r1', 'quiet', kind, 1, input='true', **fields)
    db.commit()
    _, _, sessions = capture(tmp_path, home)
    assert sessions[0]['coverage']['unmatched_calls'] == 0
    assert [e['status'] for e in sessions[0]['events'] if e['kind'] == 'result'] == [expected]
    db.close()


def test_t3_byte_cap_is_honest_and_bounded(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    item(db, 'r1', 'large', 'command_execution', 3, input='cat file', output='x'*20000)
    db.commit()
    _, result, sessions = capture(tmp_path, home, max_bytes=3000)
    assert sessions
    assert sessions[0]['coverage']['byte_cap_reached']
    assert not sessions[0]['coverage']['complete']
    db.close()


def test_t3_bad_schema_is_reported_not_absent(tmp_path):
    home = tmp_path / 'home'
    path = home / '.t3/userdata/statev2.sqlite'
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()
    _, result, sessions = capture(tmp_path, home)
    assert sessions == []
    assert result['t3_unreadable_sources'] == 1


def test_t3_oversized_thread_metadata_is_reported_without_loading_it(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    db.execute('UPDATE orchestration_v2_projection_threads SET payload_json=?',
               (json.dumps({'title': 'x'*10000}),))
    db.commit()
    _, result, sessions = capture(tmp_path, home, max_bytes=3000)
    assert not sessions
    assert result['t3_byte_capped_threads'] == 1
    db.close()


def test_t3_one_incomplete_run_does_not_disqualify_a_complete_run(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    add_run(db, 'r2', 'codex', 2)
    item(db, 'r1', 'secret', 'secret_request', 3)
    db.commit()
    store, result, _ = capture(tmp_path, home)
    rows = cases(store, result['audit_id'])
    assert any(row['coverage']['complete'] for row in rows)
    assert any(not row['coverage']['complete'] for row in rows)
    db.close()


def test_t3_old_history_cannot_spend_the_recent_evidence_budget(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    add_run(db, 'r2', 'codex', 2)
    db.execute("UPDATE orchestration_v2_projection_runs SET requested_at='2020-01-01T00:00:00Z' WHERE run_id='r1'")
    db.execute("UPDATE orchestration_v2_projection_messages SET created_at='2020-01-01T00:00:00Z',payload_json=? WHERE run_id='r1'",
               (json.dumps({'text': 'x'*4000, 'createdBy': 'user', 'creationSource': 'web'}),))
    db.commit()
    store = Store(tmp_path / 'private')
    result = audit(store, home, idle_s=0, since_epoch=1700000000, max_bytes=3000)
    assert result['sessions'] == 1
    assert any(row['coverage']['complete'] for row in cases(store, result['audit_id']))
    db.close()


def test_t3_bad_timestamp_is_local_to_its_run(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    add_run(db, 'r2', 'codex', 2)
    db.execute("UPDATE orchestration_v2_projection_turn_items SET payload_json=? WHERE turn_item_id='cmdr1'",
               (json.dumps({'type': 'command_execution', 'input': 'pytest', 'output': 'bad',
                            'startedAt': 'bad', 'completedAt': 'bad'}),))
    db.commit()
    store, result, _ = capture(tmp_path, home)
    assert any(row['coverage']['complete'] for row in cases(store, result['audit_id']))
    db.close()


@pytest.mark.parametrize('bad_ref', [['bad'], 'bad'])
def test_t3_bad_native_reference_does_not_crash(tmp_path, bad_ref):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    db.execute('UPDATE orchestration_v2_projection_provider_threads SET payload_json=?',
               (json.dumps({'nativeThreadRef': bad_ref}),))
    db.commit()
    _, result, sessions = capture(tmp_path, home)
    assert result['sessions'] == 1
    assert sessions[0]['native_sessions'] == []
    db.close()


@pytest.mark.parametrize('kind', ['compaction', 'handoff', 'secret_request'])
def test_t3_context_gaps_do_not_pass_judge_completeness(tmp_path, kind):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    item(db, 'r1', 'gap', kind, 3)
    db.commit()
    store, result, sessions = capture(tmp_path, home)
    assert len(sessions) == 1
    rows = cases(store, result['audit_id'])
    assert rows
    assert not any(row['coverage']['complete'] for row in rows)
    db.close()
