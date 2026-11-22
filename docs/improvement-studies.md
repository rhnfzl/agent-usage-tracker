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
It counts occurrences and distinct source sessions for each error signal.
Errors with empty output remain visible as individual incidents.
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

To freeze a new trial with the policy in one operation, omit `trial_spec_id` from the policy.
Pass the trusted trial file separately. Review the frozen inputs before you execute.

```sh
aut improve study create --spec-file /private/study.json --trial-spec-file /private/trial.json
aut improve study run --study STUDY_ID
aut improve study run --study STUDY_ID --execute
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

Legacy records without usable source attribution remain counted and unknown when relevant.
Unrelated old cases do not block a study with no requested reference results.
Missing attribution cannot establish holdout independence; accessed records still pass integrity checks.

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

## Measure ordinary work after adoption

If you want a comparison from ordinary sessions, add an observation policy when you create the
study. Freeze the baseline cases first. Keep them separate from discovery and control cases.

Use `ordinary-work` to find measurable attempts in a private audit. The CLI and MCP use the same
coverage checks and return the prompt and completed-turn references.

```bash
aut evidence --audit AUDIT_ID --content redacted ordinary-work \
  --metric tool_errors --coverage-basis tool_events --require-turn-context
aut evidence --audit AUDIT_ID --content redacted freeze-case SESSION_ID EVENT_ID
```

Review the task family before freezing a case. The preview reports distinct sessions and coverage
exclusions for each page. Add `--include-excluded` to see individual reasons.
For incomplete T3 captures, the preview reports available projection gap counts by item kind.
The counts identify missing evidence without exposing the item payloads.
If the item capture limit omits rows, AUT reports how many belong to each affected run.
Other runs can still qualify when their tool evidence is complete.
Follow `next_cursor` to continue the scan. Use `--before` with a timezone to exclude later work.
The study checks source reuse, project and model compatibility, and any required setup evidence.

Use the private study preflight to apply the proposed baseline rules before you freeze cases.
It reserves discovery, control, and proposal-support evidence across recaptures and native mirrors.

```bash
aut improve study preflight --from-study STUDY_ID --audit AUDIT_ID \
  --metric tool_errors --coverage-basis tool_events --require-turn-context --include-excluded
```

For a new study, use `--scope-file` with a JSON object containing `proposal_id`,
`discovery_cases`, and `control_cases`. Set the same metric and setup requirements you plan
to freeze. Preflight counts independent choices in preview order for the current page.
Select comparable task families yourself, then freeze the cases you want to use.

```json
"observation_policy": {
  "metric": "tool_errors",
  "minimum_pairs": 3,
  "minimum_improvement": 0,
  "require_turn_context": true,
  "coverage_basis": "tool_events",
  "baseline_cases": {
    "FIRST_BASELINE_CASE_ID": "fresh worktree setup",
    "SECOND_BASELINE_CASE_ID": "fresh worktree setup",
    "THIRD_BASELINE_CASE_ID": "fresh worktree setup"
  }
}
```

Use `tool_errors`, `tool_calls`, or `identical_tool_retries`. The threshold is a mean reduction per
eligible pair. Record the
adoption with `aut improve adopt`. Then capture fresh ordinary work and pair attempts with the
same task family and matching recorded project, harness, and model labels. Use independent sessions for every case.

```json
{
  "adoption_id": "ADOPTION_ID",
  "pairs": [
    {"baseline": "BASELINE_CASE_ID", "candidate": "FRESH_CASE_ID", "family": "fresh worktree setup"}
  ]
}
```

```sh
aut improve study observe --study STUDY_ID --spec-file paired-observations.json --preview
aut improve study observe --study STUDY_ID --spec-file paired-observations.json
aut improve study assess --study STUDY_ID
```

Preview checks the frozen policy and pair evidence without saving an observation.

The comparison excludes incomplete cases, repeated evidence, mismatched setups, and cases outside
the time window. Candidates must come from a UTC day after the recorded adoption date.
Unpaired baselines, too few eligible pairs, or a mean reduction within the threshold leave the result
inconclusive. The result reports a decrease or increase in the measured count.
The controlled trial keeps its own result.

### Measure repeated requests after errors

Use `identical_tool_retries` to count identical requests repeated after a known tool error
in the same captured attempt. Select this metric in `ordinary-work`, study preflight, and the
frozen observation policy. AUT requires complete tool-event coverage, known result statuses,
and usable request fingerprints. A missing fingerprint or clipped request makes the count unknown.
An attempt with enough evidence and zero matching retries is a valid zero.
If an older snapshot lacks the tool-event verification marker, capture the source again with
the current AUT before choosing study cases.

Provide `baseline_reviews` when freezing a retry study. An operator must accept candidate
attempts using the same criterion. This keeps an attempt that stopped before finishing the task
from being counted as a better outcome. Changed-argument matches remain available for investigation.

### Require reviewed task acceptance

Use one acceptance criterion per task family. Review each frozen baseline case against that
criterion. Cite the captured reply or result that supports your review.

```sh
aut improve study review-case --case BASELINE_CASE_ID --family "fresh worktree setup" \
  --criterion "The requested checks finish and the report identifies each result." \
  --result accepted --cite RESULT_EVENT_ID
```

Add `baseline_reviews` to the observation policy. Map each baseline case ID to its accepted
review ID before you freeze the study. Use the same wording to review each fresh candidate.
Include the returned ID as `candidate_review_id` in its observation pair.

A missing, rejected, or unknown candidate review leaves the comparison inconclusive. The report
retains the review IDs and outcome counts. Review records capture the operator's decision against
the stated criterion. This step makes no model calls.

Set `require_turn_context` to require an observed model and workspace from both attempts.
AUT can link those fields to completed turns in supported native Codex logs.
T3 runs can use that context when at least two strong item references match one completed native
Codex turn. Include the native snapshot when exporting a bundle that needs to show this context.
Missing or conflicting records and attempts spanning several native turns exclude the pair.
Study creation requires enough eligible baseline cases before adoption.
Legacy cases can still use session labels when the policy allows them.
The report counts the pairs that used observed turn context separately.
Submit an empty `pairs` list to record an inconclusive result while eligible follow-up is pending.

Choose `coverage_basis: "tool_events"` before adoption to measure counts from complete tool events.
Freeze cases from a capture that checks for tool record gaps. AUT requires the prompt, a completed
boundary, timestamps, and one result per call. Clipped text and events omitted from the review
sample can still provide count evidence. Compaction, ambiguous tool identities, and mixed runs
exclude a pair. A Codex turn that mixes model calls with execution items can count an action twice.
AUT excludes those turns and pairs where the two attempts use different known representations.
The report counts the pairs that qualify through tool events alone.
The default `full_case` basis also requires a complete review sample.

Choose `metric: "codex_model_calls"` with `require_turn_context: true` to count Codex function
and custom tool requests. Omit `coverage_basis` for this metric. AUT counts raw requests from
the native turn start, and requires one matching output of the same type for each request.
Execution records, built-in search, and other tool types are outside this metric.
Inspect the count and start time on the boundary event through the CLI or MCP.
The study checks coverage in an interactive native Codex session. T3 can use that count
when the audit includes its corroborated native source. AUT requires one opening request,
matching prompt evidence, and native timestamps within the T3 run. Later steering makes
the count unavailable in both native Codex and T3 attempts. The preview and study cite the native boundary
that supplied each count. The observation window includes the opening time from both sources.

Set `require_setup_revision: true` with `require_turn_context: true` to verify the proposed file
in the recorded Git revisions. The study requires the original file in each baseline revision.
It requires the approved file in both the adoption commit and each candidate revision.
For a new file, the baseline revision must show that the path is absent.
Git objects must be available locally. Missing revisions and different contents exclude the pair.

AUT can use a native Codex session header for the first observed turn in the same workspace.
To measure later turns, opt in to recording a Git revision at prompt submission. Add a command
handler to Codex's `UserPromptSubmit` hooks, then review and trust it in Codex. Use absolute
paths for `aut`, the store, and the project. Use the same store for the hook and the evidence
capture. [Codex hook configuration](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/aut evidence --root /path/to/private-store record-codex-turn --project /path/to/repo",
            "timeout": 12
          }
        ]
      }
    ]
  }
}
```

The hook records the Git revision, time, and workspace fingerprint for that native turn.
It works from the project and its linked Git worktrees. The capture checks the session, turn,
workspace, and timing before attaching the receipt to a completed attempt. You can inspect it
on the boundary event with full or redacted access. T3 can use the receipt when its run matches
the native Codex turn. Earlier attempts keep the evidence available when they were captured.

A verified revision describes the committed file. To also check the file in the actual worktree,
add `--setup-file .agents/skills/example/SKILL.md` to the hook command. Use paths relative to
`--project`. Repeat the option for up to 16 paths. AUT records only digests or confirmed absence.
Symlinks, unavailable files, and files above 1 MiB leave the setup unknown.

Use `--require-worktree-setup` in study preflight. Set `require_worktree_setup: true` and
`require_turn_context: true` in the frozen observation policy. AUT checks the original file on
the baseline side and the approved file on the candidate side. A new file must be absent at
baseline. A working copy with different bytes leaves the pair ineligible, even when the
commit is correct. The report counts pairs with verified worktree setup.

Set `require_approved_file_read: true` with `require_turn_context: true` to require evidence
that the candidate received the approved file contents. AUT recognizes a successful native
Codex `cat` of a single literal path when the command result belongs to the same recorded turn.
It compares the path and returned bytes with the proposed file. T3 can use the same evidence
from a corroborated native turn. The report saves the matching call and result references.
Partial reads, missing receipts, and changed bytes leave the pair unverified.
This check records the content the tool returned. Following the instructions and task quality
remain separate questions.
