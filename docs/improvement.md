# Private local improvement

`aut improve` finds evidence of agent problems and tests explicit setup changes.
It keeps evidence, model estimates, proposals, and trial results in a private local store.
Normal collection and the configured sink remain separate.

[Private studies](improvement-studies.md) connect incidents, setup provenance, prospective trial
policies, independent references and fresh follow-up. They report bounded supported, rejected or
inconclusive effects without turning model estimates into general productivity claims.

## Freeze evidence

```bash
aut improve audit --days 7 --limit 30
aut improve status
```

The default store is `$XDG_DATA_HOME/aut/improve`, or `~/.local/share/aut/improve`.
Set `AUT_IMPROVE_ROOT`, or pass `aut improve --root /private/path ...`.
Use a new empty directory. Existing stores require their `.aut-improve` version marker.
Files are private, and the store's `.gitignore` excludes all contents. Never force-add them to Git.

The adapters read Claude Code and Codex logs and T3 Code's local
`~/.t3/userdata/statev2.sqlite` projections. T3 is read in a read-only SQLite transaction,
including committed WAL records. Requests explicitly created by a user in the web or mobile
client qualify as human work. Provider-imported history, agent messages and unknown origins
do not become human requests merely because they appear in T3.
T3 app threads retain provider and run identifiers across Claude/Codex switches.
Included T3 app threads group their linked native sources as frozen supplements. Native-only
history remains inspectable without becoming a second independent judge observation.
A session link alone does not prove equivalent coverage or identical execution records.
Projection gaps, missing outputs, attachments, handoffs and clipped records remain explicit;
they cannot establish complete evidence for an affected attempt. Unsupported databases are
reported as unreadable sources rather than as an absence of work.
T3 source hashes cover selected logical records, not the changing SQLite database file.
Database record identifiers replace JSONL line references for those events.
The T3 reader does not prove that project instructions reached a past agent turn.
Repository instructions apply to their project; an AUT-specific test rule is not copied to
other projects with different test commands.
Native discovery uses the shared source resolver for collection, local labels and private inspection.
It includes registered Claude/Codex/Pi homes, live Claude/Codex home overrides and explicit T3 Claude profiles.
See [shared harness coverage](harness-coverage.md) for the registry and read-only doctor command.
These source associations do not establish human input or complete task evidence.
They omit positively identified automated sessions and sources active within the last two minutes.
Codex sources identified as automated by their first session header skip body processing.
Unknown sources retain the full evidence checks.
The audit filters event timestamps, matches calls to results, and records source hashes.
`--project /path/to/repo` restricts an audit to a readable repository and its worktrees.

Each source read uses at most 16 MiB. Large sources retain their first metadata record and a
line-aligned recent window. Source byte ranges and omitted prefixes remain explicit.
Unknown fork ordinals cannot establish whether tail records are copied history, so those tails
remain excluded. A complete observed attempt can be reviewed even when its whole session is incomplete.
Captured events are redacted and limited to 6,000 characters.
Coverage exposes incomplete lines, clipped events, missing results, and copied history.
Codex stores some replies both as native messages with structured memory citations and as
rendered messages with citation text. Equivalent representations share one reply; changed
reply bodies or citation data remain conflicts.
Completeness describes captured records, not proof of the model's full context.
Pattern-based redaction can leave private code, paths, names, and credentials.
Audit output contains counts and identifiers, without prompt or tool text.

Content-addressed records are immutable. A changed record fails its hash check rather than being reset.
`events.jsonl` preserves progress and decisions.

## Inspect captured case evidence

```bash
aut improve inspect --case CASE_ID
```

This writes a private HTML view under the local store and prints its path and counts.
The view shows the matching captured attempt, including context omitted from the model input.
It links the original local trace and frozen snapshot, with line or byte references for each event.
Trace text stays in the private view, outside command output and the configured sink.
Missing and clipped evidence stays visible. Inspection does not make a judgment eligible or verified.
The original link opens the current file; the displayed evidence remains the immutable saved snapshot.

## Review with local models

Install the existing label extra and cache the required weights separately:

```bash
uv pip install 'agent-usage-tracker[label]'
aut improve diagnose --audit AUDIT_ID --limit 12 --budget-s 300 --max-calls 40
```

Defaults use `pplx-decider-flash` and `clef-flash`.
`--models NAME,NAME` selects registered local decision models.
Diagnosis never downloads weights or calls a remote model.
Apple silicon and a working MLX runtime are required.
The process acquires AUT's shared `replay-model.lock` without waiting and checks memory headroom.
Busy inference leaves work deferred.
Both CLI commands, `diagnose` and `run`, enforce a hard subprocess deadline.
The deadline includes startup, evidence collection, model preparation, inference, and saving progress.
The Python `diagnose` function accepts an inherited monotonic deadline and checkpoints completed work.
Callers that use it directly must provide a deadline wrapper when needed.

New model calls need enough remaining time for their expected duration.
Each model and operation starts with an allowance of half the original diagnosis budget.
Later calls reserve twice the longest observed duration for that model and operation during this run.
AUT checks admission again after acquiring shared resources. A deferred call does not spend a call count.
A case too large for the time left stays pending, and AUT still assesses later cases that fit.
The run stops when the time or the call count is spent.
Cached votes, judgments, and reviews remain usable without a new inference allowance.
Rejected explanatory reviews still contribute their measured duration before a repair attempt.
This estimate does not guarantee preemption of an unusually slow native call.
A long audit can leave too little time for a first call; its frozen evidence resumes later.

Matching checkpoints record `unfinished_call` before an admitted model operation.
It names the model revision, case, criterion and operation without a durable result.
It does not prove native execution started. Saving a vote or review attempt clears it.
A killed worker retains the descriptor so its failed operation can be inspected.
A model load is checkpointed the same way, with `phase: load` and `first_call_at`, and its
`model_call` row is written before any inference, so a deadline during either keeps both.

After stopping a command, process reaping and output-reader cleanup share one second.
Incomplete cleanup returns a failure with `cleanup_timeout` and the owned `pid`.
An unknown exit status is never accepted. An OS-stuck child can still retain model locks.
Completed process startup counts against the command deadline; an unreturning spawn remains OS-dependent.

Each criterion is a markdown rubric (see [Rubrics](#rubrics)). Two are blocking: avoidable
recovery and completion supported by evidence.
Cases mix failed and clean user attempts. Each attempt ends at a recorded turn boundary or the next prompt.
Sampling prefers complete captured attempts within each group. Each source checks at most
20 recent completed turns to select a clean control that fits the existing event and text budgets.
If none fits, the latest available clean control retains its explicit gaps. Audits retain all coverage gaps.
Complete attempts retain up to 40 events and 24,000 text characters.
Clipped, unmatched, ambiguous, or omitted attempt evidence stays unknown before inference.
Clipping in an unrelated earlier attempt does not invalidate a complete later attempt.
This sample does not estimate population prevalence.
Votes, completed judgments, and completed explanatory reviews resume after interruption.
Unchanged cases reuse judgments across later audits. Model preparation resolves the actual
model revision before AUT constructs a cache identity.

Add a cached instruct model to verify unanimous failure votes:

```bash
aut improve diagnose --audit AUDIT_ID \
  --explain-model mlx-community/Qwen3-4B-Instruct-2507-4bit
```

Its output must use the expected schema and cite existing event IDs.
It cannot supply executable commands or change files.
Incomplete completion evidence remains unknown.
Choice descriptions state what each label means for its criterion. Their exact text is
part of that rubric's evaluator identity, so a changed description cannot reuse its earlier votes.
All model results are provisional estimates, including agreement between models.
An explanatory model can veto a decision, but cannot upgrade model disagreement into pass or fail.
Disagreements stay unknown without spending an explanatory-model call.
Reviews over the rubric's `max_input_chars` (6,000 by default) serialized case characters stay
unknown with `review_input_limit` recorded.
Invalid reviews retain their bounded, redacted responses in private `review_attempts` records.
One schema repair is allowed. Both attempts count against the model-call and time budgets.
They are not human labels or calibrated accuracy measurements.
Ask a human only when evidence cannot settle a required fact, authority, or preference.
The private `inbox.html` shows escaped review text and case identifiers.

## Rubrics

Each criterion is a markdown file. Built-ins ship in
`src/agent_usage_tracker/improve/rubrics/`: `avoidable_recovery` and `completion_supported`
(blocking, moved from code with their exact text), and `verbosity`, `redundant_tests` and
`code_quality` (non-blocking). `code_quality` needs commit outputs, which no case has yet, so it
never runs.

Your own files go in `rubrics/` next to the config file
(`~/.config/agent-usage-tracker/rubrics/*.md`). A file with the same `id` replaces the built-in, and keeps the built-in's `blocking` unless the
file sets it.
A file with only `id: NAME` and `enabled: false` in its frontmatter switches that rubric off.
A broken file stops the diagnosis with its file name; it is never skipped silently, so keep
notes and other markdown out of that folder.

```markdown
---
id: verbosity
version: 1
threshold: 0.7
priority: 30
blocking: false
applies_to: {"triggers": ["error_result", "control"], "requires": ["final_reply"]}
max_cases: 4
checks: {"fail_must_cite": ["reply"]}
---
## Question
Is the assistant's final reply no longer than the result needs?
## Definition
What each label means, as one paragraph.
## Labels
- pass: ...
- fail: ...
- unknown: ...
```

Frontmatter is `key: value` lines. A value that starts with `[`, `{`, a digit, `true` or `false`
is JSON; anything else is a string. Wrapped lines under a heading or label join with one space.

| Field | Default | Meaning |
|---|---|---|
| `id`, `version` | required | Identity. Bump `version` on any change of meaning. |
| `threshold` | 0.7 | Minimum model probability for a pass or fail vote, above 0.5 and at most 1. |
| `priority` | 100 | Lower runs first. |
| `blocking` | false | `true`: the diagnosis is not complete until this rubric is judged on all its cases. `false`: best effort; it uses only time and calls left after the blocking rubrics. |
| `applies_to.triggers` | both | `error_result`, `control`, `merge` (no case has `merge` yet). |
| `applies_to.requires` | none | `final_reply` (a reply event), `test_command` (a call whose command family is a test runner such as `pytest`, `python -m pytest`, `npm test`), `outputs` (commit outputs; none yet). |
| `max_cases` | 200 | Cases per diagnosis for this rubric. |
| `max_input_chars` | 6000 | Review size cap, at most 24,000. |
| `input` | `attempt` | `outputs_summary` is accepted only with `requires: ["outputs"]`. |
| `checks.fail_must_cite` | none | Event kinds a fail review must cite. |
| `checks.fail_cause_not_in` | none | Causes a fail review may not give. |
| `checks.unknown_unless_complete` | false | Force unknown on incomplete attempts. |
| `qualify` | none | Recorded for later human-label qualification. |

AUT refuses a file with a missing heading, a label set that is not exactly pass, fail and unknown,
an unknown field, `requires` value or check, or a value out of range.

Each diagnosis saves every rubric as a private `rubric_snapshots` record and names the snapshots
in its policy. Each rubric has its own evaluator identity, so editing one rubric file re-votes
only that rubric; the other rubrics keep their votes and judgments. Any byte change in a file
counts, even without a version bump. Each judgment records `rubric_version`, `rubric_sha256` and
`rubric_snapshot`. Judgments made before markdown rubrics have none of these. Their diagnosis
policy holds the old rubric text, and `rubric.legacy_version` maps it to version 1 when it equals
the text the v1 files were moved from, else 0. Validation of an older diagnosis uses its old
identity, so its result does not change. Because the rubric identity changed, the first night
or two after the update re-vote the pending audit's cases once: under a tight call budget the
blocking re-votes alone can fill the first night, and the next night finishes from the saved
votes. Older judgments outside the pending audit are re-voted once, when a later audit picks
their cases up again.

The diagnosis judges rubric by rubric: blocking rubrics first, each group in priority order, so
the others get only what is left. Before markdown rubrics it went case by case; after a cut-off
night, the first rubric may now have more cases judged than the second. `complete` counts
blocking work only, so with no blocking rubric enabled it is complete at once. `finished` and
`total` count blocking judgments; `optional_finished` and `optional_total` count the rest;
`verdicts` counts both. `reason` is `budget` when any work, blocking or not, was left.
An error during non-blocking work, such as low memory or an invalid model distribution, stops
the non-blocking rubrics without failing the diagnosis: `reason` is `optional_error` (or
`budget` when blocking work was also left) and `optional_skipped` names the rubric and the
error type. An error during blocking work still
fails the run. The scheduled worker releases its pending audit at the first
checkpoint where blocking work is complete, so a slow non-blocking rubric never holds the next
audit, even if the run is cut off later. When the run ends it writes the released state once
more, so an `audit --cycle` that started during the non-blocking work returns `superseded`.

## Prepare a scoped change

Create text files with the exact old and new spans:

```bash
aut improve propose --case CASE_ID --scope /path/to/repo \
  --target /path/to/repo/instructions.txt \
  --before-file /private/old-span.txt --after-file /private/new-span.txt
```

The old span must match exactly once. The target must remain inside the explicit scope.
Drafting saves original content, candidate content, diff, evidence, and rollback instructions.
It does not modify the source file.
Saved and staged content preserves exact UTF-8 newline bytes, including CRLF.
Add `--candidate /path/to/isolated/worktree` to stage there.
Staging rejects a changed source base, path escapes, and unrelated candidate edits.
It cannot stage into the original scope itself.
The operator supplies the target and replacement. Historical text and model output cannot authorise writes.

## Compare baseline and candidate

A trusted specification uses argv arrays and independent working directories:

```json
{
  "version": 1,
  "kind": "command_check",
  "directories": {
    "baseline": "/path/to/baseline",
    "candidate": "/path/to/candidate"
  },
  "cases": [{
    "id": "acceptance",
    "argv": ["/path/to/python", "-m", "pytest", "-q"],
    "timeout_s": 60
  }],
  "repeats": 3,
  "seed": 7
}
```

Optional `files.baseline` and `files.candidate` map relative paths to expected SHA-256 hashes.
The runner checks them before execution or resume. Other inputs are not frozen automatically.

```bash
aut improve trial --spec-file /private/trial.json
aut improve trial --spec TRIAL_SPEC_ID --execute --budget-s 600
```

Execution requires `--execute`. Treat the specification as executable operator input.
The runner does not invoke a shell implicitly. Explicit commands can still modify files or use the network.
Use disposable worktrees and safe acceptance commands.

Condition order varies reproducibly by case and repeat.
Every result remains available. Completed trials are not rerun on resume.
Commands have time limits and a 256 KiB captured-output limit.
Timed-out or output-limited commands cannot count as accepted.
Trial environments exclude inherited secrets and use the condition's `src` directory for `PYTHONPATH`.
Command checks do not establish an agent behaviour or productivity improvement.
Those claims need controlled agent tasks, untouched evaluation cases, repeated trials, and fresh follow-up work.

### Check a frozen search task

Trusted Python acceptance commands can use `improve.search_check.check_search_stream`.
Pass the captured native JSONL stream, a `SearchTask`, and the unchanged fixture directory.
The task supplies the full file inventory, contents, search scope, symbol, status, and expected hits.
AUT validates that ground truth against the files before checking the answer.
Logical file names and scope use relative forward-slash paths on every platform.
Frozen contents preserve exact UTF-8 bytes, including line endings.

The checker reports answer correctness separately from evidence completeness.
It accepts supported read-only searches, compound scope checks, and full native file reads.
It never executes commands from a model's trace. Unsupported shell syntax, uncertain shell
state, missing results, and incomplete scope evidence remain unknown.
An independently supported answer can remain correct when its procedure cannot be certified.
Fixtures with changed contents, symbolic links, or special files cannot receive acceptance.

This is a finite checker for frozen Python-symbol search fixtures, not a general task judge.
Do not use its acceptance as proof that a remote action occurred.
Freeze acceptance code and independent controls before an experiment.
Later checker improvements produce a new diagnostic evaluation, not a rewrite of earlier outcomes.

### Agent tasks

Set `kind` to `agent_task` for a trial with independent acceptance checks.
Provide a nonempty `files` hash map for both conditions. Each execution gets a fresh copy of only those files.
Each case also needs `acceptance_argv` and `acceptance_files`.
The latter maps absolute acceptance-script and input paths to their SHA-256 hashes.
Keep acceptance code outside both templates and the agent's writable workspace.
The runner checks these hashes before and after acceptance execution.
Acceptance runs outside the writable workspace without its Python import path.
Use `{workspace}` as a complete acceptance argv element to pass the workspace path.
Malformed metrics and acceptance failures remain terminal trial results on resume.

The trusted agent runner receives `{metrics_file}` as one complete argv element.
This path sits outside the agent workspace. The runner writes a JSON object there:

```json
{"tool_calls": 3, "interventions": 0}
```

Capture these values from the agent runner. Do not ask the agent to grade its own activity.
Command success alone cannot pass a task. Independent acceptance and valid metrics must also succeed.
Results retain each workspace, execution, acceptance check, elapsed time, and metrics.
Condition summaries include failed runs and missing metrics. They do not establish an improvement automatically.

## Validate the judges

Use executable facts where possible. Freeze reference evidence and oracle inputs before observing model results.
A reference specification contains `version: 1`, `split` (`development` or `holdout`), and `references`.
Each reference has `evidence_sha256`, `rubric`, and `expected` (`pass`, `fail`, or `unknown`).
The evidence digest covers the case's events, coverage, and omitted-event count using AUT's canonical JSON.
Provide `oracle_argv`, absolute `oracle_cwd`, `oracle_files` (absolute path/hash map), and `timeout_s`.
Include `{reference_input}` as a complete oracle argv element.
The runner supplies an immutable JSON file containing evidence and rubric, without expected labels.
The trusted oracle checks this evidence independently and returns a `references` array.
Each result includes `rubric`, `expected`, and `evidence_sha256_parts` (four 16-character strings).
The digest parts preserve exact identity through text redaction. Their order and labels must match the frozen references.
Oracle inputs and the supplied evidence are checked again after execution.

```bash
aut improve validate --spec-file /private/references.json --diagnosis DIAGNOSIS_ID
aut improve validate --spec-file /private/references.json --diagnosis DIAGNOSIS_ID --execute
```

Execution requires explicit opt-in because oracle commands are executable operator input.
Reports separate detected failures, missed failures, false alarms, abstentions, and missing judgments.
Abstaining on a known failure counts as a missed failure. Missing judgments prevent rate calculations.
Synthetic reference results do not measure real-work accuracy or grant patch authority.
The split label does not prove independence. Any suite used to tune prompts becomes development data.

## Run a bounded cycle

```bash
aut improve run --days 7 --limit 30 --case-limit 12 --budget-s 300 --max-calls 40
```

This freezes evidence and resumes diagnosis in a subprocess with a hard deadline.
One inherited deadline covers the whole cycle. AUT reserves time to save its result.
On supported POSIX workers, a cooperative alarm interrupts unfinished model work before
the outer deadline. A native call that delays Python signal handling can still time out.
Overlapping cycles return `busy`. Contention, exhausted budgets, and failures remain visible.
Both `run` and `diagnose` exit 1 on a hard timeout or worker error, after printing their JSON result.
Completed cycles, cooperative budget deferrals, and overlapping cycles exit 0.
Failed workers retain bounded, redacted diagnostics in private `cycle_errors` records.
Matching worker checkpoints remain available when the outer deadline kills the worker.
Results from an earlier worker or another policy cannot count as the current result.
The command returns their identifiers without printing captured trace text.
An interrupted or budget-limited cycle resumes its frozen audit before collecting another batch.
The process writes `last_cycle.json` and refreshes `inbox.html`.
It does not apply proposals, execute trial commands, or upload evidence.

A pending frozen audit is reused while its own `--days` and `--limit` match the run.
Changing `--budget-s` or `--max-calls` keeps it. Changing days or limit builds a new audit.

To keep the audit out of the judge's budget, freeze it in a separate step first:

```bash
aut improve audit --cycle --days 7 --limit 30 --budget-s 1500
```

It returns `frozen`, `kept` (a matching audit is already pending), `busy`, `budget`, or
`superseded` (a cycle finished while it built, so it queues nothing; this includes a cycle
that released its audit early and was still judging non-blocking rubrics when the build began).
Only `budget` exits 1, and it leaves the pending audit unchanged.
`--cycle` takes no `--project`. Plain `aut improve audit` is unchanged and writes no pending audit.
`packaging/launchd/dev.aut.improve-audit.plist` is a template that runs this step at 03:50.

When that step did not run, `aut improve run --audit-budget-s 600` builds the audit under its
own 600-second cap and then gives the judge its full `--budget-s`. The outer timeout grows by the
same amount only on those nights. The default 0 keeps the audit inside the run's budget.
The worker report adds `worker_started_at` and `audit_s` (0 when the audit was reused).

Schedule the command with an existing operating-system timer.
Use an absolute executable and explicit private store path.
Keep budgets small until review quality and operating costs are measured.
Evidence remains until deliberately removed. Configure backups and retention separately.

### Install a stable runtime

Build a wheel, then install it outside the development checkout using cached dependencies:

```bash
uv build --wheel
python -m agent_usage_tracker.improve.runtime \
  --wheel dist/agent_usage_tracker-0.1.0-py3-none-any.whl \
  --store "$HOME/.local/share/aut/improve" \
  --runtime-root "$HOME/.local/share/aut/improve-runtime"
```

Use `--constraints /private/requirements.lock` to keep dependency versions fixed across builds.
The installer excludes the old project-wheel reference from dependency constraints.
It verifies the installed module and wheel hash using Python's isolated mode.
It saves the resolved dependency lock and refuses unmanaged or changed existing runtimes.
Installation is offline and leaves older releases available for rollback.

Point the operating-system timer at the returned `python` path with `-I -m agent_usage_tracker.cli`.
Use the private store as its working directory. The worker no longer depends on a source-checkout path.
Verify an installed run before changing the timer. Preserve the previous timer configuration for rollback.
Local MLX inference still requires Apple silicon. Base commands need the existing cross-platform CI matrix before broader distribution.
