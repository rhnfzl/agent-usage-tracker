import os
import sys
from pathlib import Path

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


@pytest.mark.parametrize("fixture_kind", ["copy", "hardlink", "unbound"])
def test_study_run_checks_frozen_candidate_and_uses_its_budget(tmp_path, fixture_kind):
    from agent_usage_tracker.improve.proposals import file_digest
    from agent_usage_tracker.improve.studies import run

    store, study = fixture(tmp_path)
    spec = store.get("trial_specs", study["trial_spec_id"])
    paths = {k: Path(v) / "setup" for k, v in spec["directories"].items()}
    for condition, path in paths.items():
        path.write_text(condition)
    scope = tmp_path / "source"
    scope.mkdir()
    source = scope / "setup"
    source.write_text("baseline")
    if fixture_kind == "hardlink":
        paths["baseline"].unlink()
        os.link(source, paths["baseline"])
    proposal = store.get("proposals", study["proposal_id"])
    proposal.update(scope=str(scope), before_sha256=file_digest(paths["baseline"]),
                    after_sha256=file_digest(paths["candidate"]))
    study["proposal_id"] = store.put("proposals", proposal)
    acceptance = tmp_path / "check.py"
    acceptance.write_text("import os\nfrom pathlib import Path\n"
                          "assert 'PYTHONPATH' not in os.environ\n"
                          "assert Path('setup').read_text() == 'candidate'\n")
    spec["files"] = {k: {"setup": file_digest(path)} for k, path in paths.items()}
    spec["cases"][0].update(argv=[sys.executable, str(acceptance)],
                            acceptance_files={str(acceptance): file_digest(acceptance)})
    if fixture_kind == "unbound":
        spec["cases"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(0)"]
    study["trial_spec_id"] = store.put("trial_specs", spec)
    key = create(store, study)["study_id"]
    if fixture_kind != "copy":
        with pytest.raises(AutError, match="hard link|pinned acceptance entrypoint"):
            run(store, key, execute=True)
        assert not store.keys("trial_results")
        assert source.read_text() == "baseline"
        return
    assert run(store, key)["execution_required"]
    assert not store.keys("trial_results")
    assert run(store, key, execute=True)["trial"]["accepted"] == {"baseline": 0, "candidate": 1}
    assert source.read_text() == "baseline"
    paths["candidate"].write_text("changed")
    with pytest.raises(AutError, match="candidate.*hash"):
        run(store, key, execute=True)


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


def test_shortlist_counts_recurrence_by_source_and_skips_empty_error_reports(tmp_path):
    from agent_usage_tracker.improve.studies import shortlist

    store = Store(tmp_path / 'store')
    snapshots = []
    for session, reports in [
        ('first', ['', '{"exit_code":1,"output":""}', 'permission denied', 'permission denied']),
        ('second', ['', '{"exit_code":1,"output":""}', 'permission denied']),
    ]:
        events = [{'id': str(i), 'kind': 'result', 'status': 'error', 'text': report}
                  for i, report in enumerate(reports)]
        snapshots.append(store.put('sessions', {'agent': 'codex', 'session_id': session,
                                                'events': events}))
    audit = store.put('audits', {'session_ids': snapshots})
    result = shortlist(store, audit)
    assert len(result['incidents']) == 7
    assert sum(incident['signal'] is None for incident in result['incidents']) == 4
    assert list(result['signal_counts'].values()) == [3]
    assert list(result['signal_session_counts'].values()) == [2]


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


def test_study_freezes_absence_when_proposal_adds_a_skill(tmp_path):
    from agent_usage_tracker.improve.proposals import file_digest
    from agent_usage_tracker.improve.studies import run

    store, study = fixture(tmp_path)
    trial = store.get("trial_specs", study["trial_spec_id"])
    source = tmp_path / "source"
    source.mkdir()
    target = ".agents/skills/check/SKILL.md"
    candidate = Path(trial["directories"]["candidate"]) / target
    candidate.parent.mkdir(parents=True)
    candidate.write_text("Verified instructions", encoding="utf-8")
    expected = file_digest(candidate)
    proposal = store.get("proposals", study["proposal_id"])
    proposal.update(scope=str(source), target=target, before_sha256=None, after_sha256=expected)
    study["proposal_id"] = store.put("proposals", proposal)
    acceptance = tmp_path / "accept.py"
    acceptance.write_text(f"from pathlib import Path\nassert Path({target!r}).is_file()\n")
    trial["files"] = {"baseline": {target: None}, "candidate": {target: expected}}
    trial["cases"][0].update(argv=[sys.executable, str(acceptance)],
                             acceptance_files={str(acceptance): file_digest(acceptance)})
    study["trial_spec_id"] = store.put("trial_specs", trial)
    key = create(store, study)["study_id"]
    assert run(store, key, execute=True)["trial"]["accepted"] == {"baseline": 0, "candidate": 1}
    assert not (source / target).exists()
    baseline = Path(trial["directories"]["baseline"]) / target
    baseline.parent.mkdir(parents=True)
    baseline.write_text("Contaminated baseline")
    with pytest.raises(AutError, match="baseline.*hash"):
        run(store, key)


@pytest.mark.parametrize("coverage_basis", ["full_case", "tool_events", "codex_model_calls"])
def test_natural_work_observation_measures_only_eligible_pairs(
    tmp_path, monkeypatch, capsys, coverage_basis,
):
    import json
    from datetime import datetime, timezone

    from agent_usage_tracker.cli import main
    from agent_usage_tracker.improve import studies

    class PolicyClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 2, 28, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(studies, "datetime", PolicyClock)
    store, policy = fixture(tmp_path)
    project = tmp_path / "source"
    project.mkdir()
    proposal = store.get("proposals", policy["proposal_id"])
    proposal["scope"] = str(project)
    policy["proposal_id"] = store.put("proposals", proposal)

    def add(name, timestamp, errors, complete=True, model="one", native_started=None):
        events = [{"id": "p", "kind": "prompt", "text": name, "timestamp": timestamp}]
        for n in range(2):
            events.extend([
                {"id": f"c{n}", "kind": "call", "call_id": str(n),
                 "text": name, "timestamp": timestamp},
                {"id": f"r{n}", "kind": "result", "call_id": str(n),
                 "status": "error" if n < errors else "success", "timestamp": timestamp},
            ])
        context = {"status": "observed", "model": model, "cwd": str(project)}
        events.append({"id": "end", "kind": "boundary", "text": "turn_complete",
                       "timestamp": timestamp, "turn_context": context})
        for event in events:
            event["native_turn_key"] = name
        coverage = {"complete": complete, "source_incomplete": not complete,
                    "tool_events_checked": True}
        if coverage_basis == "tool_events":
            events[2]["truncated"] = True
            coverage["complete"] = False
        if coverage_basis == "codex_model_calls":
            events[1]["tool_representation"] = "model_call"
            events[3]["tool_representation"] = "execution_item"
            events[-1]["codex_model_call_receipt"] = {
                "count": 1 + errors, "started_at": native_started or timestamp,
                "thread_origin": "interactive",
            }
            coverage["complete"] = False
        agent = "codex" if coverage_basis == "codex_model_calls" else "pi"
        snapshot = store.put("sessions", {"agent": agent, "session_id": name, "model": "one",
            "cwd": str(project), "origin": "interactive", "events": events, "coverage": coverage})
        return store.put("cases", {"session_snapshot": snapshot, "events": events,
            "coverage": coverage, "omitted_task_events": 0})

    baseline = [add("old" + str(n), "2025-02-27T12:00:00+00:00", 1) for n in range(3)]
    policy["observation_policy"] = {
        "metric": "codex_model_calls" if coverage_basis == "codex_model_calls" else "tool_errors",
        "minimum_pairs": 2, "minimum_improvement": 0, "require_turn_context": True,
        "baseline_cases": {case_id: "workspace setup" for case_id in baseline},
    }
    if coverage_basis != "codex_model_calls":
        policy["observation_policy"]["coverage_basis"] = coverage_basis
    invalid = add("incomplete-baseline", "2025-02-27T12:00:00+00:00", 1, complete=False)
    too_few = policy["observation_policy"] | {"baseline_cases": {
        baseline[0]: "workspace setup", invalid: "workspace setup",
    }}
    with pytest.raises(AutError, match="eligible baseline"):
        create(store, policy | {"observation_policy": too_few})
    key = create(store, policy)["study_id"]
    adoption = store.put("adoptions", {"proposal_id": policy["proposal_id"],
        "adopted_at": "2025-03-01", "commit": "a" * 40})
    candidate = [add("new" + str(n), "2025-03-03T12:00:00+00:00", 0, complete=n < 2)
                 for n in range(3)]
    spec = tmp_path / "observations.json"
    spec.write_text(json.dumps({"adoption_id": adoption, "pairs": [
        {"baseline": old, "candidate": new, "family": "workspace setup"}
        for old, new in zip(baseline, candidate)]}))
    assert main(["improve", "--root", str(store.root), "study", "observe",
                 "--study", key, "--spec-file", str(spec), "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview.pop("preview") is True
    assert store.keys("study_observations") == []
    assert main(["improve", "--root", str(store.root), "study", "observe",
                 "--study", key, "--spec-file", str(spec)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {k: v for k, v in result.items() if k != "observation_id"} == preview
    assert result["comparison"] == "observed_decrease"
    assert result["eligible_pairs"] == 2
    assert result["recorded_turn_context_pairs"] == 2
    assert result["coverage_basis"] == coverage_basis
    assert result["tool_events_only_pairs"] == (2 if coverage_basis == "tool_events" else 0)
    assert result["excluded_pairs"] == 1
    assert result["mean_reduction"] == 1
    assert not result["causal_effect_established"]
    assert not result["task_quality_verified"]
    spec.write_text(json.dumps({"adoption_id": None, "pairs": []}))
    assert main(["improve", "--root", str(store.root), "study", "observe",
                 "--study", key, "--spec-file", str(spec)]) == 1
    capsys.readouterr()
    report = assess(store, key)
    assert report["stages"]["observations"][0]["observation_id"] == result["observation_id"]
    assert not report["improvement_established"]

    different_model = add("different-turn-model", "2025-03-03T12:00:00+00:00", 0, model="two")
    spec.write_text(json.dumps({"adoption_id": adoption, "pairs": [
        {"baseline": baseline[0], "candidate": different_model, "family": "workspace setup"},
    ]}))
    command = ["improve", "--root", str(store.root), "study", "observe",
               "--study", key, "--spec-file", str(spec)]
    assert main(command) == 0
    mismatch = json.loads(capsys.readouterr().out)
    assert mismatch["comparison"] == "inconclusive"
    assert mismatch["exclusions"] == {"project_harness_or_model_labels_differ": 1}

    if coverage_basis == "codex_model_calls":
        old_turn = add("already-open", "2025-03-02T10:00:00+00:00", 0,
                       native_started="2025-03-01T10:00:00+00:00")
        spec.write_text(json.dumps({"adoption_id": adoption, "pairs": [
            {"baseline": baseline[0], "candidate": old_turn, "family": "workspace setup"},
        ]}))
        assert main(command) == 0
        assert json.loads(capsys.readouterr().out)["exclusions"] == {
            "missing_coverage_or_wrong_window": 1,
        }

    spec.write_text(json.dumps({"adoption_id": adoption, "pairs": []}))
    assert main(command) == 0
    no_work = json.loads(capsys.readouterr().out)
    assert no_work["comparison"] == "inconclusive"
    assert no_work["eligible_pairs"] == 0
    assert set(no_work["unpaired_baseline_ids"]) == set(baseline)


def test_reviewed_ordinary_work_requires_accepted_outcomes_for_the_frozen_cohort(
    tmp_path, monkeypatch, capsys,
):
    import json
    from datetime import datetime, timezone

    from agent_usage_tracker.cli import main
    from agent_usage_tracker.improve import studies, study_reviews
    from agent_usage_tracker.improve.judge import freeze_case
    from agent_usage_tracker.improve.study_observe import observe

    class Clock(datetime):
        current = datetime(2025, 2, 28, 12, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(studies, "datetime", Clock)
    monkeypatch.setattr(study_reviews, "datetime", Clock)
    store, spec = fixture(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    proposal = store.get("proposals", spec["proposal_id"])
    spec["proposal_id"] = store.put("proposals", proposal | {"scope": str(project)})
    family = "workspace setup"
    criterion = "The requested checks finish and the report identifies each result."

    def add(name, when, error=False):
        events = [
            {"id": "prompt", "kind": "prompt", "text": "Check workspace " + name},
            {"id": "call", "kind": "call", "call_id": "cmd", "text": "verify"},
            {"id": "result", "kind": "result", "call_id": "cmd",
             "status": "error" if error else "success", "text": "First check"},
            {"id": "reply", "kind": "reply", "text": "Report for " + name},
            {"id": "end", "kind": "boundary", "text": "turn_complete"},
        ]
        for event in events:
            event["timestamp"] = when
        coverage = {"complete": True, "tool_events_checked": True, "calls": 1, "matched_results": 1}
        snapshot = store.put("sessions", {"agent": "pi", "session_id": name, "origin": "interactive",
            "model": "same", "cwd": str(project), "events": events, "coverage": coverage})
        audit = store.put("audits", {"session_ids": [snapshot]})
        return freeze_case(store, audit, snapshot, "end")

    baseline = [add(f"baseline-{n}", "2025-02-27T12:00:00Z", error=True) for n in range(3)]
    review_cmd = ["improve", "--root", str(store.root), "study", "review-case"]

    def review(case, result="accepted", same_criterion=criterion):
        assert main([*review_cmd, "--case", case, "--family", family,
                     "--criterion", same_criterion, "--result", result, "--cite", "reply"]) == 0
        return json.loads(capsys.readouterr().out)["review_id"]

    assert main([*review_cmd, "--case", baseline[0], "--family", family,
                 "--criterion", criterion, "--result", "accepted", "--cite", "outside"]) == 1
    capsys.readouterr()
    reviews = {case: review(case) for case in baseline}
    observation = {
        "metric": "tool_errors", "minimum_pairs": 2, "minimum_improvement": 0,
        "baseline_cases": dict.fromkeys(baseline, family), "baseline_reviews": reviews,
    }
    different = review(baseline[2], same_criterion="Only the first check needs to finish.")
    with pytest.raises(AutError, match="one frozen acceptance criterion"):
        create(store, spec | {"observation_policy": observation | {
            "baseline_reviews": reviews | {baseline[2]: different},
        }})
    with pytest.raises(AutError, match="already accepted"):
        create(store, spec | {"observation_policy": observation | {
            "baseline_reviews": reviews | {baseline[2]: review(baseline[2], result="rejected")},
        }})
    Clock.current = datetime(2025, 2, 28, 13, tzinfo=timezone.utc)
    study = create(store, spec | {"observation_policy": observation})["study_id"]
    adoption = store.put("adoptions", {"proposal_id": spec["proposal_id"],
        "adopted_at": "2025-03-01", "commit": "a" * 40})
    Clock.current = datetime(2025, 3, 3, 13, tzinfo=timezone.utc)
    candidate = [add(f"candidate-{n}", "2025-03-03T12:00:00Z") for n in range(3)]
    pairs = [{"baseline": old, "candidate": new, "family": family,
              "candidate_review_id": review(new)} for old, new in zip(baseline, candidate)]

    result = observe(store, study, {"adoption_id": adoption, "pairs": pairs})
    assert result["comparison"] == "observed_decrease"
    assert result["task_acceptance"] == {"basis": "operator_review", "all_cases_accepted": True,
                                         "candidate_results": {"accepted": 3}}
    assert result["pairs"][0]["baseline_review_id"] == reviews[baseline[0]]
    assert result["pairs"][0]["candidate_review_id"] == pairs[0]["candidate_review_id"]
    assert not result["causal_effect_established"]

    rejected = observe(store, study, {"adoption_id": adoption, "pairs": [*pairs[:2], pairs[2] | {
        "candidate_review_id": review(candidate[2], result="rejected"),
    }]}, preview=True)
    assert rejected["comparison"] == "inconclusive"
    assert rejected["eligible_pairs"] == 2
    assert rejected["exclusions"] == {"task_not_accepted": 1}
    assert rejected["task_acceptance"]["candidate_results"] == {"accepted": 2, "rejected": 1}
    unknown = observe(store, study, {"adoption_id": adoption, "pairs": [*pairs[:2], pairs[2] | {
        "candidate_review_id": review(candidate[2], result="unknown"),
    }]}, preview=True)
    assert unknown["comparison"] == "inconclusive"
    assert unknown["exclusions"] == {"task_acceptance_unknown": 1}

    missing = observe(store, study, {"adoption_id": adoption, "pairs": [
        *pairs[:2], {name: value for name, value in pairs[2].items() if name != "candidate_review_id"},
    ]}, preview=True)
    assert missing["comparison"] == "inconclusive"
    assert missing["exclusions"] == {"task_review_missing": 1}
    with pytest.raises(AutError, match="frozen acceptance criterion"):
        observe(store, study, {"adoption_id": adoption, "pairs": [pairs[0] | {
            "candidate_review_id": review(candidate[0], same_criterion="Only print a report."),
        }]})
    assert len(store.keys("study_observations")) == 1


@pytest.mark.parametrize("read_mode", [None, "native", "mirrored"])
def test_strict_observation_checks_adoption_and_session_setup_revisions(tmp_path, monkeypatch, read_mode):
    import hashlib
    import json
    import subprocess
    from datetime import datetime, timezone

    from agent_usage_tracker.improve import decide, studies
    from agent_usage_tracker.improve.evidence import native_item_key, read_session
    from agent_usage_tracker.improve.judge import freeze_case
    from agent_usage_tracker.improve.proposals import prepare
    from agent_usage_tracker.improve.study_observe import observe
    from agent_usage_tracker.improve.t3 import link_native_context

    class PolicyClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 2, 28, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(studies, "datetime", PolicyClock)
    store, policy = fixture(tmp_path)
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), "-c", "user.name=T",
                                        "-c", "user.email=t@x", "-c", "commit.gpgsign=false",
                                        *args], text=True).strip()

    target = source / "setup"
    git("init", "-q", "-b", "main")
    target.write_text("before")
    git("add", "setup")
    git("commit", "-qm", "baseline")
    old = git("rev-parse", "HEAD")
    proposal = prepare(store, policy["discovery_cases"][0], source, target, "before", "after",
                       extra={"title": "Update setup"})
    decide.accept(store, proposal)
    target.write_text("after")
    git("add", "setup")
    git("commit", "-qm", "candidate")
    new = git("rev-parse", "HEAD")
    policy["proposal_id"] = proposal

    def add(name, phase, revision, exit_code, returned="after", read_turn=None, worktree_content=None):
        stamp = "2025-02-27T12:00:00Z" if phase == "baseline" else "2025-03-03T12:00:00Z"
        cwd = str(source)
        rows = [
            ("session_meta", {"id": name, "source": "cli", "cwd": cwd,
                              **({"git": {"commit_hash": revision}} if read_mode != "mirrored" else {})}),
            ("event_msg", {"type": "task_started", "turn_id": name}),
            ("turn_context", {"turn_id": name, "model": "one", "cwd": cwd}),
            ("event_msg", {"type": "item_completed", "item": {"type": "UserMessage",
                          "id": "prompt", "content": "Check " + name}}),
            ("event_msg", {"type": "item_completed", "item": {"type": "CommandExecution",
                          "id": "run", "command": "check", "exit_code": exit_code}}),
            ("event_msg", {"type": "task_complete", "turn_id": name}),
        ]
        if read_mode and phase == "candidate":
            rows.insert(-1, ("event_msg", {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": "read-setup", "command": "cat setup",
                "exit_code": 0, "cwd": cwd, "aggregated_output": returned,
            }, "turn_id": read_turn or name}))
        path = tmp_path / (name + ".jsonl")
        path.write_text("".join(json.dumps({"type": kind, "payload": payload, "timestamp": stamp})
                                + "\n" for kind, payload in rows))
        def lookup_revision(session, turn):
            from agent_usage_tracker.improve.file_reads import path_fingerprint

            assert session == turn == name
            workspace_text = worktree_content or ("before" if phase == "baseline" else "after")
            return {"workspace_sha256": path_fingerprint(source), "commit": revision,
                    "recorded_at": stamp, "setup_files": {
                        path_fingerprint(source / "setup"): hashlib.sha256(workspace_text.encode()).hexdigest(),
                    }}

        row = read_session(path, "codex", track_native_items=read_mode == "mirrored",
                           turn_revision=lookup_revision if read_mode == "mirrored" else None)
        if read_mode == "mirrored":
            native = store.put("sessions", row)
            app = {
                **row, "agent": "t3", "session_id": "t3:" + name,
                "supplemental_sources": [native],
                "native_runs": {name: {"session_id": name, "items": [
                    native_item_key("prompt"), native_item_key("run"),
                ]}},
                "events": [{**event, "run_id": name} for event in row["events"]],
            }
            for event in app["events"]:
                for key in ("native_turn_key", "turn_context", "native_file_read"):
                    event.pop(key, None)
            assert link_native_context(app, {native: row}, set()) == 1
            assert app["events"][-1]["turn_context"]["turn_start_revision"]["commit"] == revision
            row = app
        snapshot = store.put("sessions", row)
        audit = store.put("audits", {"session_ids": [snapshot]})
        return freeze_case(store, audit, snapshot, row["events"][-1]["id"])

    baseline = [add("base-" + str(n), "baseline", old, 1) for n in range(2)]
    policy["observation_policy"] = {
        "metric": "tool_errors", "require_turn_context": True, "require_setup_revision": True,
        "minimum_pairs": 2, "minimum_improvement": 0,
        "baseline_cases": {case: "setup" for case in baseline},
    }
    if read_mode:
        policy["observation_policy"]["require_approved_file_read"] = True
    if read_mode == "mirrored":
        policy["observation_policy"]["require_worktree_setup"] = True
    wrong = add("baseline-with-candidate", "baseline", new, 0)
    with pytest.raises(AutError, match="verified setup revisions"):
        create(store, policy | {"observation_policy": policy["observation_policy"] |
                                {"baseline_cases": {baseline[0]: "setup", wrong: "setup"}}})
    key = create(store, policy)["study_id"]
    adopted = decide.adopt(store, proposal, commit=new, adopted_at="2025-03-01")
    wrongly_adopted = decide.adopt(store, proposal, commit=old, adopted_at="2025-03-01")
    candidates = [add("candidate-" + str(n), "candidate", new, 0) for n in range(2)]
    pairs = [{"baseline": b, "candidate": c, "family": "setup"}
             for b, c in zip(baseline, candidates)]
    observed = observe(store, key, {"adoption_id": adopted["adoption_id"], "pairs": pairs})
    assert observed["comparison"] == "observed_decrease"
    assert observed["setup_revision_verified_pairs"] == 2
    assert observed["approved_file_read_pairs"] == (2 if read_mode else 0)
    if read_mode == "mirrored":
        assert observed["worktree_setup_verified_pairs"] == 2
        for pair in observed["pairs"]:
            native_source = store.get("sessions", pair["approved_file_read"]["session_snapshot"])
            assert native_source["agent"] == "codex"
        changed = add("dirty-worktree", "candidate", new, 0, worktree_content="local changes")
        dirty = observe(store, key, {"adoption_id": adopted["adoption_id"], "pairs": [
            pairs[0], {**pairs[1], "candidate": changed},
        ]}, preview=True)
        assert dirty["exclusions"] == {"worktree_setup_unverified": 1}
        assert dirty["worktree_setup_verified_pairs"] == 1
        assert dirty["comparison"] == "inconclusive"
    assert observed["adoption_commit_setup"] == {"status": "verified", "revision": new}
    assert not observed["attempt_setup_exposure_verified"]
    excluded = observe(store, key, {"adoption_id": wrongly_adopted["adoption_id"], "pairs": pairs})
    assert excluded["exclusions"] == {"adoption_commit_unverified": 2}
    assert excluded["comparison"] == "inconclusive"
    stale = add("stale-candidate", "candidate", old, 0)
    pairs[0]["candidate"] = stale
    excluded = observe(store, key, {"adoption_id": adopted["adoption_id"], "pairs": pairs})
    assert excluded["exclusions"] == {"setup_revision_unverified": 1}
    assert excluded["comparison"] == "inconclusive"
    if read_mode:
        for label, args in (("wrong-read", {"returned": "different bytes"}),
                            ("other-turn", {"read_turn": "earlier"})):
            pairs[0]["candidate"] = add(label, "candidate", new, 0, **args)
            excluded = observe(store, key, {"adoption_id": adopted["adoption_id"], "pairs": pairs})
            assert excluded["exclusions"] == {"approved_file_read_unverified": 1}
            assert excluded["comparison"] == "inconclusive"


def test_create_can_freeze_a_trusted_trial_file_with_the_policy(tmp_path, capsys):
    import json

    from agent_usage_tracker.cli import main

    store, spec = fixture(tmp_path)
    trial_id = spec.pop("trial_spec_id")
    trial = tmp_path / "trial.json"
    trial.write_text(json.dumps(store.get("trial_specs", trial_id)))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(spec))
    assert main(["improve", "--root", str(store.root), "study", "create",
                 "--spec-file", str(policy), "--trial-spec-file", str(trial)]) == 0
    study_id = json.loads(capsys.readouterr().out)["study_id"]
    assert store.get("studies", study_id)["trial_spec_id"] == trial_id
    assert not store.keys("trial_results")
