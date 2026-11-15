"""Invented search tasks keep answer correctness separate from execution evidence."""

import copy
import json

import pytest


FILES = {
    "grove/main.py": "# invented fixture\nLABEL = 'copper_gate'\n",
    "grove/nested/worker.py": "VALUE = 3\nLABEL = 'copper_gate'\n",
    "harbor/main.py": "LABEL = 'other_gate'\n",
    "notes.txt": "copper_gate is not Python\n",
}
HITS = [{"path": "grove/main.py", "line": 2}, {"path": "grove/nested/worker.py", "line": 2}]
OUTPUT = "grove/main.py:2:LABEL = 'copper_gate'\ngrove/nested/worker.py:2:LABEL = 'copper_gate'"


def fixture(tmp_path, status="found"):
    from agent_usage_tracker.improve.search_check import SearchHit, SearchTask

    for name, text in FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    scope = {"found": "grove", "not_found": "harbor", "unavailable": "quarry"}[status]
    hits = tuple(SearchHit(**hit) for hit in HITS) if status == "found" else ()
    return SearchTask(scope, "copper_gate", FILES, status, hits)


def pair(key, name, data, text, error=False):
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": key, "name": name, "input": data}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": key, "content": text, "is_error": error}]}},
    ]


def stream(task, evidence, answer=None):
    return [
        {"type": "system", "subtype": "init"}, *evidence,
        {"type": "result", "subtype": "success", "is_error": False,
         "structured_output": answer or {
             "status": task.status, "hits": [vars(hit) for hit in task.hits]}},
    ]


def check(tmp_path, task, events):
    from agent_usage_tracker.improve.search_check import check_search_stream

    return check_search_stream("\n".join(json.dumps(event) for event in events), task, tmp_path)


def search(command="rg -n -g '*.py' copper_gate grove", output=OUTPUT, error=False):
    return pair("search", "Bash", {"command": command}, output, error)


def test_complete_search_accepts_fixture_derived_hits(tmp_path):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search()))
    assert result.accepted
    assert result.correct is True and result.evidence_complete
    assert result.classification == "accepted"


@pytest.mark.parametrize("command", [
    "grep -rHn --include='*.py' copper_gate grove",
    "rg --line-number --type py copper_gate grove",
    "rg -n -F -e copper_gate -g '*.py' grove",
])
def test_supported_direct_search_options_cover_the_same_scope(tmp_path, command):
    task = fixture(tmp_path)
    assert check(tmp_path, task, stream(task, search(command))).accepted


def test_literal_directory_change_uses_command_relative_output(tmp_path):
    task = fixture(tmp_path)
    evidence = search("cd grove && rg -n -g '*.py' copper_gate .",
                      "./main.py:2:LABEL = 'copper_gate'\n./nested/worker.py:2:LABEL = 'copper_gate'")
    assert check(tmp_path, task, stream(task, evidence)).accepted


def test_directory_probe_cannot_hide_a_failed_prerequisite(tmp_path):
    task = fixture(tmp_path)
    assert check(tmp_path, task, stream(task, search(
        "test -d grove && rg -n -g '*.py' copper_gate grove"))).accepted
    assert not check(tmp_path, task, stream(task, search(
        "test -d quarry && rg -n -g '*.py' copper_gate grove"))).accepted


def test_true_empty_requires_successful_complete_search(tmp_path):
    task = fixture(tmp_path, "not_found")
    evidence = search("rg -n -g '*.py' copper_gate harbor", "Exit code 1", True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence = search("rg -n -g '*.py' copper_gate harbor || true", "")
    assert check(tmp_path, task, stream(task, evidence)).accepted


def test_missing_scope_requires_path_bound_missing_result(tmp_path):
    task = fixture(tmp_path, "unavailable")
    evidence = search("rg -n -g '*.py' copper_gate quarry",
                      "Exit code 2\nrg: quarry: No such file or directory (os error 2)", True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence = search("rg -n -g '*.py' copper_gate harbor", "Exit code 1", True)
    assert not check(tmp_path, task, stream(task, evidence)).accepted


def test_native_full_read_requires_exact_file_contents(tmp_path):
    task = fixture(tmp_path, "not_found")
    evidence = pair("read", "Read", {"file_path": "harbor/main.py"},
                    "1→LABEL = 'other_gate'")
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence[1]["message"]["content"][0]["content"] = "1→made up content"
    assert not check(tmp_path, task, stream(task, evidence)).accepted


def test_native_grep_requires_full_scope_and_exact_output(tmp_path):
    task = fixture(tmp_path, "not_found")
    evidence = pair("grep", "Grep", {"pattern": "copper_gate", "path": "harbor",
                    "glob": "*.py", "output_mode": "content"}, "No matches found")
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence[0]["message"]["content"][0]["input"]["head_limit"] = 1
    assert not check(tmp_path, task, stream(task, evidence)).accepted


@pytest.mark.parametrize("command", [
    "echo 'grove/main.py:2:copper_gate'",
    "rg -n -g '*.py' copper_gate grove | head -1",
    "rg -n -g '*.py' copper_gate grove > answer.txt",
    "rg -n -g '*.py' copper_gate grove 2>/dev/null",
    "rg -n -g '*.py' copper_gate grove --max-depth 0",
    "rg -n -g '*.py' copper_gate grove --glob '!worker.py'",
    "rg -n copper_gate grove/*.py",
    "rg -n copper_gate $(echo grove)",
    "cd quarry && rg -n -g '*.py' copper_gate grove",
])
def test_unsupported_or_suppressing_commands_never_earn_coverage(tmp_path, command):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search(command)))
    assert result.correct is True
    assert not result.evidence_complete and result.classification == "unknown"


@pytest.mark.parametrize("output,error", [(OUTPUT, True), (OUTPUT + "\npermission denied", False),
                                         ("", False), ("[output truncated]", False)])
def test_hidden_result_failure_and_incomplete_output_are_unknown(tmp_path, output, error):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search(output=output, error=error)))
    assert result.correct is True and not result.accepted


def test_correct_answer_without_execution_remains_unknown(tmp_path):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, []))
    assert result.correct is True and not result.evidence_complete


def test_wrong_answer_is_separate_from_complete_execution(tmp_path):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search(), {"status": "not_found", "hits": []}))
    assert result.correct is False and result.evidence_complete
    assert result.classification == "wrong_task" and not result.accepted


def test_partial_scope_never_proves_true_empty(tmp_path):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search("rg -n copper_gate grove/main.py",
                                                    "2:LABEL = 'copper_gate'")))
    assert not result.evidence_complete


@pytest.mark.parametrize("mutation", ["unmatched", "orphan", "conflict", "final_error",
                                     "duplicate_final", "result_before_call"])
def test_stream_integrity_failures_block_acceptance(tmp_path, mutation):
    task = fixture(tmp_path)
    events = stream(task, search())
    if mutation == "unmatched":
        events.pop(2)
    elif mutation == "orphan":
        events.insert(-1, pair("orphan", "Bash", {}, "")[1])
    elif mutation == "conflict":
        events[2:2] = search("echo forged")
    elif mutation == "final_error":
        events[-1]["is_error"] = True
    elif mutation == "duplicate_final":
        events.append(copy.deepcopy(events[-1]))
    else:
        events[1], events[2] = events[2], events[1]
    assert not check(tmp_path, task, events).accepted


def test_recovered_search_can_supply_independent_complete_evidence(tmp_path):
    task = fixture(tmp_path)
    bad = pair("failed", "Bash", {"command": "rg -n copper_gate grove"},
               "Exit code 2\nrg: permission denied", True)
    assert check(tmp_path, task, stream(task, bad + search())).accepted


@pytest.mark.parametrize("mutation", ["changed", "added", "deleted", "symlink"])
def test_fixture_integrity_failure_blocks_otherwise_correct_results(tmp_path, mutation):
    task = fixture(tmp_path)
    target = tmp_path / "grove/main.py"
    if mutation == "changed":
        target.write_text("changed\n")
    elif mutation == "added":
        (tmp_path / "extra.py").write_text("copper_gate\n")
    elif mutation == "deleted":
        target.unlink()
    else:
        target.unlink()
        target.symlink_to(tmp_path / "harbor/main.py")
    assert not check(tmp_path, task, stream(task, search())).accepted


def test_invalid_trusted_ground_truth_cannot_certify_a_result(tmp_path):
    from dataclasses import replace

    task = replace(fixture(tmp_path), hits=())
    assert not check(tmp_path, task, stream(task, search())).accepted


def test_scope_escape_is_uncredited(tmp_path):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search("rg -n copper_gate ../grove")))
    assert not result.accepted


def test_malformed_answer_status_is_unknown(tmp_path):
    task = fixture(tmp_path)
    result = check(tmp_path, task, stream(task, search(), {"status": [], "hits": []}))
    assert result.correct is None and not result.accepted


def test_exact_fixture_bytes_reject_newline_mutation(tmp_path):
    task = fixture(tmp_path)
    (tmp_path / "harbor/main.py").write_bytes(b"LABEL = 'other_gate'\r\n")
    assert not check(tmp_path, task, stream(task, search())).accepted


def test_a_search_can_certify_an_existing_directory_without_python_files(tmp_path):
    from dataclasses import replace

    task = fixture(tmp_path, "not_found")
    (tmp_path / "empty").mkdir()
    task = replace(task, scope="empty")
    evidence = search("rg -n -g '*.py' copper_gate empty", "Exit code 1", True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    assert not check(tmp_path, task, stream(task, [])).accepted


def test_default_search_does_not_claim_hidden_or_ignored_file_coverage(tmp_path):
    from dataclasses import replace

    task = fixture(tmp_path, "not_found")
    (tmp_path / "harbor/.hidden.py").write_text("VALUE = 4\n")
    task = replace(task, files={**FILES, "harbor/.hidden.py": "VALUE = 4\n"})
    evidence = search("rg -n -g '*.py' copper_gate harbor", "Exit code 1", True)
    assert not check(tmp_path, task, stream(task, evidence)).accepted
    evidence = search("rg -n --hidden -g '*.py' copper_gate harbor", "Exit code 1", True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    (tmp_path / ".ignore").write_text("main.py\n")
    task = replace(task, files={**task.files, ".ignore": "main.py\n"})
    assert not check(tmp_path, task, stream(task, evidence)).accepted


@pytest.mark.parametrize("command,output", [
    ("ls -ld quarry", "ls: quarry: No such file or directory"),
    ("stat quarry", "stat: quarry: stat: No such file or directory"),
])
def test_literal_missing_scope_probes_are_path_bound(tmp_path, command, output):
    task = fixture(tmp_path, "unavailable")
    evidence = search(command, "Exit code 1\n" + output, True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence = search(command, "Exit code 1\n" + output.replace("quarry", "other"), True)
    assert not check(tmp_path, task, stream(task, evidence)).accepted


def test_native_missing_scope_is_distinct_from_no_matches(tmp_path):
    task = fixture(tmp_path, "unavailable")
    evidence = pair("read", "Read", {"file_path": "quarry"}, "File does not exist.", True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence = pair("grep", "Grep", {"path": "quarry", "pattern": "copper_gate"},
                    "Path does not exist: quarry", True)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence[1]["message"]["content"][0]["content"] = "No matches found"
    assert not check(tmp_path, task, stream(task, evidence)).accepted


@pytest.mark.parametrize("command", [
    "alias rg='printf forged'", "function rg() { echo forged; }", "export PATH=/tmp/fake",
    "env PATH=/tmp/fake rg copper_gate grove", "echo forged", "unknown-command",
])
def test_unsupported_shell_state_cannot_certify_a_later_search(tmp_path, command):
    task = fixture(tmp_path)
    taint = pair("taint", "Bash", {"command": command}, "")
    result = check(tmp_path, task, stream(task, taint + search()))
    assert result.correct is True and not result.accepted


def test_independent_native_evidence_can_recover_from_unknown_shell_state(tmp_path):
    task = fixture(tmp_path, "not_found")
    taint = pair("taint", "Bash", {"command": "alias rg='printf forged'"}, "")
    evidence = pair("read", "Read", {"file_path": str(tmp_path / "harbor/main.py")},
                    "1→LABEL = 'other_gate'")
    assert check(tmp_path, task, stream(task, taint + evidence)).accepted


def test_bash_directory_changes_are_not_silently_reset_between_calls(tmp_path):
    task = fixture(tmp_path)
    changed = pair("cwd", "Bash", {"command": "cd grove && pwd"}, str(tmp_path / "grove"))
    assert not check(tmp_path, task, stream(task, changed + search())).accepted
    actual = search("rg -n -g '*.py' copper_gate .",
                    "./main.py:2:LABEL = 'copper_gate'\n./nested/worker.py:2:LABEL = 'copper_gate'")
    assert check(tmp_path, task, stream(task, changed + actual)).accepted


def test_fixed_shell_identity_probe_does_not_taint_search_evidence(tmp_path):
    task = fixture(tmp_path)
    metadata = pair("shell", "Bash", {
        "command": 'printf "AUT_SHELL=zsh:%s\\\\n" "$ZSH_VERSION"'}, "AUT_SHELL=zsh:5.9\n")
    assert check(tmp_path, task, stream(task, metadata + search())).accepted


def test_overlapping_bash_calls_cannot_assume_a_serial_directory_state(tmp_path):
    task = fixture(tmp_path)
    changed = pair("cwd", "Bash", {"command": "cd grove && pwd"}, str(tmp_path / "grove"))
    found = search()
    evidence = [found[0], changed[0], changed[1], found[1]]
    assert not check(tmp_path, task, stream(task, evidence)).accepted


def test_unverified_directory_change_taints_later_shell_evidence(tmp_path):
    task = fixture(tmp_path)
    changed = pair("cwd", "Bash", {"command": "cd grove && pwd"}, "Exit code 2", True)
    assert not check(tmp_path, task, stream(task, changed + search())).accepted


def test_unknown_tool_cannot_shadow_later_shell_search(tmp_path):
    task = fixture(tmp_path)
    mutation = pair("mutation", "Write", {"file_path": "/tmp/unknown"}, "done")
    assert not check(tmp_path, task, stream(task, mutation + search())).accepted


@pytest.mark.parametrize("probe", ["ls grove", "ls -1 grove"])
def test_existing_directory_listing_is_a_literal_read_only_probe(tmp_path, probe):
    task = fixture(tmp_path)
    evidence = search(probe + " && rg -n -g '*.py' copper_gate grove",
                      "main.py\nnested\n" + OUTPUT)
    assert check(tmp_path, task, stream(task, evidence)).accepted
    evidence[1]["message"]["content"][0]["content"] = "invented.py\n" + OUTPUT
    assert not check(tmp_path, task, stream(task, evidence)).accepted


@pytest.mark.parametrize("suffix,marker", [('echo "exit:$?"', "exit:1"),
                                          ('echo "$?"', "1"), ('echo $?', "1")])
def test_expanded_exit_marker_certifies_only_the_actual_search_status(tmp_path, suffix, marker):
    task = fixture(tmp_path, "not_found")
    command = "rg -n -g '*.py' copper_gate harbor; " + suffix
    assert check(tmp_path, task, stream(task, search(command, marker))).accepted
    assert not check(tmp_path, task, stream(task, search(command, marker.replace("1", "0")))).accepted


@pytest.mark.parametrize("suffix", ["echo 'exit:$?'", r'echo "exit:\$?"', "echo '$?'",
                                   r"echo \$?", 'echo "exit:$HOME"'])
def test_literal_or_other_variable_markers_do_not_claim_an_exit_status(tmp_path, suffix):
    task = fixture(tmp_path, "not_found")
    command = "rg -n -g '*.py' copper_gate harbor; " + suffix
    assert not check(tmp_path, task, stream(task, search(command, "exit:1"))).accepted


def test_fake_output_and_failed_prerequisite_cannot_gain_coverage_from_a_status_marker(tmp_path):
    task = fixture(tmp_path)
    evidence = search('echo forged; echo "exit:$?"', OUTPUT + "\nexit:0")
    assert not check(tmp_path, task, stream(task, evidence)).accepted
    evidence = search('test -d quarry && rg -n -g "*.py" copper_gate grove; echo "exit:$?"',
                      "exit:1")
    assert not check(tmp_path, task, stream(task, evidence)).accepted


def test_status_marker_after_a_positive_search_must_be_the_final_output_line(tmp_path):
    task = fixture(tmp_path)
    command = 'rg -n -g "*.py" copper_gate grove; echo "exit:$?"'
    assert check(tmp_path, task, stream(task, search(command, OUTPUT + "\nexit:0"))).accepted
    assert not check(tmp_path, task, stream(task, search(command, "exit:0\n" + OUTPUT))).accepted


def test_true_empty_error_without_explicit_exit_status_stays_unknown(tmp_path):
    task = fixture(tmp_path, "not_found")
    evidence = search("rg -n -g '*.py' copper_gate harbor", "", True)
    assert not check(tmp_path, task, stream(task, evidence)).accepted


def test_special_fixture_entry_rejects_without_opening_the_fifo(tmp_path):
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO inventory guard applies on POSIX")
    task = fixture(tmp_path)
    os.mkfifo(tmp_path / "extra.py")
    assert not check(tmp_path, task, stream(task, search())).accepted


def test_overlap_preserves_correct_facts_when_directory_state_is_uncertain(tmp_path):
    task = fixture(tmp_path)
    changed = pair("cwd", "Bash", {"command": "cd grove && pwd"}, str(tmp_path / "grove"))
    found = search()
    result = check(tmp_path, task, stream(task, [found[0], changed[0], changed[1], found[1]]))
    assert result.correct is True and not result.evidence_complete
    assert result.classification == "unknown"


def test_state_free_overlapping_searches_commute_at_the_known_directory(tmp_path):
    task = fixture(tmp_path)
    first = pair("one", "Bash", {"command": "rg -n copper_gate grove/main.py"},
                 "2:LABEL = 'copper_gate'")
    second = pair("two", "Bash", {"command": "rg -n copper_gate grove/nested/worker.py"},
                  "2:LABEL = 'copper_gate'")
    events = [first[0], second[0], second[1], first[1]]
    assert check(tmp_path, task, stream(task, events)).accepted


def test_overlap_with_unknown_shell_state_cannot_credit_an_earlier_pending_search(tmp_path):
    task = fixture(tmp_path)
    found = search()
    changed = pair("state", "Bash", {"command": "export PATH=/tmp/unknown"}, "")
    result = check(tmp_path, task, stream(task, [found[0], changed[0], changed[1], found[1]]))
    assert result.correct is True and not result.evidence_complete


def test_native_read_can_recover_after_an_uncertain_overlap_group(tmp_path):
    task = fixture(tmp_path, "not_found")
    found = search("rg -n copper_gate harbor", "Exit code 1", True)
    changed = pair("state", "Bash", {"command": "cd grove && pwd"}, str(tmp_path / "grove"))
    native = pair("native", "Read", {"file_path": str(tmp_path / "harbor/main.py")},
                  "1→LABEL = 'other_gate'")
    evidence = [found[0], changed[0], found[1], changed[1], *native]
    assert check(tmp_path, task, stream(task, evidence)).accepted


@pytest.mark.parametrize("suffix,marker", [
    ('echo "STATUS=$?"', "STATUS=1"), ('echo rc:$?', "rc:1"),
    ('echo "search result / $?"', "search result / 1"), ('echo result_$?', "result_1"),
])
def test_status_echo_accepts_safe_literal_prefixes_without_a_label_allowlist(tmp_path, suffix, marker):
    task = fixture(tmp_path, "not_found")
    command = "rg -n -g '*.py' copper_gate harbor; " + suffix
    assert check(tmp_path, task, stream(task, search(command, marker))).accepted
    assert not check(tmp_path, task, stream(task, search(command, marker.replace("1", "0")))).accepted


@pytest.mark.parametrize("suffix", [
    "echo 'STATUS=$?'", r'echo "STATUS=\$?"', 'echo "STATUS=$HOME$?"',
    'echo "STATUS=`pwd`$?"', 'echo "STATUS=$(pwd)$?"', 'echo "STATUS=$?suffix"',
])
def test_status_prefix_cannot_add_another_expansion_or_literal_variable(tmp_path, suffix):
    task = fixture(tmp_path, "not_found")
    command = "rg -n -g '*.py' copper_gate harbor; " + suffix
    result = check(tmp_path, task, stream(task, search(command, "STATUS=1")))
    assert result.correct is True and not result.accepted


@pytest.mark.parametrize("order", [("search_call", "write_call", "write_result", "search_result"),
                                   ("search_call", "write_call", "search_result", "write_result"),
                                   ("write_call", "search_call", "search_result", "write_result")])
def test_overlapping_unknown_mutator_cannot_certify_a_pending_search(tmp_path, order):
    task = fixture(tmp_path)
    found = search()
    mutation = pair("mutation", "Write", {"file_path": "/tmp/unknown"}, "done")
    events = dict(zip(("search_call", "search_result", "write_call", "write_result"), found + mutation))
    result = check(tmp_path, task, stream(task, [events[key] for key in order]))
    assert result.correct is True and not result.evidence_complete
    assert result.classification == "unknown"


def test_unknown_tool_cannot_claim_the_bash_metadata_exemption_during_overlap(tmp_path):
    task = fixture(tmp_path)
    found = search()
    mutation = pair("mutation", "Write", {
        "command": 'printf "AUT_SHELL=zsh:%s\\\\n" "$ZSH_VERSION"'}, "done")
    result = check(tmp_path, task, stream(task, [found[0], mutation[0], mutation[1], found[1]]))
    assert result.correct is True and not result.evidence_complete


def test_independent_read_recovers_after_a_search_overlaps_an_unknown_mutator(tmp_path):
    task = fixture(tmp_path, "not_found")
    found = search("rg -n copper_gate harbor", "Exit code 1", True)
    mutation = pair("mutation", "Write", {"file_path": "/tmp/unknown"}, "done")
    native = pair("read", "Read", {"file_path": str(tmp_path / "harbor/main.py")},
                  "1→LABEL = 'other_gate'")
    evidence = [found[0], mutation[0], found[1], mutation[1], *native]
    assert check(tmp_path, task, stream(task, evidence)).accepted


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "StructuredOutput"])
def test_independent_read_only_tools_do_not_taint_an_overlapping_search(tmp_path, tool):
    task = fixture(tmp_path)
    found = search()
    readonly = pair("readonly", tool, {}, "")
    evidence = [found[0], readonly[0], readonly[1], found[1]]
    assert check(tmp_path, task, stream(task, evidence)).accepted
