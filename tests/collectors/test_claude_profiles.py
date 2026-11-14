"""Configured T3 Claude profiles carry native usage without guessed human turns."""

import json

import pytest

from agent_usage_tracker.collectors import claude_code
from agent_usage_tracker.event import UsageEvent
from test_collectors import _cfg, _drain, _write_jsonl


def configured_profile(home, profile):
    settings = home / '.t3/userdata/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'providerInstances': {
        'personal': {'driver': 'claudeAgent', 'config': {'homePath': str(profile)}},
        'alias': {'driver': 'claudeAgent', 'config': {'homePath': str(profile)}},
        'unrelated': {'driver': 'codex', 'config': {'homePath': '/not-claude'}},
    }}))


def test_claude_configured_profile_usage_is_discovered_once(tmp_path):
    home = tmp_path / 'home'
    profile = tmp_path / 'custom-claude'
    configured_profile(home, profile)
    records = [{'type': 'user', 'uuid': 'u', 'sessionId': 's', 'entrypoint': 't3-code',
                'timestamp': '2026-01-01T00:00:00Z', 'message': {'content': 'Fix it'}},
               {'type': 'assistant', 'uuid': 'a', 'sessionId': 's', 'entrypoint': 't3-code',
                'timestamp': '2026-01-01T00:00:01Z', 'message': {'id': 'm',
                'model': 'claude', 'usage': {'input_tokens': 2, 'output_tokens': 3}}}]
    path = profile / 'projects/p/s.jsonl'
    _write_jsonl(path, records)
    assert claude_code.main_session_files(home) == [str(path)]
    rows = _drain(claude_code.collect, _cfg(home))
    usage = [r for r in rows if isinstance(r, UsageEvent)]
    assert len(usage) == 1
    assert usage[0].total_tokens == 5
    assert usage[0].kind == 'agent'
    assert usage[0].session.run_origin == 'unknown'
    assert all(r.kind != 'steer' for r in rows)


def test_claude_profile_subagents_keep_their_parent_identity(tmp_path):
    home = tmp_path / 'home'
    profile = tmp_path / 'custom-claude'
    configured_profile(home, profile)
    path = profile / 'projects/p/parent/subagents/agent-child.jsonl'
    _write_jsonl(path, [{'type': 'assistant', 'uuid': 'a', 'sessionId': 'parent',
        'agentId': 'child', 'timestamp': '2026-01-01T00:00:01Z',
        'message': {'usage': {'input_tokens': 2, 'output_tokens': 3}}}])
    rows = [r for r in _drain(claude_code.collect, _cfg(home)) if isinstance(r, UsageEvent)]
    assert len(rows) == 1
    assert rows[0].session.run_origin == 'subagent'
    assert rows[0].session.parent_session_id == 'parent'
    assert claude_code.main_session_files(home) == []


def test_t3_client_alone_is_not_a_human_prompt():
    assert claude_code.human_text({'type': 'user', 'entrypoint': 't3-code',
                                 'message': {'content': 'Software forwarded this'}}) is None


def test_bad_profile_settings_do_not_hide_unrelated_codex_review(tmp_path):
    from agent_usage_tracker.collectors.session_digest import local_info

    settings = tmp_path / '.t3/userdata/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{')
    _write_jsonl(tmp_path / '.codex/sessions/rollout-codex-id.jsonl', [
        {'type': 'session_meta', 'payload': {'id': 'codex-id', 'source': 'cli'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Fix it'}},
    ])
    assert 'codex-id' in local_info(tmp_path, {'codex-id'})


def test_unknown_profile_username_reports_unavailable(tmp_path):
    from agent_usage_tracker.event import CollectorUnavailable

    configured_profile(tmp_path, '~aut-review-user-does-not-exist')
    with pytest.raises(CollectorUnavailable, match='configuration is unreadable'):
        claude_code.projects_dirs(tmp_path)
