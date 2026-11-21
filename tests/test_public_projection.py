"""Keep private usage and identity fields out of the published aggregate format."""

import json

from agent_usage_tracker.observation_report import public_draft


def test_public_projection_uses_only_the_existing_aggregate_fields():
    scope = "private-account-fixture"
    employer = "fixture-employer-name"
    path = "/identity-fixture/private/path"
    argument = "secret argument fixture"
    prompt = "private prompt fixture"
    private_cost = "12345.6789"
    token_count = 987654
    report = {
        "generated_at": "2026-06-15T00:00:00+00:00", "data_through": None,
        "usage": {"input_tokens": {"total": token_count}, "cost_usd": private_cost},
        "quotas": [{"scope": scope, "path": path}],
        "tools": {"private": {"role": "primary", "source": employer,
                              "stream": path, "account": scope, "arguments": argument,
                              "ok": 1, "error": 0, "unknown": 0}},
        "studies": [{"scoped_decision": "inconclusive", "prompt": prompt, "cost_usd": private_cost}],
        "coverage": {"observations": 1, "source": employer, "scope": scope},
    }
    projection = public_draft(report)
    assert set(projection) == {"schema_version", "publication_status", "generated_at",
                               "data_through", "observed_tools", "study_decisions", "coverage"}
    assert projection["observed_tools"]["other"] == {"ok": 1, "error": 0, "unknown": 0}
    text = json.dumps(projection)
    for private in (scope, employer, path, argument, prompt, private_cost, str(token_count)):
        assert private not in text
