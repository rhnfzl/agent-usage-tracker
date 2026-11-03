# Data contract

One normalized row per turn: an **assistant turn** (`kind:"agent"`, carries tokens) or a
**human turn** (`kind:"human"`, a prompt you sent, zero tokens). The collector emits it, the
n8n webhook validates it, and the sink stores it. Keep this file and `push.py::COLUMNS` and
the homelab `agents.usage_event` schema in sync.

### One session, two paths

A session file can sit at more than one path: Claude Code encodes the project directory into
the name, and the same repo reached two ways (`-Users-me-proj` and `prefix---Users-me-proj`)
gets a byte-identical copy under both. The glob then reads it twice and emits every turn twice.

That is harmless for stored data — both copies derive the same `event_id`, so the sink keeps
one. It is **not** harmless for anything that counts before the sink sees it, so `aut scan`
tallies each `event_id` once (and prints how many repeats it collapsed) and `aut export` writes
each row once, since its CSV is loaded with `COPY`, which has no `ON CONFLICT` to fall back on.

Dedup is by `event_id`, never by file: copies can and do diverge (one path keeps receiving
turns after the other stops), so skipping a file outright would drop real usage.

### What counts as a human turn

`kind:"human"` means **a person typed this**, not merely "the log recorded it under the user
role". Both agents write a great deal of machine-generated text into that role, and counting it
inflates the denominator of `agent_turns_per_human_turn`, which silently understates leverage.
Excluded:

| Agent | Excluded | Signal |
|---|---|---|
| Claude Code | tool results, sidechains (subagent transcripts), meta and compaction records | `isSidechain` / `isMeta` / `isCompactSummary`, `tool_result` blocks |
| Claude Code | background-task notifications, slash-command stdout, interrupt notices | text opens with `<task-notification>`, `<local-command-stdout>`, `[Request interrupted` |
| Claude Code | SDK programs driving a session | `entrypoint` in `sdk-py`, `sdk-cli` |
| Claude Code | Stop hook verdicts, turns made only of context blocks, sessions that only ran `/usage` | text opens with `Stop hook feedback:`; nothing left after removing `<system-reminder>`, `<ide_selection>`, `<ide_opened_file>` blocks; a read from byte 0 with no model call whose every prompt is `/usage` (a later chunk is never dropped, so a live scan and a full re-read agree; ported from AgentsView, MIT) |
| Codex | agent-spawned threads, scripted `codex exec`, MCP calls, another agent's integration | `session_meta.source` (`subagent` key / `exec` / `mcp`), `originator` |
| Codex | Codex Desktop scheduled automations | message opens with `Automation:` **and** contains `Automation ID:` |

A typed slash command (`/review`) **is** a human turn. An unknown or absent `entrypoint` stays
counted: only positively-identified programs are dropped.

Note this is a **classification change, not a schema change** — `kind` still takes the same two
values. Rows already in a sink keep the classification they were written with, because
`event_id` + `ON CONFLICT` makes re-scanning a no-op. Reclassifying history means deleting the
`kind='human'` rows and re-pushing.

## Event

```jsonc
{
  "event_id": "<sha256(agent|...)[:32]>",  // deterministic idempotency key
  "ts": "2026-07-18T20:11:04Z",            // ISO-8601, from the source log line (NOT NULL)
  "machine": "laptop",                      // role-based label, stamped by the runner
  "agent": "claude_code|codex|cursor|pi",
  "kind": "agent",                          // 'agent' = assistant turn (tokens); 'human' = a prompt you sent (0 tokens, model null)
  "model": "claude-opus-4-8",               // may be null (esp. cursor)
  "provider": "anthropic|openai|cursor|...",
  "input_tokens": 0,                        // FRESH (non-cached) prompt tokens
  "output_tokens": 0,
  "cache_read_tokens": 0,                   // prompt tokens served from cache
  "cache_write_tokens": 0,                  // prompt tokens written to cache
  "reasoning_tokens": null,                 // codex only; null elsewhere. SUBSET of output_tokens: informational, never added to totals or cost
  "total_tokens": 0,
  "est_cost_usd": null,                     // NOTIONAL / API-equivalent (subs are flat-rate)
  "project": "agent-usage-tracker"          // repo dir basename only, never a full path
}
```

### One repo, many worktrees

`project` is the working directory's basename, so before this rule a git worktree reported as a
project of its own: a repo worked on across 50 branches showed up as 50 projects and no
per-project total was real. A worktree is the same repo, so `<repo>/<container>/<branch>/sub`
is normalized to `<repo>/sub` before the basename is taken — which also keeps a monorepo's inner
package attributed to that package rather than to the branch it was checked out on.

Only the two conventional layouts are recognised, and both require a **dot**-directory so an
ordinary project folder can never trip the rule:

| Layout | Example | `project` |
|---|---|---|
| `<repo>/.claude/worktrees/<branch>` | Claude Code's own | `repo` |
| `<repo>/.worktrees/<branch>` (any `.*worktrees`) | hand-rolled | `repo` |
| `<repo>/.worktrees/<branch>/<pkg>` | monorepo package | `pkg` |

`git worktree add ../repo-wt` is indistinguishable from a sibling repo by path alone, so it is
left as-is. Like the `kind` rule above this is a **classification change, not a schema change**:
`ON CONFLICT` makes a re-scan a no-op, so rows already in a sink keep the project they were
written with until you re-export and UPDATE them by `event_id`.

## Webhook payload

`POST <webhook_url>` with header `X-AUT-Secret: <shared secret>`:

```json
{ "events": [ { ...event... }, ... ] }
```

The sink upserts with `ON CONFLICT (event_id, ts) DO NOTHING`, so re-delivery (retries,
both transports, re-scans) never double-counts.

## Normalization rules (why counts are comparable across agents)

- `input_tokens` means **fresh, non-cached** prompt tokens everywhere. Claude Code and Pi
  already separate cache fields. **Codex reports `input_tokens` inclusive of cached**, so
  its collector stores `input = input - cached_input` and `cache_read = cached_input`.
- One row per assistant response / `token_count` event, not per session. Codex uses the
  per-turn `last_token_usage` delta (not the cumulative total) to avoid double counting.
- `est_cost_usd` is only populated where the source logs a cost (Pi, Cursor). It is an
  API-equivalent estimate, never a real bill.
- `ts` is required at the sink (hypertable partition column). The collector **drops** any
  record whose source line has no timestamp (they are counted as skipped, never pushed), so
  null never reaches the sink even though `UsageEvent.ts` is typed `str | None`.

## Per-agent reality (measured on real logs, 2026-07-18)

| Agent | Source | Model | Timestamp | Notes |
|---|---|---|---|---|
| **claude_code** | `~/.claude/projects/**/*.jsonl` `assistant.message.usage` | yes | record `timestamp` | Most tokens are in the **cache** fields; summing only `input_tokens` undercounts ~460x. Globs all files (20k+); an inode+offset watermark reads only changed ones. |
| **codex** | `~/.codex/sessions/**/rollout-*.jsonl` `token_count` | `turn_context.model` | event `timestamp` | `input` includes cached; we split it. Captures `reasoning_tokens`. |
| **pi** | `~/.pi/agent/sessions/**/*.jsonl` `message.usage` | `message.model` / `model_change` | record `timestamp` | Cleanest schema; older sessions may log zeros. |
| **cursor** | `state.vscdb` `cursorDiskKV bubbleId:*` | **often null** (not in DB) | bubble `createdAt` or joined `composerData` | EXPERIMENTAL: a LOWER BOUND on tokens. For complete + model-attributed + costed data use `cursor_api`. Full-scans each run + dedups at the sink. **The only source of Cursor human turns** — see below. |

### The two Cursor sources are complementary, not redundant

They overlap on tokens and must not both be counted, which is why `config.example.toml` says to
pick one. But each holds something the other structurally cannot:

| | `cursor` (local `state.vscdb`) | `cursor_api` |
|---|---|---|
| human turns | **yes** | **none** — the feed is token usage, it has no prompt records |
| cache read/write tokens | always 0 | yes, and they dominate Cursor's totals |
| `cursor-agent` CLI usage | no, IDE only | yes |
| history older than first scan | yes, whatever the DB still holds | **no** — `cursor_api_lookback_days` bounds only the FIRST scan, so the API can never reach back past the day it was enabled |

`cursor_human_only = true` makes the combination safe: the local collector emits prompts and drops
its token rows, so the double-count the warning guards against cannot happen — a human turn carries
zero tokens by construction. Use it when you want `cursor_api`'s accurate tokens *and* Cursor human
turns, or when the local DB is the only place your older Cursor history still exists.

## Scan heartbeat: agents.collector_scan

Usage rows only prove a machine was used, not that it was watched: a collector that dies looks
the same as a quiet week. So every `aut scan` (not `--dry-run`) also writes one row per collector
it ran, after the usage for that collector has been delivered:

| Column | Meaning |
|---|---|
| `machine`, `collector` | who ran, which collector |
| `started_at`, `finished_at` | wall clock around that collector's drain loop (UTC) |
| `status` | `ok`, or `error` if the collector raised (the scan still fails exactly as before) |
| `events`, `human` | new agent / human turns this scan, the same counts the scan report prints |
| `newest_ts` | latest valid event timestamp seen this scan, null if none |
| `error` | the exception type only (for example `OperationalError`). The message never leaves the machine: it can name a project path or part of a credential, so it stays on local stderr |
| `aut_version` | the client version |

All rows of a scan go out in one `INSERT ... ON CONFLICT (machine, collector, finished_at) DO
NOTHING` over `transport.pg_dsn`, also when a collector raised. A webhook-only config skips it
with one stderr note. The heartbeat **never blocks usage delivery**: any failure sending it
prints one warning and is swallowed, so usage rows, the watermark and the exit status are the
same as without it.

## Session identity: agents.event_session

`usage_event` rows are insert-only, so which session an event belongs to lives in its own table,
one row per usage `event_id` (the same id, so it joins 1:1). `claude_code` fills it for every
event it emits, agent and human turns alike; the other collectors do not yet.

| Column | Meaning |
|---|---|
| `event_id`, `machine`, `agent` | the usage event this row describes |
| `session_id` | main session file: the record's `sessionId` (fallback: file stem). Subagent file: its `agentId` (fallback: file stem without `agent-`) |
| `parent_session_id` | subagents only: the record's `sessionId`, which is the PARENT session (fallback: the directory above `subagents/`). NULL for a main session |
| `run_origin` | `subagent` for any file under `subagents/` (including `subagents/workflows/<wf>/`); else `automated` for `entrypoint` `sdk-py` / `sdk-cli`; else `interactive` for `cli`; else `unknown` |
| `turn_id` | the user turn this event answers: the `promptId` of the most recent user record earlier in the same file, tool_result records included (they carry the current turn's `promptId`). A subagent keeps its FIRST user record's `promptId`, the parent turn that spawned it. NULL when no user record was seen yet |

`promptId` only appears on user records, never on assistant records, which is why the turn is
carried forward in file order, and saved with the file's read position so the next scan of that
file picks up the same turn. A session row is never rewritten once stored (the writer is INSERT
only), so a row stored with NULL `turn_id` stays NULL.

Delivery: over `transport.pg_dsn`, usage rows and session rows are written on one connection in
**one transaction** (`ON CONFLICT (event_id) DO NOTHING` for sessions), so a failure stores
neither and the watermark does not move. The webhook carries usage only and says so once on
stderr.

History: `aut backfill --sessions-only [--agents claude_code] [--dry-run]` re-reads every file from
the start on a throwaway watermark (the live one is neither read nor saved) and sends **only**
session rows. It never re-sends usage: a full re-read can emit an `(event_id, ts)` pair that differs
from the stored one, which would land as a new duplicate. Safe to re-run. `--dry-run` prints counts
(rows by `run_origin`, rows with NULL `turn_id`, subagent rows without a parent) and sends nothing.

## Activity: agents.activity_event

Non-token activity from Claude Code, one row per thing that happened. `attrs` (jsonb) holds
counts, flags, short enums, ids and hashes only, **never** prompt text, tool output, file paths,
titles, commit messages, URLs or repository names. `session_id` / `turn_id` follow the session
identity rules above; `ts` is the source record's own timestamp; `event_id` is
`make_event_id("claude_code", kind, seed)`.

| kind | one row per | seed | attrs |
|---|---|---|---|
| `steer` | human turn (1:1 with `kind='human'`) | the human turn's uuid | `via` (typed, suggestion_accepted, queued, slash_command, bash_input, skill), `words`, `chars`, `after_interrupt` |
| `interrupt` | `[Request interrupted` user record in a main session (a subagent's copy is the same keypress) | uuid | `during_tool` |
| `rejection` | errored tool_result opening `The user doesn't want to proceed` | tool_use_id | none |
| `commit` | SHA printed by a Bash `git commit` (not `--dry-run`), paired by tool_use_id | tool_use_id + sha | `sha`, `repo_key`, `amend` |
| `pr` | `pr-link` record (the same PR is linked many times; count distinct `(session_id, attrs->>'number')` in SQL) | the record's uuid, else sessionId + prNumber + the record's ts | `number`, `host` (github, bitbucket, other), `repo_key` |
| `turn` | `system` `turn_duration` record | uuid | `duration_ms`, `message_count`, `tool_calls`, `tool_errors`, `by_tool`, `hook_blocks`, `max_identical_run`, `retry_flag`, `runaway_flag` |
| `compaction` | `system` `compact_boundary` record | uuid | `trigger` when present |
| `invoke` | `Skill` tool_use | tool_use_id | `skill` (`mcp` for an MCP name) |

Rules worth knowing:

- `via=queued`: `promptSource == "queued"`, or (no `promptSource`, older files) the same text was
  enqueued earlier in the scan chunk by a `queue-operation` or a `queued_command` attachment.
  Slash, bash and skill prefixes win over the prompt source.
- `after_interrupt`: the previous non-meta user record in the file was an interrupt.
- Hook denials (`PreToolUse:... hook error`) are not rejections; they count in the turn's
  `hook_blocks` (and in `tool_errors`, which counts every errored tool_result).
- A commit call is a command segment (split on `;`, `&&`, `||`, `|` and newlines, heredoc bodies
  removed first) that is `git [global options such as -C <dir>, -c <k=v>]* commit`, optionally
  behind `VAR=x`, `env`, `command`, `timeout N` or `rtk [proxy]`, without `--dry-run`. So
  `--no-commit`, `cat-file commit`, `verify-commit` or `commit` in an echo or a heredoc never count.
- Commit SHAs come from git's `[branch sha]` line; failing that (and only for a successful call)
  from Claude Code's structured `gitOperation.commit`, or a condensed line printed by a
  command-output proxy. The condensed form is read only where it can be the commit's own output:
  the first line (`ok <sha>` / `<sha> <subject>`) when nothing ahead of the commit could have
  printed (only `cd`, `pushd`, `set`, `export`, `true`, `mkdir`, `rm`, `cat > file`, a variable
  assignment or `git add` came before), else the last `ok <sha>` line. The branch is not stored:
  branch names are free text.
- `repo_key` = HMAC-SHA256 of `host/owner/repo` (lowercased; credentials, port and `.git`
  dropped) under a per-install salt, from `git remote get-url origin` in the record's `cwd` (2 s
  timeout, cached per cwd per pass), NULL when the directory or remote is gone. A PR's key uses
  its URL host plus `prRepository` the same way, so commits and PRs of one repo share a key. The
  salt is the config key `repo_key_salt` (an `env:VAR` value works), else a random salt created
  (mode 0600) as `repo_key.salt` next to the state file on first use. Without the salt the recipe
  is not reproducible, so stored keys cannot be matched against a list of candidate repo names. Set
  the same `repo_key_salt` on every machine for their keys to match; changing it changes every new
  key. The remote URL is never stored or logged.
- `by_tool` keys are the tool names, except that every MCP tool (`mcp__<server>__<tool>`) counts
  as `mcp`: a server label is free text and can name an employer or a client.
- Activity extraction is best effort: a record whose extraction raises is skipped and counted (one
  stderr line per pass), and never holds back the usage and session rows of its file.
- Tool block ids seen in the open turn (the newest 200) ride in the watermark until the turn ends,
  so a block Claude Code re-logs just after a scan boundary counts once. A subagent file never
  gets a `turn_duration`, so its turn counters are not kept in the watermark.
- Turn aggregates cover the records since the previous turn end in the same file, carried across
  scans in the file's watermark entry. `retry_flag` = `max_identical_run >= 3` (same tool and
  same input in a row); `runaway_flag` = `tool_errors >= 6`, or `>= 3` while one tool ran 10+
  times. Both rules come from AgentsView (MIT).

Delivery: over `transport.pg_dsn` in the same transaction as the usage and session rows of that
batch, `ON CONFLICT (event_id, ts) DO NOTHING` (the writer has INSERT only). The webhook skips
them with the same one-line note as session rows. History: `aut backfill --activity-only
[--dry-run]` works exactly like `--sessions-only` (both flags may be given together) and never
sends usage rows.

## Derived artifact: metrics.json (not part of the event contract)

`aut report` produces `metrics.json` — an aggregate of the events above, never a passthrough.
It is read from the sink with the `project` column deliberately excluded, so the published
artifact aggregates to `agent × model × month × token-class (× kind)` and cannot carry a
project name. `$` figures are notional / API-equivalent, computed from the synced price map
(`aut sync-prices`), not from `est_cost_usd`.
