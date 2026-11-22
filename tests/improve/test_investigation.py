import json

import pytest

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.store import Store


@pytest.fixture
def evidence(tmp_path):
    store = Store(tmp_path / "private")
    keys = []
    for text in ("setup failed; inspect retry", "retry followed compaction", "compaction recovered"):
        key = store.put("sessions", {"agent": "codex", "coverage": {},
                        "events": [{"id": "result", "kind": "result", "text": text}]})
        keys.append(key)
    audits = [store.put("audits", {"session_ids": batch, "coverage": {}})
              for batch in (keys[:1], keys[1:])]
    return store, audits, keys


def test_recursive_recipe_and_replay(evidence, tmp_path, capsys):
    pytest.importorskip("pydantic_monty")
    store, audits, keys = evidence
    args = ["evidence", "--root", str(store.root), "--audit", audits[0], "--content", "redacted"]
    path = tmp_path / "recipe.py"
    path.write_text("""first = read("search", query=params["seed"])
second = read("search", scope="2", lead=first["receipt"], query="retry")
third = read("search", scope="2", lead=second["receipt"], query="recovered")
[item["session_id"] for page in [first, second, third] for item in page["data"]["items"]]
""", encoding="utf-8")
    assert main([*args, "run-code", str(path), "--include-audit", audits[1],
                 "--params", '{"seed":"setup failed"}', "--save"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == keys
    assert [row["lead"] for row in result["reads"]] == [None, 1, 2]
    assert result["reads"][2]["references"] == [{"session_id": keys[2], "event_id": "result"}]
    assert main([*args, "replay-code", result["run_id"], "--include-audit", audits[1]]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["matches"] is True
    assert replay["result"] == keys


def test_bundle_grant_and_direct_reads(evidence, tmp_path, capsys):
    pytest.importorskip("pydantic_monty")
    store, audits, keys = evidence
    args = ["evidence", "--root", str(store.root), "--audit", audits[0], "--content", "metadata"]
    bundle = tmp_path / "observed.json"
    assert main([*args, "export", str(bundle), keys[0]]) == 0
    capsys.readouterr()
    path = tmp_path / "recipe.py"
    path.write_text(f'read("event", session_id="{keys[0]}", event_id="result")["data"].get("text")')
    assert main(["evidence", "--bundle", str(bundle), "--content", "full",
                 "run-code", str(path)]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["result"] is None
    assert value["scopes"]["1"]["content"] == "metadata"


def test_recursive_search_matches_direct_evidence_across_pages(
        evidence, tmp_path, capsys):
    from agent_usage_tracker.improve.access import Evidence

    pytest.importorskip("pydantic_monty")
    store, audits, keys = evidence
    first = Evidence("redacted", store, audits[0])
    related = Evidence("redacted", store, audits[1])
    seed = first.search(query="setup failed", limit=1)
    query = seed["items"][0]["preview"].split()[-1]
    next_page = related.search(query=query, limit=1)
    tail_query = next_page["items"][0]["preview"].split()[-1]
    counterexamples = related.search(query=tail_query, limit=1)
    rest = related.search(query=tail_query, limit=1, cursor=counterexamples["next_cursor"])
    pages = [seed, next_page, counterexamples, rest]
    expected = sorted({item["session_id"] for page in pages for item in page["items"]})

    path = tmp_path / "follow.py"
    path.write_text("""
start = read("search", query="setup failed", limit=1)
lead = start["data"]["items"][0]["preview"].split()[-1]
middle = read("search", scope="2", query=lead, limit=1, lead=start["receipt"])
query = middle["data"]["items"][0]["preview"].split()[-1]
found = {start["data"]["items"][0]["session_id"], middle["data"]["items"][0]["session_id"]}
receipt = middle["receipt"]
cursor = ""
while cursor is not None:
    page = read("search", scope="2", query=query, limit=1, cursor=cursor, lead=receipt)
    for hit in page["data"]["items"]:
        found.add(hit["session_id"])
    cursor = page["data"]["next_cursor"]
    receipt = page["receipt"]
sorted(found)
""", encoding="utf-8")
    assert main(["evidence", "--root", str(store.root), "--audit", audits[0],
                 "run-code", str(path), "--include-audit", audits[1]]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["result"] == expected == sorted(keys)
    assert outcome["calls"] == len(pages)
    assert outcome["reads"][-1]["has_more"] is False
    assert len(json.dumps(outcome["result"])) < len(json.dumps(pages))


@pytest.mark.parametrize("source", [
    'read("search", limit=True)',
    'read("search", scope="other")',
    'read("search", lead=1)',
    'while True:\n    pass',
    '"x" * 100000',
])
def test_rejects_invalid_or_unbounded_programs(evidence, tmp_path, capsys, source):
    pytest.importorskip("pydantic_monty")
    store, audits, _ = evidence
    path = tmp_path / "recipe.py"
    path.write_text(source, encoding="utf-8")
    assert main(["evidence", "--root", str(store.root), "--audit", audits[0],
                 "run-code", str(path)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "investigation:" in output.err


def test_print_and_fresh_scope_recipe(evidence, tmp_path, capsys):
    pytest.importorskip("pydantic_monty")
    store, audits, _ = evidence
    recipe = tmp_path / "recipe.py"
    recipe.write_text('print("observed")\nscopes["1"]["sessions"]', encoding="utf-8")
    base = ["evidence", "--root", str(store.root), "--audit"]
    assert main([*base, audits[0], "run-code", str(recipe), "--save"]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["stdout"] == "observed\n"
    assert observed["result"] == 1
    assert main([*base, audits[1], "replay-code", observed["run_id"]]) == 1
    assert "evidence scope" in capsys.readouterr().err
    assert main([*base, audits[1], "run-code", "--recipe", observed["run_id"]]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["result"] == 2



def test_unicode_source_parameters_and_output(evidence, tmp_path, capsys):
    pytest.importorskip("pydantic_monty")
    store, audits, _ = evidence
    recipe = tmp_path / "recipe.py"
    recipe.write_text('"café " + params["place"]', encoding="utf-8")
    assert main(["evidence", "--root", str(store.root), "--audit", audits[0],
                 "run-code", str(recipe), "--params", '{"place":"東京"}']) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "café 東京"
