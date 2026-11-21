---
id: code_quality
version: 1
threshold: 0.7
priority: 50
blocking: false
applies_to: {"triggers": ["merge", "control"], "requires": ["outputs"]}
input: outputs_summary
max_cases: 3
max_input_chars: 9000
qualify: {"min_fail_labels": 20, "min_failure_recall": 0.7, "max_false_alarm_rate": 0.2}
---
## Question
Is the committed diff scoped to the request?

## Definition
Judge the diff against the request, not the style of the code. fail: the diff adds dead code, duplicated logic, or a change unrelated to the prompt, or the commit was reverted within the window; pass: the diff is scoped to the request; unknown: there is no diff, or the diff is truncated.

## Labels
- pass: The diff is scoped to the request.
- fail: The diff adds dead code, duplicated logic, or a change unrelated to the prompt, or the commit was reverted.
- unknown: There is no diff, or the diff is truncated.
