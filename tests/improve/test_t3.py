"""T3 app requests stay linked when the provider changes."""

import json
import sqlite3

import pytest

from agent_usage_tracker.improve.judge import cases
from agent_usage_tracker.improve.store import Store, audit


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
    _, result, sessions = capture(tmp_path, home, limit=1)
    assert len(sessions[0]['supplemental_sources']) == 1
    assert result['duplicates'] == 1
    db.close()


def test_t3_automation_does_not_become_human_work(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db, human=False)
    db.commit()
    _, result, sessions = capture(tmp_path, home)
    assert sessions == []
    assert result['t3_automated_runs'] == 1
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


def test_t3_missing_output_and_projection_loss_remain_incomplete(tmp_path):
    home = tmp_path / 'home'
    db, _ = fixture_db(home)
    add_run(db)
    db.execute("DELETE FROM orchestration_v2_projection_turn_items WHERE type='command_execution'")
    item(db, 'r1', 'incomplete', 'command_execution', 1, input='pytest', status='running')
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
