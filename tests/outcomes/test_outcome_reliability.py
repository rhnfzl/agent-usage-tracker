"""Missing local evidence is not proof of abandonment or a permanent unmerged PR."""

from agent_usage_tracker import outcomes as oc


def test_old_commit_without_a_local_clone_stays_undecided():
    rows = [{"sha": "abcdef1234", "repo_key": "repo", "ts": 1.0,
             "session_id": "s", "amend": False}]
    results, _ = oc.evaluate(rows, [], now_ts=100 * 86400, cache={})
    assert results[0]["state"] == "undecided"
    assert results[0]["sticky"] is True


def test_negative_pr_cache_does_not_hide_a_later_merge(monkeypatch):
    monkeypatch.setattr(oc, "gh_merged", lambda repo, sha: True)
    cache = {"gh:abcdef": False}
    result = oc._ask_gh(oc._GhJob("owner/repo", "abcdef", 1.0, False), 100 * 86400, cache)
    assert result.state == "merged"
    assert cache["gh:abcdef"] is True


def test_negative_pr_answers_are_not_permanent_cache_entries(monkeypatch):
    monkeypatch.setattr(oc, "gh_merged", lambda repo, sha: False)
    cache = {}
    oc._ask_gh(oc._GhJob("owner/repo", "abcdef", 1.0, True), 100 * 86400, cache)
    assert cache == {}
