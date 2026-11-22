# Private local improvement

`aut improve` finds evidence of agent problems and tests explicit setup changes.
It keeps evidence, model estimates, proposals, and trial results in a private local store.
Normal collection and the configured sink remain separate. The optional `aut publish-improve`
command sends advisory counts and operator-written adoption markers to the private sink.

[Private studies](improvement-studies.md) connect incidents, setup provenance, prospective trial
policies, independent references and fresh follow-up. They report bounded supported, rejected or
inconclusive effects without turning model estimates into general productivity claims.

For interactive analysis from a coding agent, use the [CLI and MCP evidence workflow](evidence.md).
It can freeze the specific attempt around a selected event for a proposal or study.

## Freeze evidence

```bash
aut improve audit --days 7 --limit 30
aut improve status
```

The default store is `$XDG_DATA_HOME/aut/improve`, or `~/.local/share/aut/improve`.
Set `AUT_IMPROVE_ROOT`, or pass `aut improve --root /private/path ...`.
Use a new empty directory. Existing stores require their `.aut-improve` version marker.
Files are private, and the store's `.gitignore` excludes all contents. Never force-add them to Git.

The adapters read Claude Code, Codex and Pi logs, Cursor CLI chat stores, and T3 Code's local
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

Each source read uses at most 16 MiB. Large JSONL sources retain their first metadata record and a
line-aligned recent window. Source byte ranges and omitted prefixes remain explicit.
Unknown fork ordinals cannot establish whether tail records are copied history, so those tails
remain excluded. A complete observed attempt can be reviewed even when its whole session is incomplete.
Pi uses its recorded stop reason, omits copied parent entries, and keeps branches with uncertain
order ineligible. Cursor CLI reads the latest root and its linked blobs in one read-only transaction.
It excludes registered automated chats and subagent chats. A later request can close an earlier
request with a final observed reply. The trailing request and roots with missing evidence remain
ineligible. Compacted Cursor history remains ineligible. An attachment omission marks its attempt
incomplete and keeps the attachment bytes out of the saved evidence. Cursor CLI groups events by
the request timestamp and does not infer missing workspace paths.
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
It links the original local trace and frozen snapshot, with line, byte or database record references.
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
The private `inbox.html` shows open proposals with copy-ready accept and reject commands, the
top 10 failure groups with their status (next section), the last cycle's judgments with
escaped review text, rubric version, trigger and case identifiers, and the last 5 adopted fixes.

## Case outputs

Judges can compare a committed change with the captured request and final reply.
This adds private local evidence without changing the case or requiring the sink.

AUT finds commit SHAs only in matched results of real `git commit` calls within that attempt.
Dry runs do not count. It reads up to eight commits from the captured session's absolute working
directory. Each Git read has a five-second limit and a 64 KiB output limit. Each attachment gets
at most ten seconds, and diagnosis allows thirty seconds total within its existing time budget.
Missing repositories and exhausted attachment budgets increment `outputs_skipped`; normal blocking
judgments continue. Partial reads mark the output truncated and preserve a private warning.

Append-only `case_outputs` records contain seven-character SHAs, counts, redacted diff statistics
(800 characters), a redacted patch (1,500 characters), truncation status, PR numbers from matched
`gh pr create` results, and check timestamps. An unchanged attachment reuses its saved record.
Requests pass this text as `facts.untrusted_outputs`, which is evidence, never instructions.
Output rubric cache keys include the attachment identity. Clipped patches or missing prompt/reply
produce unknown without model calls. Oversized output requests also produce unknown with
`review_skipped: review_input_limit`.

States reflect local Git refs only: `merged` means reachable from the remote default ref,
`reverted` means a revert message on that ref names the commit, `open` means a local branch
contains it, and `unreachable` means none of those refs contains the object. AUT does not fetch
or call a provider API. Stale refs can delay detection. Squash merges can remain open until
branch deletion, then appear unreachable. These states do not measure rework, fourteen-day
survival, or causal benefit.

After the worker, the parent refreshes open outputs checked within thirty days, with a thirty-second
total cap. Missing repositories leave the prior snapshot intact and add an `outputs_skipped`
journal row. State changes enter the bounded `merge_queue.json`, including changes first noticed
during attachment. Each diagnosis admits at most four merge cases after its normal and label
work. Each rubric has a separate capped merge sample. These cases use the same model cache,
locks and call/time budgets as labelled extras, and cannot hold normal completion open.
Only rubrics accepting `merge` run on this path. Their judgments record `trigger: merge` and
the output identity, while the original case keeps its trigger. This key remains distinct from
the original judgment, even when both use the same output snapshot. Completed comparisons leave
the queue under a lock. Concurrent additions and newer snapshots remain queued.

Human output labels use the same prompt, final reply and output facts as the judge.
Each label records its output snapshot, and qualification compares it only with judgments for
that snapshot. A newer snapshot makes the case available for another label. A page opened before
that change refuses a stale save. Older labels remain private history. Label thresholds, scoring
and advisory authority stay unchanged. Existing labels without outputs keep their original identity.

## Rubrics

Each criterion is a markdown file. Built-ins ship in
`src/agent_usage_tracker/improve/rubrics/`: `avoidable_recovery` and `completion_supported`
(blocking, moved from code with their exact text), and `verbosity`, `redundant_tests` and
`code_quality` (non-blocking). `code_quality` runs only when the attempt has local commit outputs.

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
| `applies_to.triggers` | both | `error_result`, `control`, `merge` (a queued local commit-state change). |
| `applies_to.requires` | none | `final_reply` (a reply event), `test_command` (a call whose command family is a test runner such as `pytest`, `python -m pytest`, `npm test`), `outputs` (a local commit diff). |
| `max_cases` | 200 | Cap for each sample: normal cases, queued label cases, and queued merge cases. |
| `max_input_chars` | 6000 | Review size cap, at most 24,000. |
| `input` | `attempt` | `outputs_summary` uses the captured prompt, final reply and outputs; it requires `outputs`. |
| `checks.fail_must_cite` | none | Event kinds a fail review must cite. |
| `checks.fail_cause_not_in` | none | Causes a fail review may not give. |
| `checks.unknown_unless_complete` | false | Force unknown on incomplete attempts. |
| `qualify` | none | Human-label policy: `min_fail_labels`, `min_failure_recall`, `max_false_alarm_rate`. |

AUT refuses a file with a missing heading, a label set that is not exactly pass, fail and unknown,
an unknown field, `requires` value or check, or a value out of range.

Each diagnosis saves every rubric as a private `rubric_snapshots` record and names the snapshots
in its policy. Each rubric has its own evaluator identity, so editing one rubric file re-votes
only that rubric; the other rubrics keep their votes and judgments. Any byte change in a file
counts, even without a version bump. Each judgment records `rubric_version`, `rubric_sha256` and
`rubric_snapshot`. Judgments made before markdown rubrics have none of these. Their diagnosis
policy holds the old rubric text, and `rubric.legacy_version` maps it to version 1 when it equals
the text the v1 files were moved from, else 0. Validation of an older diagnosis uses its old
identity, so its result does not change. Readers also retain the original version-1 job-bound
keys, without rewriting saved judgments or treating version-0 history as a current qualification.
Because the rubric identity changed, the first night
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
An error during non-blocking work never fails the diagnosis. An invalid model vote skips that
case only; an error outside a vote, such as low memory, stops the remaining non-blocking
rubrics. `reason` is then `optional_error` (or `budget` when blocking work was also left) and
`optional_skipped` lists each rubric and error type. A skipped case is tried again next night.
An error during blocking work still fails the run.
The scheduled worker releases its pending audit at the first
checkpoint where blocking work is complete, so a slow non-blocking rubric never holds the next
audit, even if the run is cut off later. When the run ends it writes the released state once
more, so an `audit --cycle` that started during the non-blocking work returns `superseded`.

## Group recurring failures

```bash
aut improve groups --days 14 --threshold 3
```

This reads the stored judgments, runs no model, and saves a new report. It groups judgments whose case falls in the
last `--days` local work days (the date of the case's first timestamped event) and that are
`fail`, or `unknown` with at least one model voting `fail` at the rubric threshold (a split
vote). Only the newest judgment per case and rubric counts: the highest `rubric_version`, then
the one the latest diagnosis listed. A diagnosis that was killed journals no list, so a tie
among its judgments falls back to a fixed but arbitrary order. Cases dated after today are
left out.

The group key is a digest of five signals:

| Signal | From |
|---|---|
| `rubric` | the judgment |
| `cause` | the evidence review's cause, else `unreviewed` |
| `tool` | the call answered by the anchor result: the first error result the review cites, else the first result it cites, else the case's last error result. MCP tools show as `mcp:server` |
| `error_class` | the anchor's text, lower-cased, with paths, long hex runs, numbers and quoted strings removed, matched against a fixed table. A JSON-shaped result (Codex command and MCP results, T3 dict outputs) is matched on its error fields (`error`, `stderr`, `message`), else its output fields (`output`, `content`, ...), plus a non-zero `exit_code`, not on the raw JSON. A successful anchor has an empty error class (`command_not_found`, `no_such_file`, `missing_module`, `permission_denied`, `timeout`, `unknown_tool`, `user_declined`, `shell_glob`, `push_rejected`, `conflict`, `syntax_error`, `test_failure`, `nonzero_exit`). The whole text is matched, because Claude Code and Codex print `Exit code 1` first. No match gives `other:` and 8 hex of a digest of the first line |
| `command_family` | the anchor call's command, as for rubric `requires` (for example `git push`, `python -m pytest`, `pytest`) |

The project is left out of the key, so the same failure in three repositories is one group;
`projects` counts its tasks per repository. `distinct_tasks` counts distinct sessions (the
same identity studies use), so one long session counts once. A group is `recurring` at
`--threshold` tasks or more. `audit_incidents` counts the newest scheduled audit's error results
with the same tool, error class and command family, including results no case sampled.
Groups sort by recurring, then tasks, then last work day.

Each run saves a private `group_reports` record and journals `groups`; the command prints the
report as JSON. A record holds ids, signal names and counts only, never evidence text. A tool or
command name that is long or has unusual characters becomes `other`. Records that cannot be
read are counted as `skipped`.

`aut improve run` and `aut improve diagnose` refresh the groups after the worker exits, outside the judge's budget and
on every outcome. A grouping failure journals `groups_error` with the error type and never
fails the cycle or the inbox.

## Drafted proposals

`aut improve draft` prepares a small change for human review. It writes proposals and candidates
inside the private store. It never edits source files, accepts proposals, commits, applies or merges changes.

```bash
aut improve draft --max-groups 1 --budget-usd 1
aut improve draft --target /path/to/project/config.toml
aut improve draft --mode failover --order pi --explain-model cached-local-model
aut improve draft --mode round_robin --order codex,claude,cursor,pi
```

The newest group report supplies recurring groups, in report order. Default: one group, at most
two calls per group, a 360-second run budget and 180 seconds per model call. Rejected groups
stay blocked unless `--reconsider FULL_GROUP_KEY` names one. Open proposals, accepted proposals
and adoptions also block drafting. A stale proposal permits a new draft.

Automatic selection prefers the group's harness. Ties use current quota headroom and the last
100 draft attempts' success rates. Missing or stale quota stays unknown. Existing quota reports
come from `~/.config/aut/observations.sqlite3`, opened read-only. Errors and timeouts advance to
the next adapter. Invalid output gets one repair attempt. Local backends follow eligible remote
backends in every mode. Decisions and skip reasons are recorded privately.

Remote packets contain signals, counts, redacted review rationales and selected redacted file
text. The user approved selected file text on 2026-11-21. The default excludes conversation
events for every remote flavor and failover attempt. `--excerpts all` separately opts in to
redacted events from at most three cases, clipped to 4,000 characters each. Local models include these
events locally. Target paths are local mappings; the model receives `target_1` style aliases.
The exact sent prompt is saved under `draft_packets`, and its digest is kept in the proposal.
Redaction detects known patterns; it cannot identify every possible secret.

Discovery offers project AGENTS.md (otherwise CLAUDE.md) for one project. Cross-project groups
use global instruction files from every registered Claude and Codex profile, plus ~/AGENTS.md.
Tool-contract groups may also offer matching Claude skill files. Environment groups use explicit
configuration globs, restricted to `.toml`, `.json`, `.yaml`, `.yml`, `.ini`, `.cfg`, `.conf` and
`.env` files. Code requires `--target`. At most four readable text targets of 8,000 characters
each are offered. Oversized or unreadable files are skipped with reasons.

Optional settings are read without loading sink configuration:

```toml
[improve]
draft_targets = ["~/.config/example/*.toml"]
draft_excerpts = "none"
# Optional AUT-specific Pi model. Use an existing OAuth profile.
# draft_pi_model = "provider/model"
# Optional local llama.cpp drafter on macOS or Linux. Use an instruction-tuned GGUF already on disk.
# draft_gguf = "/absolute/path/to/instruct.gguf"
# draft_gguf_gpu_layers = 0
```

Generated BEGIN/END blocks and lines with detected secrets are hidden. Returned `before` spans
must match the original local file exactly once and cannot cross hidden regions. No secret is
restored into model output. Responses must have exactly the requested fields, bounded explanatory
text, offered rubric ids and at most 1,200 added characters. A changed source invalidates the draft.

Git targets stage in a private shared clone at source HEAD, without source worktree metadata
writes. A dirty source target can produce an unstaged proposal when the clone differs. Non-Git
targets stage in a plain private candidate directory. Review the inbox diff and use the existing
human accept/reject commands. A shared clone depends on the source object store until accepted.

### Adapter limits

The verified remote protocol is **Pi 1.5.0 on POSIX** with a configured model and unexpired file-based
OAuth credentials. It uses a temporary credential-only home and an empty working directory.
Tools, MCP, extensions, skills, context files, templates, themes and session persistence are
disabled explicitly. Only the chosen provider's credential is copied; the source is not refreshed
or changed. Set `draft_pi_model` to use a different provider and model from Pi's default.
Other Pi versions fail closed until their isolation is checked.
Windows Pi discovery also fails closed: the existing subprocess runner cannot kill its complete
process tree there. GGUF drafting also requires POSIX process cleanup. MLX requires Apple silicon.

For local drafting without MLX, install `llama-server` from llama.cpp and set `draft_gguf`.
AUT starts a tool-free server on a private Unix socket and stops it after the attempt.
CPU is the default with zero GPU layers. A GPU-enabled llama.cpp build can offload layers
with a positive `draft_gguf_gpu_layers` value. Actual CUDA acceleration requires an NVIDIA
host and a CUDA-enabled build. AUT uses the local file and does not fetch weights during drafting.

Installed Codex 0.171.0 has no verified zero-tools control. Cursor 2026.11.05 print mode retains
tools. Claude 2.1.330 OAuth mode cannot prove absence of managed hooks; bare mode disables OAuth
and still permits managed hooks. These adapters are unavailable with explicit reasons. A read-only
filesystem sandbox is insufficient. No command silently runs these CLIs with tools enabled.

Pi OAuth cost is unknown, not zero. The report separates `known_spend_usd` and `unpriced_calls`.
`--budget-usd` caps admission against reported dollar spend; it cannot cap an unpriced subscription
call. Call counts and subprocess deadlines still apply. Both local backends use the shared `replay-model` lock,
offline mode and a hard subprocess deadline. No MLX inference or remote model calls were used
to validate this feature.

Isolation references: [Pi CLI](https://docs.pi.dev/latest/cli),
[Pi settings](https://docs.pi.dev/latest/settings),
[Claude hooks](https://code.claude.com/docs/en/hooks),
[Claude CLI](https://code.claude.com/docs/en/cli-reference), and
[Cursor parameters](https://cursor.com/docs/cli/reference/parameters).
Installed help was checked on 2026-11-21. These references describe CLI controls, not a live-run result.

`packaging/launchd/dev.aut.improve-draft.plist` is an optional 05:30 template. It does not run at
load. Replace its runtime, user and PATH placeholders before operator installation. Its PATH must
find the CLI and its runtime. Merging or packaging the template changes no live job.

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

## Accept, reject and adopt

Decide on a proposal from a terminal. The inbox page shows each open proposal's diff and the
commands to copy. An id is the full key or a unique prefix of 6 or more hex characters.

```bash
aut improve inbox list                      # open proposals and groups: ids and counts, no text
aut improve inbox accept 3fa2c1d0 [--note "…"]
aut improve inbox reject 3fa2c1d0 --reason "…"
```

Each decision is a new private `decisions` record and a `decision` journal row. The latest
decision per proposal wins. Running the same decision again changes nothing and says so. A
conflicting decision, such as a reject after an accept, is refused. One lock makes concurrent
decisions safe, and the page is rebuilt after each one.

Accept writes nothing outside the store and the candidate:

- If the target still has the saved digest, the proposal is accepted as it is.
- If the target changed but the old span still occurs exactly once and outside any generated
  `<!-- BEGIN … -->` … `<!-- END … -->` block, a new proposal is prepared from the same span,
  with the same extra fields and `supersedes: <old id>`. It is staged in
  `<store>/candidates/<new id>` when the old one was staged. The old id is marked `stale` with
  `superseded_by`, and decisions on the old id follow to the new one.
- Otherwise the proposal is marked `stale` and the command fails with "target changed".
  Proposals saved before spans were recorded always go `stale` when the target changed.
  A `stale` decision does not block the group; only a reject does, and a stale proposal can
  still be rejected. A target that exists but cannot be read right now is an error, not stale.

When the staged candidate's Git directory is its own real `.git` directory (a clone, not a
worktree or a `.git` symlink, which write into another repository), accept commits the target there on branch
`aut/improve-<id8>`, with the proposal id in the message. It uses Git plumbing on a private
index, so no checkout, hook or clean filter runs, the candidate's own index and branch stay as
they are, and only the literal target path changes. The command prints what to run next: fetch that branch into the
real repository, or copy the staged file after checking the target digest. You review, apply
and merge the change yourself.

Reject needs a reason of 1 to 500 characters with at least one letter or digit, so the page's
`"…"` placeholder pasted unedited is refused. It blocks the proposal's failure group, which the
inbox shows as "rejected on <date>", even when another proposal of the group was accepted. An
accepted group shows "accepted" until it is adopted. It
deletes `<store>/candidates/<id>` (and those of proposals it superseded) only; an operator's own
`--candidate` directory stays. A retry after a crash adds a missing journal row.
The copied commands and next steps are quoted for a POSIX shell.

After the merge, record the adoption:

```bash
aut improve adopt 3fa2c1d0 --commit <sha> [--pr N]
aut improve adopt --title "Run tests with CI's pytest entrypoint, not python -m pytest" \
  --commit ef03df5 --pr 53 --date 2026-11-14 --target ~/.claude/CLAUDE.md \
  --rubric avoidable_recovery --kind instruction --note "…"
```

Adopting a proposal needs an accept first and copies its target, rubric ids, kind and group.
It copies the title only from an operator proposal; a drafted proposal's title is model text,
so `--title` is required. The title is human-written, one line, at most 120 characters. The title and date are
meant for the sink (a later `publish-improve` step); targets, commit and note stay private.
`benefit_claim` is always false. The same proposal and commit (or, without a proposal, the same
title and commit) is one adoption, even when the command runs again on a later day.

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

For a saved prospective study, use `aut improve study run --study STUDY_ID` to check its inputs.
Add `--execute` to run the frozen trial within the study's time budget and report its decision.
Keep the baseline and candidate in separate directories outside the original source directory.
Both copies must match the proposal's recorded file hashes. Commands and acceptance files
come from the trusted trial specification. For command checks, use independent copies without
hard links or symlinks for the pinned files. The check must execute a pinned acceptance file
directly or pass it as a script to the current Python interpreter. The runner removes the
candidate's Python import path from protected checks. The study records evidence without
accepting or applying a proposal.

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

## Label real cases to check the judges

You can compare a judge with your own answers on captured work. Judges remain advisory even
when they meet the qualification policy. Nothing here accepts a proposal or changes your setup.

```bash
aut improve label --rubric completion_supported --limit 30
aut improve label --rubric avoidable_recovery --limit 30
```

The page runs on 127.0.0.1 under a random token. It reads the private store, without a sink
connection or a model call. Open the captured attempt through its case link. Press 1 for pass,
2 for fail, 3 for unknown, s to skip, u to undo, or q to finish. Historical text is evidence,
never an instruction to execute. Answers are saved at once; Ctrl-C also ends a sitting safely.

Only complete, unclipped stored attempts that meet the enabled rubric's requirements enter the
queue. Existing labels exclude the same evidence, including copied cases, for that rubric version.
The order favors model failures, split votes, then error-triggered cases. One ordinary control
follows every four priority items while controls remain. This sample measures agreement on selected
cases, not the failure frequency across all work. Model answers stay hidden during labelling.

Answers append `human_labels` records containing evidence and case IDs, rubric ID and version,
the human label, timestamp, labeller and a null note. No evidence text is copied into a label.
Undo appends a newer `label: null` tombstone, restoring an unlabelled item. It never deletes
history, never counts as human unknown, and allows that evidence to appear again. Labels from
an older rubric version remain history and do not qualify a newer version.
If the answer record saves but its journal entry fails, the terminal reports that failure.
The page still acknowledges the saved answer, so undo remains available.

At the end, AUT saves a `qualifications` record. It compares the latest human answer per evidence
with the latest judgment for that case and rubric version. Legacy judgments map to version 1
only when the stored rubric text matches the original definition exactly. Missing judgments
are missing comparisons; a model's unknown is an abstention. Missing comparisons suppress rates
and prevent qualification. Without human pass labels, the false alarm rate is unavailable and
the rubric stays unqualified.

The built-in policy needs at least 20 human fail labels, failure recall of at least 0.7, and
a false alarm rate of at most 0.2. Human fail labels count toward the minimum; human unknown
and undone answers do not. Qualification is a check on agreement, not proof of independence,
general accuracy, or a benefit from a proposed change.

Answers without current-version judgments enter `judge_queue.json`. The next diagnosis takes
at most eight queued cases through its existing model cache and budget. Normal audit cases
are deduplicated. Extra cases run after normal work, never hold blocking audit completion,
and remain queued until every applicable enabled rubric has a judgment. A save, undo or new
qualification refresh updates pending label cases. Queue updates preserve unrelated entries.
The normal sample and queued extras each have the rubric's `max_cases` cap. This keeps a full
normal sample from preventing queued comparisons forever; shared time and call budgets still apply.
No model runs during the labelling command itself.

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
The report also scores available individual model votes by rubric and model revision.
An absent vote counts as missing. Agreement between models does not establish the reference label.
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
The process writes `last_cycle.json`, refreshes the failure groups and then `inbox.html`.
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

Build a wheel, then install the newest one outside the development checkout using cached dependencies:

```bash
uv build --wheel
python -m agent_usage_tracker.improve.runtime \
  --wheel "$(ls -t dist/agent_usage_tracker-*-py3-none-any.whl | head -n 1)" \
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

### Read a private report card

The report card shows stored improvement evidence, open proposals and adoption comparisons in one local report.
Sampled judgments remain advisory. The report does not establish prevalence, causal benefit or payback.

```bash
aut improve report-card --days 28 --format md
aut improve --root /private/store report-card --format html
aut improve report-card --format json
```

Days must be between 1 and 366. The command always writes `report-card.md` and `report-card.html` in the private store.
It prints the selected format to stdout. Both report files use private file permissions.
Treat stdout and these files as private. No network connection, model call or sink credential resolution occurs.
The command reads only `ledger_timezone` from the configuration file, with the normal default when absent.

The current window includes today and compares with the preceding window of equal length.
Case dates use their first timestamped event in the ledger zone. Timestamps without a zone mean UTC.
Counts reuse `scores.daily`, including its newest-judgment and legacy-version rules.
Rubric versions, harnesses and triggers remain separate. Pass, fail and unknown counts appear for both windows.
Failure rates divide fail by pass plus fail. Unknown judgments stay visible outside that denominator.
Changes compare the same rubric version, harness and trigger and show percentage points.

Each adoption compares 14 ledger dates before its date with 14 dates after it, excluding the adoption date.
These are calendar dates of work, including weekends. Future case dates do not enter either side.
Each side needs at least 10 known control judgments before displaying its failure rate.
Smaller samples display `too few cases`, with known and unknown counts. Rubric versions remain separate.
Task counts mean distinct sessions. Group-linked adoption counts use recorded membership across stored group reports.
Explicit signal-linked adoptions can match tool, error class and command family in captured cases.
Missing group membership or usable signals leaves task counts unavailable.
These are observed before/after counts, not a causal estimate or a measured benefit.

Recurring groups use the latest saved group report, with that report's own window shown.
No group refresh runs. Proposal age comes from its preparation journal date, or remains unavailable.
The report shows open proposal titles, owner components, staged flags and shell-quoted decision commands.
Coverage uses the latest saved audit and cached `coverage.json`, without scanning source logs or querying usage stores.
Harness gaps require recorded usage events and no stored improvement session for that harness.
Missing cached coverage remains unknown. The incomplete-evidence fraction uses newest current-version judgments in the current window.

Loop cost shows model-call records, recorded load/inference seconds, diagnosis reports, labels and human decision events.
Load rows are separate from inference calls. Untimed records remain explicit.
Diagnosis call totals sum completed invocation reports and overlap model-call records. Do not add these totals.
Missing timing is unknown. Label event counts include undo events.
Drafter USD sums known costs for proposals prepared inside the window; missing costs are counted separately.
The report makes no payback calculation.

JSON contains counts, identifiers, booleans, nulls and enum values only.
Dates, titles, owner text, commands, rates, durations and money amounts appear only in the private human reports.
Transcript text, rationale, patches and target paths do not appear in either report.
The decision commands can include the shell-quoted private store path. All operator and model text is escaped for each output format.

**Write footprint and specification ruling:** the labelling specification requires the report card to recalculate current qualifications.
That requirement takes precedence over the report section's original two-file-only statement.
The command calls existing `qualify.run` for each enabled rubric before building the report.
This permits only its existing derived qualification records, qualification journal entries, queue synchronization and lock effects,
plus the two report files. Source evidence, judgments, human labels and decisions stay unchanged.
A new empty store receives the normal store initialization files. Existing support files are not rewritten.
`build`, `to_markdown`, `to_html` and `to_json` are otherwise read-only.

### Publish advisory scores and adoption markers

You can compare sampled judge scores with the dates you adopt fixes in the private effort dashboard.
A marker records adoption, and does not prove that a fix helped. All judges remain advisory.

```bash
aut publish-improve --dry-run --days 90
aut publish-improve --days 90
```

The command needs `transport.pg_dsn` for delivery and the updated sink schema.
It reads `AUT_IMPROVE_ROOT`, else `~/.local/share/aut/improve` (under `XDG_DATA_HOME` when set).
It refuses a missing store or missing `.aut-improve` marker, and never changes the private store.
Dry runs send no data or heartbeat and do not update the cache.
Days must be between 1 and 366.

Counts use the case's first timestamped event in the configured `ledger_timezone`, not the machine zone.
Timestamps without a zone are treated as UTC. Cases without timestamps do not enter the series.
The newest diagnosis wins for each case, rubric version and trigger.
Stopped cycles use the diagnosis recorded in their progress report.
Legacy judgments map to rubric version 1 for the original text, else version 0.
Control, error-result and merge-trigger samples remain separate, as do harnesses.
Incomplete evidence counts as unknown. The newest qualification record sets the qualified flag.
Missing qualification means unqualified. Error samples do not estimate overall failure prevalence.

Only daily counts, rubric identifiers and versions, harnesses, triggers and qualification flags leave the store.
Adoption delivery includes the short adoption identifier, local noon on its date, human-written title, kind, rubric identifiers and PR number.
Paths, case or session identifiers, commit SHAs, rationale, patches and transcripts stay private.
Public text fields must be short, single-line, and contain no path separators or long hexadecimal strings.
Unsafe metadata stops delivery before any score or adoption insert.

The cache is `improve_publish_cache.json` beside the AUT state file (`AUT_STATE` overrides its location).
It is scoped to the sink, private-store root and ledger zone. Only changed score rows are sent.
Keys removed inside the requested window get one zero row; older history remains unchanged.
The cache advances only after both writes succeed. Retries and a lost cache are harmless.
Adoptions use insert-on-conflict, so the first delivered marker remains authoritative.
The `improve_publish` heartbeat reports each delivery attempt through the existing collector-scan mechanism.

To schedule delivery, set `AUT_IMPROVE_PUBLISH=1` in the existing scan-job environment after the sink schema is deployed.
This adds one daily step after outcomes, with retries until it succeeds. No new timer is installed.
Both the scan runtime and improve runtime must include the rubric and score modules.
Merging this code enables no jobs and sends no data.
