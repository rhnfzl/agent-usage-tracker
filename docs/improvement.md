# Private local improvement

`aut improve` finds evidence of agent problems and tests explicit setup changes.
It keeps evidence, model estimates, proposals, and trial results in a private local store.
Normal collection and the configured sink remain separate.

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
The audit filters event timestamps, matches calls to results, and records source hashes.
`--project /path/to/repo` restricts an audit to a readable repository and its worktrees.

Each source read stops at 16 MiB. Captured events are redacted and limited to 6,000 characters.
Coverage exposes incomplete lines, clipped events, missing results, and copied history.
Completeness describes captured records, not proof of the model's full context.
Pattern-based redaction can leave private code, paths, names, and credentials.
Audit output contains counts and identifiers, without prompt or tool text.

Content-addressed records are immutable. A changed record fails its hash check rather than being reset.
`events.jsonl` preserves progress and decisions.

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

Separate criteria cover avoidable recovery and completion supported by evidence.
Cases mix failed and clean user attempts. Each attempt ends at a recorded turn boundary or the next prompt.
Sampling prefers complete captured attempts within each group. Audits retain all coverage gaps.
Complete attempts retain up to 40 events and 24,000 text characters.
Clipped, unmatched, ambiguous, or omitted attempt evidence stays unknown before inference.
Clipping in an unrelated earlier attempt does not invalidate a complete later attempt.
This sample does not estimate population prevalence.
Votes resume after interruption. Unchanged cases reuse judgments across later audits.

Add a cached instruct model to review flagged and eligible uncertain cases:

```bash
aut improve diagnose --audit AUDIT_ID \
  --explain-model mlx-community/Qwen3-4B-Instruct-2507-4bit
```

Its output must use the expected schema and cite existing event IDs.
It cannot supply executable commands or change files.
Incomplete completion evidence remains unknown.
All model results are provisional estimates, including agreement between models.
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

## Run a bounded cycle

```bash
aut improve run --days 7 --limit 30 --case-limit 12 --budget-s 300 --max-calls 40
```

This freezes evidence and resumes diagnosis in a subprocess with a hard deadline.
Overlapping cycles return `busy`. Contention, exhausted budgets, and failures remain visible.
An interrupted or budget-limited cycle resumes its frozen audit before collecting another batch.
The process writes `last_cycle.json` and refreshes `inbox.html`.
It does not apply proposals, execute trial commands, or upload evidence.

Schedule the command with an existing operating-system timer.
Use an absolute executable and explicit private store path.
Keep budgets small until review quality and operating costs are measured.
Evidence remains until deliberately removed. Configure backups and retention separately.
