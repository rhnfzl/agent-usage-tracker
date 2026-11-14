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

The initial adapters read Claude Code and Codex logs.
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
The Python `diagnose` function checks elapsed time between calls; callers must provide a deadline wrapper when needed.

Separate criteria cover avoidable recovery and completion supported by evidence.
Cases mix failed and clean user attempts. Each attempt ends at a recorded turn boundary or the next prompt.
Sampling prefers complete captured attempts within each group. Each source checks at most
20 recent completed turns to select a clean control that fits the existing event and text budgets.
If none fits, the latest available clean control retains its explicit gaps. Audits retain all coverage gaps.
Complete attempts retain up to 40 events and 24,000 text characters.
Clipped, unmatched, ambiguous, or omitted attempt evidence stays unknown before inference.
Clipping in an unrelated earlier attempt does not invalidate a complete later attempt.
This sample does not estimate population prevalence.
Votes resume after interruption. Unchanged cases reuse judgments across later audits.

Add a cached instruct model to verify unanimous failure votes:

```bash
aut improve diagnose --audit AUDIT_ID \
  --explain-model mlx-community/Qwen3-4B-Instruct-2507-4bit
```

Its output must use the expected schema and cite existing event IDs.
It cannot supply executable commands or change files.
Incomplete completion evidence remains unknown.
Choice descriptions state what each label means for its criterion. Their exact text is
part of evaluator identity, so a changed description cannot reuse earlier votes.
All model results are provisional estimates, including agreement between models.
An explanatory model can veto a decision, but cannot upgrade model disagreement into pass or fail.
Disagreements stay unknown without spending an explanatory-model call.
Reviews over 6,000 serialized case characters stay unknown with `review_input_limit` recorded.
Invalid reviews retain their bounded, redacted responses in private `review_attempts` records.
One schema repair is allowed. Both attempts count against the model-call and time budgets.
They are not human labels or calibrated accuracy measurements.
Ask a human only when evidence cannot settle a required fact, authority, or preference.
The private `inbox.html` shows escaped review text and case identifiers.

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
Overlapping cycles return `busy`. Contention, exhausted budgets, and failures remain visible.
Both `run` and `diagnose` exit 1 on a hard timeout or worker error, after printing their JSON result.
Completed cycles, cooperative budget deferrals, and overlapping cycles exit 0.
Failed workers retain bounded, redacted diagnostics in private `cycle_errors` records.
The command returns their identifiers without printing captured trace text.
An interrupted or budget-limited cycle resumes its frozen audit before collecting another batch.
The process writes `last_cycle.json` and refreshes `inbox.html`.
It does not apply proposals, execute trial commands, or upload evidence.

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
