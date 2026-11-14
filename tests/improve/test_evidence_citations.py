"""Native duplicate replies retain citation provenance without hiding disagreements."""

import pytest

from agent_usage_tracker.improve.evidence import read_session
from .test_evidence import write_log


BODY = "Checks passed.\n\n"
CITATION = (
    "<oai-mem-citation>\n<citation_entries>\n"
    "MEMORY.md:4-6|note=[verification rules]\n"
    "</citation_entries>\n<rollout_ids>\n"
    "00000000-0000-0000-0000-000000000000\n"
    "</rollout_ids>\n</oai-mem-citation>"
)


def metadata():
    return {"entries": [{"path": "MEMORY.md", "lineStart": 4,
                         "lineEnd": 6, "note": "verification rules"}],
            "rolloutIds": ["00000000-0000-0000-0000-000000000000"]}


def messages(citation=None, body=BODY):
    return [
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "reply", "phase": "final_answer",
            "content": [{"type": "Text", "text": body}],
            "memory_citation": citation}}},
        {"type": "response_item", "payload": {"type": "message", "id": "reply",
            "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": BODY + CITATION}]}},
    ]


def trace(tmp_path, records):
    return write_log(tmp_path / "native.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Run checks"}},
        *records,
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])


@pytest.mark.parametrize("reverse", [False, True])
def test_native_citation_renderings_deduplicate_in_either_order(tmp_path, reverse):
    records = messages(metadata())
    evidence = read_session(trace(tmp_path, records[::-1] if reverse else records), "codex")
    assert evidence["coverage"]["complete"]
    assert evidence["coverage"].get("conflicting_records", 0) == 0
    assert evidence["coverage"]["duplicate_records"] == 1
    replies = [e for e in evidence["events"] if e["kind"] == "reply"]
    assert len(replies) == 1
    assert replies[0]["text"] == BODY + CITATION.replace(
        "00000000-0000-0000-0000-000000000000", "[redacted]")


def test_same_id_with_changed_reply_body_remains_conflicting(tmp_path):
    evidence = read_session(trace(tmp_path, messages(metadata(), "Checks failed.\n\n")), "codex")
    assert evidence["coverage"]["conflicting_records"] == 1
    assert not evidence["coverage"]["complete"]


def test_same_id_with_changed_citation_remains_conflicting(tmp_path):
    citation = metadata()
    citation["entries"][0]["note"] = "different evidence"
    evidence = read_session(trace(tmp_path, messages(citation)), "codex")
    assert evidence["coverage"]["conflicting_records"] == 1
    assert not evidence["coverage"]["complete"]


@pytest.mark.parametrize("citation", [None, {}, {"entries": "wrong"},
    {"entries": [None], "rolloutIds": []},
    {"entries": [{"path": 7, "lineStart": True, "lineEnd": 2, "note": []}],
     "rolloutIds": [None]}])
def test_malformed_citation_metadata_never_erases_a_disagreement(tmp_path, citation):
    evidence = read_session(trace(tmp_path, messages(citation)), "codex")
    assert evidence["coverage"]["conflicting_records"] == 1
    assert not evidence["coverage"]["complete"]


def test_citation_normalization_does_not_make_clipped_attempt_complete(tmp_path):
    body = "ordinary text " * 500
    records = messages(metadata(), body)
    records[1]["payload"]["content"][0]["text"] = body + CITATION
    evidence = read_session(trace(tmp_path, records), "codex")
    assert evidence["coverage"].get("conflicting_records", 0) == 0
    assert evidence["coverage"]["duplicate_records"] == 1
    assert evidence["coverage"]["truncated_events"] == 1
    assert not evidence["coverage"]["complete"]
