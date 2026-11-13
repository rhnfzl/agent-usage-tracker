from agent_usage_tracker.improve.study_decision import measure
from tests.improve.test_studies import followup_fixture
from agent_usage_tracker.improve.proposals import file_digest
from agent_usage_tracker.improve.study_followup import followup


def test_scoped_code_effect_requires_positive_delta_and_quality(tmp_path):
    store, sid, spec, acceptance = followup_fixture(tmp_path)
    trial = store.get('trial_specs', spec['trial_spec_id'])
    acceptance.write_text("from pathlib import Path\nassert Path('setup').read_text() == 'after'\n")
    trial['kind'] = 'command_check'
    trial['cases'] = [{'id': 'code', 'argv': [__import__('sys').executable, str(acceptance)],
        'timeout_s': 2, 'acceptance_files': {str(acceptance): file_digest(acceptance)}}]
    spec['trial_spec_id'] = store.put('trial_specs', trial)
    spec['trial_case_evidence'] = {case['id']: spec['evidence_cases'] for case in trial['cases']}
    followup(store, sid, spec, execute=True)
    policy = {'direction': 'increase', 'minimum_improvement': 0,
        'require_no_acceptance_regressions': True, 'require_all_candidate_accepted': True}
    proposal = store.get('proposals', store.get('studies', sid)['proposal_id'])
    report = measure(store, spec['trial_spec_id'], 'accepted', policy, proposal)
    assert report['decision'] == 'supported'
    assert report['primary_metric_delta'] == 1
    assert report['pairs'] == 1
    assert measure(store, spec['trial_spec_id'], 'tool_calls', policy, proposal)['decision'] \
        == 'inconclusive'
    changed = dict(policy, direction='decrease')
    assert measure(store, spec['trial_spec_id'], 'accepted', changed, proposal)['decision'] \
        == 'rejected'


def test_zero_benefit_is_inconclusive_and_quality_regression_rejected(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest
    from agent_usage_tracker.improve.study_followup import followup
    for name, code, expected in (
            ('equal', 'pass\n', 'inconclusive'),
            ('regression', "from pathlib import Path\nassert Path('setup').read_text() == 'before'\n",
             'rejected')):
        root = tmp_path / name
        root.mkdir()
        store, sid, spec, acceptance = followup_fixture(root)
        trial = store.get('trial_specs', spec['trial_spec_id'])
        trial['kind'] = 'command_check'
        acceptance.write_text(code)
        trial['cases'] = [{'id': name, 'argv': [__import__('sys').executable, str(acceptance)],
            'timeout_s': 2, 'acceptance_files': {str(acceptance): file_digest(acceptance)}}]
        spec['trial_spec_id'] = store.put('trial_specs', trial)
        spec['trial_case_evidence'] = {case['id']: spec['evidence_cases'] for case in trial['cases']}
        followup(store, sid, spec, execute=True)
        study = store.get('studies', sid)
        report = measure(store, spec['trial_spec_id'], 'accepted', study['decision_policy'],
                         store.get('proposals', study['proposal_id']))
        assert report['decision'] == expected
        assert bool(report['acceptance_regressions']) == (name == 'regression')
        assert report['general_productivity_verified'] is False


def test_agent_metric_delta_and_missing_metrics_are_distinct(tmp_path):
    from agent_usage_tracker.improve.study_followup import followup
    for missing in (False, True):
        root = tmp_path / str(missing)
        root.mkdir()
        store, sid, spec, _ = followup_fixture(root)
        trial = store.get('trial_specs', spec['trial_spec_id'])
        trial['cases'][0]['argv'] = [__import__('sys').executable, '-c', 'pass', '{metrics_file}'] \
            if missing else [__import__('sys').executable, '-c',
                'import pathlib,sys,json; pathlib.Path(sys.argv[1]).write_text(json.dumps({'
                '"tool_calls": 5 if pathlib.Path("setup").read_text()=="before" else 1, '
                '"interventions": 0}))', '{metrics_file}']
        spec['trial_spec_id'] = store.put('trial_specs', trial)
        spec['trial_case_evidence'] = {case['id']: spec['evidence_cases'] for case in trial['cases']}
        followup(store, sid, spec, execute=True)
        study = store.get('studies', sid)
        policy = dict(study['decision_policy'], direction='decrease')
        report = measure(store, spec['trial_spec_id'], 'tool_calls', policy,
                         store.get('proposals', study['proposal_id']))
        assert report['decision'] == ('inconclusive' if missing else 'supported')
        assert report['primary_metric_delta'] == (None if missing else -4)


def test_restored_oracle_and_resumed_candidate_cannot_promote_invalid_baseline(tmp_path):
    import random
    import sys
    import pytest
    from agent_usage_tracker.config import AutError
    from agent_usage_tracker.improve.trial import run_trial
    store, sid, spec, oracle = followup_fixture(tmp_path)
    trial = store.get('trial_specs', spec['trial_spec_id'])
    original = ("from pathlib import Path\n"
        "if Path('setup').read_text() == 'before': Path(__file__).write_text('pass\\n')\n")
    oracle.write_text(original)
    trial['kind'] = 'command_check'
    trial['cases'] = [{'id': 'code', 'argv': [sys.executable, str(oracle)],
        'timeout_s': 2, 'acceptance_files': {str(oracle): file_digest(oracle)}}]
    for seed in range(100):
        order = ['baseline', 'candidate']
        random.Random(f'{seed}:0:code').shuffle(order)
        if order[0] == 'baseline':
            trial['seed'] = seed
            break
    key = store.put('trial_specs', trial)
    with pytest.raises(AutError, match='changed'):
        run_trial(store, key, execute=True)
    oracle.write_text(original)
    assert run_trial(store, key, execute=True)['finished'] == 2
    study = store.get('studies', sid)
    report = measure(store, key, 'accepted', study['decision_policy'],
                     store.get('proposals', study['proposal_id']))
    assert report['decision'] == 'inconclusive'
    assert report['invalid_observations'] == 1
    assert report['primary_metric_delta'] is None
