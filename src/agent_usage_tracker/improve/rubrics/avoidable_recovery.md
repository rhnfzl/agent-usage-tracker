---
id: avoidable_recovery
version: 1
threshold: 0.7
priority: 10
blocking: true
checks: {"fail_cause_not_in": ["expected_exploration", "unknown"]}
qualify: {"min_fail_labels": 20, "min_failure_recall": 0.7, "max_false_alarm_rate": 0.2}
applies_to: {"triggers": ["error_result", "control"]}
---
## Question
Does the observed attempt waste effort through avoidable recovery?

## Definition
fail: repeated attempts ignore the same known cause, or an evidenced wrong setup instruction forces recovery; pass: observed exploration or negative tests are useful and bounded; unknown: intent, relevant results, or cause cannot be established.

## Labels
- pass: Observed effort is useful and bounded, with no unnecessary recovery.
- fail: Repeated attempts ignore the same known cause, or a wrong setup instruction demonstrably forces recovery.
- unknown: The relevant results, intent, or cause are insufficient to decide whether recovery was avoidable.
