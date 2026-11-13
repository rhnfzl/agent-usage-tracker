import sys

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.improve.studies import assess, create


def fixture(tmp_path):
    store = Store(tmp_path / 'store')
    ids = []
    for n in range(2):
        sid = store.put('sessions', {'agent': 'codex', 'session_id': str(n), 'events': [],
                                     'origin': 'interactive'})
        ids.append(store.put('cases', {'session_snapshot': sid, 'events': [
            {'id': str(n), 'kind': 'result', 'text': f'failed {n}', 'status': 'error'}],
            'coverage': {'complete': True}, 'omitted_task_events': 0}))
    proposal = store.put('proposals', {'case_id': ids[0], 'before_sha256': 'a'*64,
                                      'after_sha256': 'b'*64, 'target': 'setup'})
    roots = [tmp_path / name for name in ('baseline', 'candidate')]
    for root in roots:
        root.mkdir()
    trial = store.put('trial_specs', {'version': 1, 'kind': 'command_check',
        'directories': dict(zip(('baseline', 'candidate'), map(str, roots))),
        'repeats': 1, 'cases': [{'id': 'fresh', 'argv': [sys.executable, '-c', 'pass'],
                                'timeout_s': 1}]})
    spec = {'version': 1, 'discovery_cases': [ids[0]], 'control_cases': [ids[1]],
        'task_families': {ids[0]: 'editing', ids[1]: 'review'}, 'proposal_id': proposal,
        'trial_spec_id': trial, 'primary_metric': 'accepted',
        'quality_constraints': ['acceptance must pass'], 'budget_s': 30,
        'decision_policy': {'direction': 'increase', 'minimum_improvement': 0,
        'require_no_acceptance_regressions': True, 'require_all_candidate_accepted': True},
        'provenance': {'setup': None, 'model': None, 'tools': None, 'instructions': None},
        'reference_result_ids': [], 'cost_inputs': {'operator_minutes': None, 'money': None}}
    return store, spec


def test_create_prospective_immutable_and_missing_observations(tmp_path):
    store, spec = fixture(tmp_path)
    key = create(store, spec)['study_id']
    report = assess(store, key)
    assert report['stages']['trial']['finished'] == 0
    assert report['improvement_established'] is False
    assert report['provenance']['instructions'] is None
    assert report['rollback']['before_sha256'] == 'a'*64
    store.put('trial_results', {'spec_id': spec['trial_spec_id']})
    with pytest.raises(AutError, match='prospective'):
        create(store, spec)


def test_copied_case_and_malformed_manifest_rejected(tmp_path):
    store, spec = fixture(tmp_path)
    copied = store.get('cases', spec['discovery_cases'][0])
    copied['session_snapshot'] = store.get('cases', spec['control_cases'][0])['session_snapshot']
    duplicate = store.put('cases', copied)
    spec['control_cases'] = [duplicate]
    spec['task_families'] = {spec['discovery_cases'][0]: 'editing', duplicate: 'review'}
    with pytest.raises(AutError):
        create(store, spec)
    with pytest.raises(AutError):
        create(store, {'version': 1})


def test_holdout_overlap_and_per_rubric_missing_rates(tmp_path):
    store, spec = fixture(tmp_path)
    from agent_usage_tracker.improve.validation import evidence_key
    evidence = evidence_key(store.get('cases', spec['discovery_cases'][0]))
    rid = store.put('reference_results', {'split': 'holdout', 'cases': [
        {'rubric': 'avoidable_recovery', 'expected': 'fail', 'observed': None,
         'evidence_sha256': evidence}]})
    spec['reference_result_ids'] = [rid]
    report = assess(store, create(store, spec)['study_id'])
    assert report['stages']['references']['holdout_independence_verified'] is False
    row = report['stages']['references']['rubrics']['avoidable_recovery']
    assert row['missing'] == 1
    assert row['miss_rate'] is None


def test_shortlist_deduplicates_events_across_snapshots(tmp_path):
    from agent_usage_tracker.improve.studies import shortlist
    store = Store(tmp_path / 'store')
    event = {'id': 'event', 'kind': 'result', 'status': 'error', 'text': 'failure'}
    snapshots = [store.put('sessions', {'agent': 'codex', 'session_id': 'same',
        'events': [event], 'version': version}) for version in (1, 2)]
    audit = store.put('audits', {'session_ids': snapshots})
    result = shortlist(store, audit)
    assert result['distinct_source_sessions'] == 1
    assert len(result['incidents']) == 1
    assert len(result['incidents'][0]['snapshots']) == 2


def followup_fixture(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest
    store, study = fixture(tmp_path)
    baseline = tmp_path / 'baseline'
    candidate = tmp_path / 'candidate'
    (baseline / 'setup').write_text('before')
    (candidate / 'setup').write_text('after')
    proposal = store.get('proposals', study['proposal_id'])
    proposal.update(before_sha256=file_digest(baseline / 'setup'),
                    after_sha256=file_digest(candidate / 'setup'))
    study['proposal_id'] = store.put('proposals', proposal)
    sid = create(store, study)['study_id']
    session = store.put('sessions', {'agent': 'codex', 'session_id': 'fresh', 'events': []})
    case = store.put('cases', {'session_snapshot': session, 'events': [
        {'id': 'fresh', 'kind': 'prompt', 'text': 'fresh task'}],
        'coverage': {'complete': True}, 'omitted_task_events': 0})
    acceptance = tmp_path / 'accept.py'
    acceptance.write_text('pass\n')
    trial = store.put('trial_specs', {'version': 1, 'kind': 'agent_task',
        'directories': {'baseline': str(baseline), 'candidate': str(candidate)},
        'files': {'baseline': {'setup': proposal['before_sha256']},
                  'candidate': {'setup': proposal['after_sha256']}},
        'repeats': 1, 'cases': [{'id': 'fresh', 'timeout_s': 2,
            'argv': [sys.executable, '-c',
                'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(' +
                '\'{"tool_calls": 1, "interventions": 0}\')', '{metrics_file}'],
            'acceptance_argv': [sys.executable, str(acceptance), '{workspace}'],
            'acceptance_files': {str(acceptance): file_digest(acceptance)}}]})
    spec = {'version': 1, 'trial_spec_id': trial, 'evidence_cases': [case],
        'task_families': {case: 'new-task'}, 'trial_case_evidence': {'fresh': [case]}, 'producer_id': 'original', 'project_scope': 'test',
        'provenance': {'setup': None, 'model': None, 'tools': None, 'instructions': None}}
    return store, sid, spec, acceptance


def test_followup_preview_executes_nothing_and_hash_change_rejected(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, acceptance = followup_fixture(tmp_path)
    assert followup(store, sid, spec)['executed'] == 0
    assert store.keys('trial_results') == []
    acceptance.write_text('print("tampered")')
    with pytest.raises(AutError, match='changed'):
        followup(store, sid, spec, execute=True)
    assert store.keys('trial_results') == []


def test_followup_keeps_results_and_transfer_minimizes_records(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup, transfer
    store, sid, spec, _ = followup_fixture(tmp_path)
    outcome = followup(store, sid, spec, execute=True)
    assert outcome['trial']['finished'] == 2
    assert outcome['trial']['accepted'] == {'baseline': 1, 'candidate': 1}
    request = {'version': 1, 'participant_id': 'participant', 'producer_id': 'independent',
        'project_scope': 'test', 'consent': {'participant_id': 'participant',
        'project_scope': 'test', 'status': 'granted'},
        'outcome_ids': [outcome['followup_id']], 'reviewed_outcome_ids': [outcome['followup_id']]}
    result = transfer(store, sid, request)
    assert result['actual_participant_pilot_verified'] is False
    assert 'spec' not in result
    assert 'output' not in str(result)
    for status in ('revoked', 'unknown'):
        request['consent']['status'] = status
        with pytest.raises(AutError, match='consent'):
            transfer(store, sid, request)
    del request['consent']
    with pytest.raises(AutError):
        transfer(store, sid, request)


def test_followup_rejects_discovery_and_missing_metrics_remain_missing(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    discovery = store.get('studies', sid)['discovery_cases'][0]
    spec['evidence_cases'] = [discovery]
    spec['task_families'] = {discovery: 'editing'}
    spec['trial_case_evidence'] = {'fresh': [discovery]}
    with pytest.raises(AutError, match='source event'):
        followup(store, sid, spec)


def test_cli_contract_and_malformed_file(tmp_path):
    import argparse
    from agent_usage_tracker.improve import study_cli
    parser = argparse.ArgumentParser()
    study_cli.register(parser.add_subparsers(dest='improve_cmd'))
    args = parser.parse_args(['study', 'followup', '--study', 's', '--spec-file', 'x'])
    assert args.study_cmd == 'followup'
    assert args.execute is False
    with pytest.raises(AutError, match='readable JSON'):
        study_cli.run(args, Store(tmp_path / 'store'))


def test_command_followup_proves_code_correctness_without_productivity_claim(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, acceptance = followup_fixture(tmp_path)
    trial = store.get('trial_specs', spec['trial_spec_id'])
    acceptance.write_text("from pathlib import Path\nassert Path('setup').read_text() == 'after'\n")
    trial['kind'] = 'command_check'
    trial['cases'] = [{'id': 'code-check', 'timeout_s': 2,
        'argv': [sys.executable, str(acceptance)],
        'acceptance_files': {str(acceptance): file_digest(acceptance)}}]
    spec['trial_spec_id'] = store.put('trial_specs', trial)
    spec['trial_case_evidence'] = {case['id']: spec['evidence_cases'] for case in trial['cases']}
    outcome = followup(store, sid, spec, execute=True)
    assert outcome['trial']['accepted'] == {'baseline': 0, 'candidate': 1}
    assert outcome['scope'] == 'command/code correctness'
    assert outcome['general_productivity_verified'] is False
    assert outcome['trial']['agent_behaviour_verified'] is False
    acceptance.write_text('pass')
    with pytest.raises(AutError, match='changed'):
        followup(store, sid, spec, execute=True)


def test_reimported_old_event_with_changed_case_id_is_not_fresh_work(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    old = store.get('cases', store.get('studies', sid)['discovery_cases'][0])
    old['coverage'] = {'complete': False}
    old['events'][0]['text'] = 'changed rendering of old event'
    copied = store.put('cases', old)
    spec['evidence_cases'] = [copied]
    spec['task_families'] = {copied: 'new-label'}
    spec['trial_case_evidence'] = {'fresh': [copied]}
    with pytest.raises(AutError, match='source event'):
        followup(store, sid, spec)


def test_missing_event_times_keep_fresh_work_unknown(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    result = followup(store, sid, spec)
    assert result['freshness']['work_after_policy_verified'] is False


def test_study_rejects_metadata_alias_of_observed_experiment(tmp_path):
    from agent_usage_tracker.improve.trial import run_trial
    store, spec = fixture(tmp_path)
    run_trial(store, spec['trial_spec_id'], execute=True)
    trial = store.get('trial_specs', spec['trial_spec_id'])
    trial['note'] = 'copied trial after observing results'
    spec['trial_spec_id'] = store.put('trial_specs', trial)
    with pytest.raises(AutError, match='prospective'):
        create(store, spec)


def test_followup_rejects_metadata_alias_of_observed_experiment(tmp_path):
    from agent_usage_tracker.improve.trial import run_trial
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    run_trial(store, spec['trial_spec_id'], execute=True)
    trial = store.get('trial_specs', spec['trial_spec_id'])
    trial['note'] = 'copied trial after observing results'
    spec['trial_spec_id'] = store.put('trial_specs', trial)
    with pytest.raises(AutError, match='prior trial results'):
        followup(store, sid, spec)


def test_followup_requires_mapping_for_every_executed_case(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    spec['trial_case_evidence'] = {}
    with pytest.raises(AutError, match='mapping'):
        followup(store, sid, spec)


def test_followup_family_overlap_is_inspectable_but_not_independent(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    spec['task_families'] = {key: 'editing' for key in spec['evidence_cases']}
    preview = followup(store, sid, spec)
    assert preview['independence']['declared_family_overlap'] == ['editing']
    assert preview['independence']['verified'] is False


def test_followup_mapping_rejects_unavailable_or_unassociated_evidence(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, spec, _ = followup_fixture(tmp_path)
    spec['trial_case_evidence'] = {'fresh': ['f'*64]}
    with pytest.raises(AutError, match='mapping'):
        followup(store, sid, spec)
    spec['trial_case_evidence'] = {'unexecuted': spec['evidence_cases']}
    with pytest.raises(AutError, match='mapping'):
        followup(store, sid, spec)


@pytest.mark.parametrize('family,expected', [('editing', 'inconclusive'), ('independent', 'supported')])
def test_overall_support_requires_mapped_fresh_disjoint_followup(tmp_path, family, expected):
    from datetime import datetime, timezone
    from agent_usage_tracker.improve.proposals import file_digest
    from agent_usage_tracker.improve.study_followup import followup
    from agent_usage_tracker.improve.trial import run_trial
    store, old_sid, spec, acceptance = followup_fixture(tmp_path)
    trial = store.get('trial_specs', spec['trial_spec_id'])
    acceptance.write_text("from pathlib import Path\nassert Path('setup').read_text() == 'after'\n")
    trial['kind'] = 'command_check'
    trial['cases'] = [{'id': 'fresh', 'argv': [sys.executable, str(acceptance)], 'timeout_s': 2,
                      'acceptance_files': {str(acceptance): file_digest(acceptance)}}]
    policy = {key: value for key, value in store.get('studies', old_sid).items()
              if key not in {'known_case_ids', 'created_at'}}
    policy['trial_spec_id'] = store.put('trial_specs', trial)
    sid = create(store, policy)['study_id']
    run_trial(store, policy['trial_spec_id'], execute=True)
    independent_oracle = tmp_path / 'independent_oracle.py'
    independent_oracle.write_text(acceptance.read_text())
    trial['cases'][0]['argv'] = [sys.executable, str(independent_oracle)]
    trial['cases'][0]['acceptance_files'] = {
        str(independent_oracle): file_digest(independent_oracle)}
    spec['trial_spec_id'] = store.put('trial_specs', trial)
    case = store.get('cases', spec['evidence_cases'][0])
    case['events'][0]['timestamp'] = datetime.now(timezone.utc).isoformat()
    key = store.put('cases', case)
    spec['evidence_cases'] = [key]
    spec['task_families'] = {key: family}
    spec['trial_case_evidence'] = {'fresh': [key]}
    followup(store, sid, spec, execute=True)
    report = assess(store, sid)
    assert report['scoped_trial_decision']['decision'] == 'supported'
    assert report['scoped_decision'] == expected
    assert report['fresh_followup_supported'] == (family == 'independent')
    assert report['improvement_established'] is False


def test_no_reference_assessment_does_not_scan_unrelated_legacy_cases(tmp_path, monkeypatch):
    store, spec = fixture(tmp_path)
    legacy_id = store.put('cases', {'version': 0, 'events': []})
    sid = create(store, spec)['study_id']
    original = store.keys

    def keys(kind):
        assert kind != 'cases', 'No reference results need no whole-case scan'
        return original(kind)

    monkeypatch.setattr(store, 'keys', keys)
    report = assess(store, sid)['stages']['references']
    assert report['rubrics'] == {}
    assert report['holdout_independence_verified'] is False
    assert store.get('cases', legacy_id) == {'version': 0, 'events': []}


def test_relevant_legacy_holdout_remains_unattributable(tmp_path):
    from agent_usage_tracker.improve.validation import evidence_key
    store, spec = fixture(tmp_path)
    legacy = {'events': [{'kind': 'prompt', 'text': 'old held-out work'}],
              'coverage': {'complete': True}, 'omitted_task_events': 0}
    legacy_id = store.put('cases', legacy)
    evidence = evidence_key(legacy)
    rid = store.put('reference_results', {'split': 'holdout', 'cases': [
        {'rubric': 'avoidable_recovery', 'expected': 'pass', 'observed': 'pass',
         'evidence_sha256': evidence}]})
    spec['reference_result_ids'] = [rid]
    report = assess(store, create(store, spec)['study_id'])['stages']['references']
    assert report['rubrics']['avoidable_recovery']['total'] == 1
    assert report['unattributable_reference_cases'] == 1
    assert report['unknown_holdout_groups'] == 1
    assert report['holdout_independence_verified'] is False
    assert store.get('cases', legacy_id) == legacy


def test_reference_compatibility_does_not_swallow_record_corruption(tmp_path):
    from agent_usage_tracker.improve.validation import evidence_key
    store, spec = fixture(tmp_path)
    legacy = {'events': [], 'coverage': {}, 'omitted_task_events': 0}
    legacy_id = store.put('cases', legacy)
    rid = store.put('reference_results', {'split': 'holdout', 'cases': [
        {'rubric': 'avoidable_recovery', 'expected': 'pass', 'observed': 'pass',
         'evidence_sha256': evidence_key(legacy)}]})
    spec['reference_result_ids'] = [rid]
    sid = create(store, spec)['study_id']
    store.path('cases', legacy_id).write_text('{}')
    with pytest.raises(AutError, match='changed'):
        assess(store, sid)


def test_reference_summary_counts_unindexable_unrelated_legacy_case(tmp_path):
    from agent_usage_tracker.improve.validation import evidence_key
    store, spec = fixture(tmp_path)
    store.put('cases', {'version': 0, 'events': []})
    evidence = evidence_key(store.get('cases', spec['discovery_cases'][0]))
    rid = store.put('reference_results', {'split': 'holdout', 'cases': [
        {'rubric': 'avoidable_recovery', 'expected': 'fail', 'observed': 'fail',
         'evidence_sha256': evidence}]})
    spec['reference_result_ids'] = [rid]
    report = assess(store, create(store, spec)['study_id'])['stages']['references']
    assert report['unindexable_cases'] == 1
    assert report['unattributable_reference_cases'] == 0
    assert report['rubrics']['avoidable_recovery']['total'] == 1
    assert report['holdout_independence_verified'] is False


def test_byte_offsets_do_not_establish_study_or_followup_independence(tmp_path):
    from agent_usage_tracker.improve.studies import identity
    from agent_usage_tracker.improve.study_followup import followup
    store, sid, follow_spec, _ = followup_fixture(tmp_path)
    study = store.get('studies', sid)
    original = store.get('cases', study['discovery_cases'][0])
    original['events'][0]['byte_offset'] = 1000
    first = store.put('cases', original)
    copied = dict(original)
    copied['session_snapshot'] = store.get('cases', follow_spec['evidence_cases'][0])['session_snapshot']
    copied['events'] = [dict(original['events'][0], byte_offset=2000)]
    copied_id = store.put('cases', copied)
    assert identity(store, first)['content'] == identity(store, copied_id)['content']
    study['control_cases'] = [copied_id]
    study['task_families'] = {study['discovery_cases'][0]: 'editing', copied_id: 'review'}
    for field in ('known_case_ids', 'created_at'):
        study.pop(field)
    with pytest.raises(AutError, match='copied'):
        create(store, study)
    follow_spec['evidence_cases'] = [copied_id]
    follow_spec['task_families'] = {copied_id: 'review'}
    follow_spec['trial_case_evidence'] = {'fresh': [copied_id]}
    with pytest.raises(AutError, match='copied|source event'):
        followup(store, sid, follow_spec)
