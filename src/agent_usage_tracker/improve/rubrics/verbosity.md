---
id: verbosity
version: 1
threshold: 0.7
priority: 30
blocking: false
applies_to: {"triggers": ["error_result", "control"], "requires": ["final_reply"]}
max_cases: 4
checks: {"fail_must_cite": ["reply"]}
qualify: {"min_fail_labels": 20, "min_failure_recall": 0.7, "max_false_alarm_rate": 0.2}
---
## Question
Is the assistant's final reply no longer than the result needs?

## Definition
Judge only the final reply to this attempt, not the work before it. Do not count a needed command, error, or open issue as padding. fail: the final reply repeats tool output, restates the prompt, or is padded beyond what the result needs; pass: the reply states the result, its evidence, and open issues briefly; unknown: the final reply is clipped or missing.

## Labels
- pass: The final reply states the result, its evidence, and open issues briefly.
- fail: The final reply repeats tool output, restates the prompt, or is padded beyond what the result needs.
- unknown: The final reply is clipped or missing, so its length cannot be judged.
