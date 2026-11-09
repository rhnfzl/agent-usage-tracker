"""Writing remains an optional attribute with independent policy identity and bounded inputs."""

import json
import time
from collections import Counter
from datetime import timedelta

import pytest

from agent_usage_tracker.config import AutError, Config, LABEL_DECISION_TASKS
from agent_usage_tracker.label import core, local, replay, run, tasks
from label_helpers import DAY1, NOW, FakeSource, _cfg, _claude_log, _env, _steer
from test_label import sink  # noqa: F401
from test_label_local import recipe


def writing_row():
    row = recipe('single')
    row['prompt_ver'] = f'writing_activity@1/p{core.PROMPT_VER}'
    row['recipe']['members'] = row['recipe']['members'][:1]
    row['recipe']['id'] = 'synthetic-writing'
    row['binary_decision_threshold'] = {'label': 'writing', 'value': .2}
    return row


def write_policy(path, rows):
    path.write_text(json.dumps({'version': local.POLICY, 'tasks': rows}))


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('AUT_CONFIG', str(tmp_path / 'config.toml'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setattr(local, 'memory_guard', lambda: None)
    base = tmp_path / 'label-local.json'
    writing = tmp_path / 'label-local-writing.json'
    write_policy(base, {'steer_intent': recipe()})
    old_policy, old_id = local.load_policy()
    write_policy(writing, {'writing_activity': writing_row()})
    loads = []

    class Member:
        def __init__(self, task, choice):
            self.task, self.model_rev = task, choice.model_rev
            self.model_id = choice.model_id
            loads.append(task)

        def distribution(self, request):
            if self.task == 'writing_activity':
                return {'writing': .3, 'not_writing': .7}
            return {c['id']: float(c['id'] == 'instruction') for c in request.candidates}

        def close(self):
            pass

    monkeypatch.setattr(local, 'make_backend', lambda task, choice, *a: Member(task, choice))
    return base, writing, old_policy, old_id, loads


def source(tmp_path, **kw):
    cfg = _cfg(tmp_path, **kw)
    _claude_log(tmp_path, 'session', [('a', 'Fix the exporter and update the README instructions.')])
    episode = {'id': 'episode', 'first_ts': DAY1, 'last_ts': DAY1 + timedelta(hours=1),
               'members': [{'session_id': 'session'}]}
    return cfg, FakeSource(cfg, [_steer('a', 'session', DAY1)], [episode], local_context=True)


def test_writing_is_registered_without_enabling_it_by_default():
    assert 'writing_activity' in LABEL_DECISION_TASKS
    task = tasks.TASKS['writing_activity']
    assert task.ver == 1 and task.decision == 'boolean' and task.window == 'day'
    assert {c for c, desc in task.candidates} == {'writing', 'not_writing'}
    assert task.none_fits is None
    assert 'writing_activity' not in Config.__dataclass_fields__['label_tasks'].default
    assert tasks.TASKS['work_category'].ver == 2


def test_registered_writing_task_supports_review_and_evaluation(capsys):
    from agent_usage_tracker.label import bakeoff, evalset, gold
    assert set(evalset.FLOORS) == set(tasks.TASKS)
    page = gold.render('writing_activity', 'synthetic-token', None, {}, None, 0, Counter())
    assert 'writing' in page and 'not_writing' in page
    bakeoff._warn_thin('writing_activity', [], 'synthetic split')
    assert 'too small' in capsys.readouterr().err


def test_separate_recipe_never_rekeys_existing_tasks(configured):
    base, writing, old_policy, old_id, loads = configured
    assert local.load_policy() == (old_policy, old_id)
    policy, writing_id = local.load_policy(writing)
    assert set(policy['tasks']) == {'writing_activity'} and writing_id != old_id
    write_policy(base, {**old_policy['tasks'], 'writing_activity': writing_row()})
    with pytest.raises(AutError):
        local.load_policy()
    assert loads == []


@pytest.mark.parametrize('setting', [None, {'label': 'other', 'value': .2},
    {'label': 'writing', 'value': 0}, {'label': 'writing', 'value': 1},
    {'label': 'writing', 'value': True}, {'label': 'writing', 'value': 10**400},
    {'label': [], 'value': .2}])
def test_invalid_binary_boundary_is_rejected_before_loading(configured, setting):
    base, writing, old_policy, old_id, loads = configured
    row = writing_row()
    row['binary_decision_threshold'] = setting
    write_policy(writing, {'writing_activity': row})
    with pytest.raises(AutError):
        local.load_policy(writing)
    assert not loads


def test_binary_boundary_cannot_change_an_existing_multiclass_task(configured):
    base, writing, old_policy, old_id, loads = configured
    row = recipe()
    row['binary_decision_threshold'] = {'label': 'instruction', 'value': .2}
    write_policy(base, {'steer_intent': row})
    with pytest.raises(AutError):
        local.load_policy()
    assert not loads


def test_boundary_changes_the_decision_and_retains_estimate_provenance(tmp_path, configured):
    base, writing, old_policy, old_id, loads = configured
    policy, writing_id = local.load_policy(writing)
    cfg, src = source(tmp_path)
    request = tasks.units('writing_activity', src, DAY1 - timedelta(hours=2), NOW)[0]
    backend = local.LocalBackend('writing_activity', policy['tasks']['writing_activity'], writing_id, cfg)
    backend.prepare_requests([request], time.monotonic() + 30)
    response = core.decide(request, backend.distribution(request), core.Choice(),
                           backend.model_id, backend.model_rev)
    event = local.decorate(core.label_event(request, response), request, backend, response.prob)
    assert event.attrs['task'] == 'working.local.writing_activity'
    assert event.attrs['label'] == 'writing' and not event.attrs['below_tau']
    assert event.attrs['provisional'] and not event.attrs['human_verified']
    assert not event.attrs['calibrated']
    assert loads == ['writing_activity']


def test_writing_uses_only_episode_text_and_does_not_read_windows(tmp_path):
    cfg, src = source(tmp_path)
    src.windows = lambda *a: pytest.fail('Writing does not require ActivityWatch')
    requests = tasks.units('writing_activity', src, DAY1 - timedelta(hours=2), NOW)
    assert len(requests) == 1 and requests[0].unit == 'episode_day'
    assert list(requests[0].facts) == ['first_steer', 'last_steers', 'last_agent_reply']
    assert 'README' in requests[0].facts['first_steer']


def test_missing_human_context_stays_unavailable_without_changing_work_categories(tmp_path):
    cfg, src = source(tmp_path)
    src._s.append(_steer('missing', 'session', DAY1 + timedelta(minutes=1)))
    assert tasks.units('writing_activity', src, DAY1 - timedelta(hours=2), NOW) == []
    assert src.partial_text_units and src.missing_text_units
    assert len(tasks.units('work_category', src, DAY1 - timedelta(hours=2), NOW)) == 1


def test_normal_run_uses_independent_policies(tmp_path, monkeypatch, configured, sink):  # noqa: F811
    base, writing, old_policy, old_id, loads = configured
    cfg, src = source(tmp_path, label_tasks=('steer_intent', 'writing_activity'))
    writing_id = local.load_policy(writing)[1]
    with _env(tmp_path):
        report = run.run_label(cfg, src=src, local_estimates=True, now=NOW)
    events = [e for batch in sink.rows for e in batch]
    assert report['steer_intent']['rows'] == report['writing_activity']['rows'] == 1
    assert {e.attrs['task']: e.attrs['policy_id'] for e in events} == {
        'working.local.steer_intent': old_id, 'working.local.writing_activity': writing_id}


def test_writing_only_run_does_not_require_an_unrelated_recipe(tmp_path, configured, sink):  # noqa: F811
    base, writing, old_policy, old_id, loads = configured
    base.unlink()
    cfg, src = source(tmp_path, label_tasks=('writing_activity',))
    with _env(tmp_path):
        assert run.run_label(cfg, src=src, local_estimates=True, now=NOW)['writing_activity']['rows'] == 1


def test_standard_run_cannot_bypass_provisional_writing_provenance(tmp_path, configured, sink):  # noqa: F811
    cfg, src = source(tmp_path, label_tasks=('writing_activity',))
    with _env(tmp_path):
        result = run.run_label(cfg, src=src, local_estimates=False, now=NOW,
            backend_for=lambda *args: pytest.fail('Writing must require local-estimate provenance'))
    assert result['writing_activity']['failed'] == 1
    assert not sink.rows and configured[-1] == []


def test_replay_uses_writing_recipe_and_keeps_existing_progress_separate(tmp_path, monkeypatch, configured):
    base, writing, old_policy, old_id, loads = configured
    cfg, src = source(tmp_path)
    sent = []
    monkeypatch.setattr(replay, 'send_activity_rows', lambda dsn, events: sent.extend(events))
    with _env(tmp_path):
        preview = replay.prepare(cfg, 'writing_activity', (NOW - timedelta(days=3)).astimezone().date(),
                                 NOW.astimezone().date(), src=src, now=NOW)
        row = recipe()
        row['minimum_score'] = .7
        write_policy(base, {'steer_intent': row})
        assert replay.resume(cfg, preview['job_id'], apply=True)['complete']
        assert replay.resume(cfg, preview['job_id'], apply=True)['delivered'] == 0
    assert len(sent) == 1 and sent[0].attrs['task'] == 'working.local.writing_activity'
    assert sent[0].attrs['label'] == 'writing'
