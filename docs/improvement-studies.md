# Inspect a repair from incident to follow-up

A private study connects the problem, proposed repair, controlled results and fresh observations.
It makes the evidence behind a decision inspectable without changing installed instructions.

Use an existing audit, proposal and trusted trial specification from the
[improvement workflow](improvement.md). All study records stay in the same private store.

## Start with observed incidents

```bash
aut improve study shortlist --audit AUDIT_ID
```

The shortlist deduplicates source-session and event identities across snapshots.
Repeated snapshots do not establish recurrence. Groups are investigation signals, not proof of a cause.
Review source coverage before choosing discovery cases and independent clean controls.

## Freeze a policy before running the trial

Create a private JSON file with these fields:

```json
{
  "version": 1,
  "discovery_cases": ["DISCOVERY_CASE_ID"],
  "control_cases": ["CONTROL_CASE_ID"],
  "task_families": {
    "DISCOVERY_CASE_ID": "discovery-family",
    "CONTROL_CASE_ID": "control-family"
  },
  "proposal_id": "PROPOSAL_ID",
  "trial_spec_id": "TRIAL_SPEC_ID",
  "primary_metric": "accepted",
  "quality_constraints": ["Preserve independent acceptance"],
  "decision_policy": {
    "direction": "increase",
    "minimum_improvement": 0,
    "require_no_acceptance_regressions": true,
    "require_all_candidate_accepted": true
  },
  "budget_s": 180,
  "provenance": {
    "setup": null,
    "model": null,
    "tools": null,
    "instructions": null
  },
  "reference_result_ids": [],
  "cost_inputs": {
    "operator_minutes": null,
    "money": null
  }
}
```

Replace identifiers with existing private record IDs. Use multiple independent discovery sources
to establish recurrence. A source description in `provenance` records what you observed;
it does not prove that the agent loaded an instruction. Keep unavailable observations null.
`money` uses one operator-chosen accounting unit. AUT does not infer subscription spend.

```bash
aut improve study create --spec-file /private/study.json
aut improve trial --spec TRIAL_SPEC_ID --execute --budget-s 180
aut improve study assess --study STUDY_ID
```

Policy creation rejects experiments with existing stored results, including metadata-only copies.
Changing a trial label, ordering, seed, repeat count or ignored note does not make it prospective.
This checks local history; it cannot establish that an experiment never ran outside AUT.

`primary_metric` is `accepted`, `elapsed_s`, `tool_calls` or `interventions`.
Direction is `increase` or `decrease`. Paired candidate-minus-baseline differences use the metric's units.
The direction-adjusted improvement must strictly exceed `minimum_improvement`.
Zero benefit remains inconclusive. Missing or invalid observations cannot support a benefit.

`quality_constraints` preserves the operator's description. The two boolean policy fields implement
the acceptance rules. AUT does not execute arbitrary natural-language constraints.

For a supported scoped result, fixture maps must pin the proposal target to its before/after hashes.
Each command-check case also needs protected `acceptance_files` for its external command inputs.
The runner checks these hashes before and after execution. A self-modifying oracle produces a
preserved rejected receipt, not an ordinary baseline failure that can increase an improvement score.

## Judge references retain their scope

Add executable `reference_result_ids` from `aut improve validate` when available.
Map their case IDs to task families too. Assessment separates false alarms, misses, abstentions
and missing observations for each rubric. It checks source, content and declared-family overlap.

Executable references establish only tested facts. Model agreement is not a reference label.
Neither establishes general real-work accuracy or human calibration. Ask a human only when a
required fact, authority or preference cannot be resolved from the available evidence.

## Check fresh work

Prepare a separate trusted trial and new evidence cases after freezing the study policy:

```json
{
  "version": 1,
  "trial_spec_id": "FRESH_TRIAL_SPEC_ID",
  "evidence_cases": ["FRESH_CASE_ID"],
  "task_families": {"FRESH_CASE_ID": "independent-family"},
  "trial_case_evidence": {"TRIAL_CASE_NAME": ["FRESH_CASE_ID"]},
  "producer_id": "local-producer",
  "project_scope": "allowed-project",
  "provenance": {
    "setup": null,
    "model": null,
    "tools": null,
    "instructions": null
  }
}
```

Every trial case needs an evidence mapping. Every declared evidence case must be used.
These associations and task-family names are explicit operator declarations, not independently
attested semantic links. Observed event times must follow policy creation for temporal verification.
Reimporting old events with a new case ID does not prove fresh work.

```bash
aut improve study followup --study STUDY_ID --spec-file /private/followup.json
aut improve study followup --study STUDY_ID --spec-file /private/followup.json --execute
aut improve study assess --study STUDY_ID
```

Preview validates inputs without executing commands. Both command checks and agent tasks are supported.
Protected inputs remain outside condition roots. Both fixture maps must pin the proposal target.
Existing failed results remain visible and authorized interrupted work can resume.

Overall scoped support needs a supported original trial and a supported, temporally fresh follow-up
with disjoint declared families and a complete case mapping. Overlapping-family runs remain inspectable
but do not establish independent follow-up support. Code correctness, bounded agent-task observations,
general productivity and loaded instruction exposure remain separate claims.

## Prepare a reviewed transfer

Transfer requires explicit consent and an independent producer. A private request contains:

```json
{
  "version": 1,
  "participant_id": "consenting-participant",
  "producer_id": "independent-reviewer",
  "project_scope": "allowed-project",
  "consent": {
    "participant_id": "consenting-participant",
    "project_scope": "allowed-project",
    "status": "granted"
  },
  "outcome_ids": ["FOLLOWUP_ID"],
  "reviewed_outcome_ids": ["FOLLOWUP_ID"]
}
```

```bash
aut improve study transfer --study STUDY_ID --spec-file /private/transfer.json
```

Consent must match the participant and project. Unknown or revoked consent fails validation.
Only explicitly reviewed outcomes from a different producer can enter the minimal transfer record.
The result excludes trace text, command arguments, captured outputs and provenance prose.
This writes a local record; it does not send anything. Participant identity and an actual two-person
pilot remain unverified until independently observed outside these declarations.
