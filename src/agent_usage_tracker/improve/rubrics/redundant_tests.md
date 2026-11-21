---
id: redundant_tests
version: 1
threshold: 0.7
priority: 40
blocking: false
applies_to: {"triggers": ["error_result", "control"], "requires": ["test_command"]}
max_cases: 4
checks: {"fail_must_cite": ["call"]}
qualify: {"min_fail_labels": 20, "min_failure_recall": 0.7, "max_false_alarm_rate": 0.2}
---
## Question
Does each test run in the observed attempt follow a change or narrow a failure?

## Definition
Judge only the test commands that appear in the attempt. A rerun after a file change, or a narrower run after a failure, is useful. fail: the same test command ran again with no file change between runs and no new information, or a full suite ran where one cited test would answer; pass: each test run follows a change or narrows a failure; unknown: file changes between the runs cannot be seen.

## Labels
- pass: Each test run follows a change or narrows a failure.
- fail: The same test command ran again with no file change and no new information, or a full suite ran where one cited test would answer.
- unknown: File changes between the test runs cannot be seen.
