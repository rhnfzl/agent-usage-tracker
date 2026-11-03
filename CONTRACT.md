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
| Codex | agent-spawned threads, scripted `codex exec`, MCP calls, another agent's integration | the FIRST `session_meta` (a fork repeats its parent's meta later): `source` (`subagent` key / `exec` / `mcp`), `originator` |
| Codex | Codex Desktop scheduled automations | `thread_source == "automation"`, or (older files without it) the message opens with `Automation:` **and** contains `Automation ID:` |

A Codex prompt is an `event_msg user_message`. Newer Codex Desktop files carry prompts only as an
`item_completed` item of type `UserMessage`; those count only in a file with no `user_message`,
because older files log every prompt both ways. Every Pi `role: user` message is a human turn.

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
  "agent": "claude_code|codex|cursor|cursor_cli|pi|...",
  "kind": "agent",                          // 'agent' = assistant turn (tokens); 'human' = a prompt you sent (0 tokens, model null)
  "model": "claude-opus-4-8",               // may be null (esp. cursor)
  "provider": "anthropic|openai|cursor|...",
  "input_tokens": 0,                        // FRESH (non-cached) prompt tokens
  "output_tokens": 0,
  "cache_read_tokens": 0,                   // prompt tokens served from cache
  "cache_write_tokens": 0,                  // prompt tokens written to cache
  "reasoning_tokens": null,                 // codex and pi; null elsewhere. SUBSET of output_tokens: informational, never added to totals or cost
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
| **codex** | `~/.codex/sessions/**/rollout-*.jsonl` `token_count` | `turn_context.model` | event `timestamp` | `input` includes cached; we split it. Captures `reasoning_tokens`. Known gap, not fixed: some Codex Desktop `token_count` rows carry only `total_tokens` (input and output 0, no model) and are dropped (177 rows, about half of the Desktop tokens, when measured). |
| **pi** | `~/.pi/agent/sessions/**/*.jsonl` `message.usage`, plus the `usage` of `compaction` records | `message.model` / `model_change.modelId` | record `timestamp` | Cleanest schema; older sessions may log zeros. `reasoning_tokens` from `usage.reasoning` (part of output). `project` from the session header's `cwd`. |
| **cursor** | `state.vscdb` `cursorDiskKV bubbleId:*` | **often null** (not in DB) | bubble `createdAt` or joined `composerData` | EXPERIMENTAL: a LOWER BOUND on tokens. For complete + model-attributed + costed data use `cursor_api`. Full-scans each run + dedups at the sink. **The only source of Cursor human turns** — see below. |
| **cursor_cli** | `~/.cursor/chats/<h>/<uuid>/store.db` (cursor-agent CLI chats) and `~/.cursor/projects/*/sdk-agent-store/*/index.db` (SDK runs) | none | prompt's embedded `<timestamp>` (minute precision), else the chat's `createdAt`; SDK: run `started_at` | **No token rows** (the stores hold none, and `cursor_api` counts these tokens). Human rows, session rows and activity only. See the Cursor CLI sections below. |

### The two Cursor sources are complementary, not redundant

They overlap on tokens and must not both be counted, which is why `config.example.toml` says to
pick one. But each holds something the other structurally cannot:

| | `cursor` (local `state.vscdb`) | `cursor_api` |
|---|---|---|
| human turns | **yes** (IDE only; CLI prompts come from `cursor_cli`) | **none** — the feed is token usage, it has no prompt records |
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
one row per usage `event_id` (the same id, so it joins 1:1). `claude_code`, `codex` and `pi` fill
it for every event they emit, agent and human turns alike (Pi: except entries a fork copied from
its parent, see below); `cursor_cli` fills it for its human rows; the other Cursor collectors do
not. The Claude Code rules:

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

Codex (identity from the FIRST `session_meta` in the file; a forked file repeats its parent's
meta on a later line, which is ignored for identity, source and cwd):

| Column | Codex |
|---|---|
| `session_id` | `session_meta.payload.id` (the thread id). A child thread's `payload.session_id` is its parent's, so usage rows of child threads used to land on the parent. The usage `event_id` seed still uses the old field, so no stored usage row changes id |
| `parent_session_id` | `parent_thread_id`, else NULL |
| `run_origin` | `subagent` when `source` is a dict with a `subagent` key; `automated` for `source` `exec` / `mcp`, `thread_source == "automation"` or originator "Claude Code"; `interactive` for `source` `vscode` / `cli`; else `unknown` |
| `turn_id` | `turn_id` of the latest `event_msg task_started`, carried in the file's watermark entry |

Pi (identity from the session header on line 1, kept in the file's watermark entry):

| Column | Pi |
|---|---|
| `session_id` | the header `id` (fallback: the uuid in the file name) |
| `parent_session_id` | for a fork or clone: the header `id` of the file named in `parentSession`, else the uuid in that name. The path is never stored |
| `run_origin` | always `unknown`: interactive, print, RPC and SDK runs write the same file, and Pi has no subagents |
| `turn_id` | `id` of the latest user message, carried in the watermark |

A Pi fork copies its parent's entries with their ids and timestamps, all dated before its own
header. Their usage rows dedupe by id; they get no session or activity rows from the fork file.

Cursor CLI (`cursor_cli`; its only usage rows are human rows, so only they carry a session row):

| Column | Cursor CLI |
|---|---|
| `session_id` | the chat's `meta.agentId` (equal to its directory name, the fallback) |
| `parent_session_id` | always NULL: a subagent chat (meta carries `subagentInfo`, with `parentAgentId`) emits no human row, so it has no session row to hold its parent; its `turn` rows carry it as the `parent_session_id` attr |
| `run_origin` | `interactive`. A subagent chat's prompts are its parent agent talking and are not human rows. A headless `cursor-agent -p` run cannot be told apart from a typed one (no marker in the store) |
| `turn_id` | the prompt's `providerOptions.cursor.requestId` |

SDK agents (`index.db`) emit no usage row, so they get no session row either: their `agent_id` is
the `session_id` of their activity rows. Because these sessions have no session row, every
`cursor_cli` `turn` row carries the origin itself as the `run_origin` attr: `interactive` for a
typed chat, `subagent` for a subagent chat (plus `parent_session_id`, the parent's uuid, or NULL
when it is not a uuid), `automated` for an SDK run. A commit or rejection row takes its turn's
origin by joining on `(session_id, turn_id)`.

Delivery: over `transport.pg_dsn`, usage rows and session rows are written on one connection in
**one transaction** (`ON CONFLICT (event_id) DO NOTHING` for sessions), so a failure stores
neither and the watermark does not move. The webhook carries usage only and says so once on
stderr.

History: `aut backfill --sessions-only [--agents claude_code,codex,pi,cursor_cli] [--dry-run]` re-reads every file from
the start on a throwaway watermark (the live one is neither read nor saved) and sends **only**
session rows. It never re-sends usage: a full re-read can emit an `(event_id, ts)` pair that differs
from the stored one, which would land as a new duplicate. Safe to re-run. `--dry-run` prints counts
(rows by `run_origin`, rows with NULL `turn_id`, subagent rows without a parent) and sends nothing.

## Activity: agents.activity_event

Non-token activity from Claude Code, Codex, Pi and the Cursor CLI, one row per thing that happened. `attrs` (jsonb) holds
counts, flags, short enums, ids and hashes only, **never** prompt text, tool output, file paths,
titles, commit messages, URLs or repository names. `session_id` / `turn_id` follow the session
identity rules above; `ts` is the source record's own timestamp; `event_id` is
`make_event_id(agent, kind, seed)`. Claude Code:

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

### Codex and Pi activity

Same kinds and attrs as Claude Code where the source has them. Seeds start with the session id
(`session_id|...`). A forked Codex child (first `session_meta` has `forked_from_id`) opens with a
copy of its parent's history, the lines before `subagent_history_start_ordinal` (0-based); they
give no activity rows and no `turn_id` (the copy ends in a `turn_aborted` that is not a real
interrupt). A spawned child without `forked_from_id` can carry an ordinal too (measured: equal to its line count, with no copy): it is
read in full. The commit rules (command segments, SHA parsing, `repo_key`) and the turn
aggregates (`by_tool` with the MCP collapse, `max_identical_run`, `retry_flag`, `runaway_flag`)
are the shared code in `collectors/_activity_common.py`.

| kind | Codex | Pi |
|---|---|---|
| `steer` | each prompt counted as human (seeded like it); `via` always `typed`; `words`, `chars`, `images` (attached images), `after_interrupt` (an interrupt since the previous prompt) | each user message: `via` `skill` (text opens `<skill name="`), `queued` (the previous message is a toolResult: typed while the agent ran), else `typed`; `words`, `chars`, `after_interrupt` (the previous assistant record was aborted) |
| `interrupt` | `turn_aborted` with reason `interrupted`; no attrs | assistant `stopReason == "aborted"`; `during_tool` (the entry before it is a toolResult) |
| `turn` | `task_complete`: `duration_ms` from the record, else `task_started` to `task_complete`; `tool_calls` and `by_tool` from the model's tool calls (`function_call`, `custom_tool_call`; an `mcp__` namespace or a name containing `__`, the older CLI form, counts as `mcp`) plus `McpToolCall` items whose id matches no MCP call of the turn (Codex Desktop logs most MCP calls only as items); `tool_errors` from `CommandExecution` items with `exit_code != 0` and `McpToolCall` items with status `failed` (command items run inside a tool call, so they are not counted as calls) | derived: one per user message, from it to the first `stop` / `aborted` / `length` assistant record, else to the last entry before the next user message (an `error` followed by another assistant record is a retry, not an end); `duration_ms` = end ts minus prompt ts, `message_count` (the prompt, assistant and toolResult messages), tool aggregates from `toolCall` blocks, `tool_errors` from `toolResult.isError`. The last open turn of a file is emitted on a later scan |
| `commit` | `CommandExecution` item: its shell script and `aggregated_output`, `repo_key` from the item's `cwd` | `bash` toolCall paired with its toolResult by `toolCallId`; `repo_key` from the header `cwd` |
| `compaction` | `ContextCompaction` item; no attrs | `compaction` record: `tokens_before` |
| `invoke` | not possible (no skill tool; reading a skill file is a guess) | a user message opening `<skill name="X">`: `skill` = X |
| `rejection` | not possible: no user approval decisions are logged (an automatic reviewer decides) | not possible: Pi has no permission prompt |
| `pr` | not possible: no PR record | not possible from structured data (only by parsing `gh pr create` output, skipped) |

Also not possible: Codex `during_tool`, `hook_blocks`, `message_count` and a compaction
`trigger`; Pi `hook_blocks`, a compaction `trigger` and a `slash_command` / `bash_input` split of
steers (templates are expanded before logging).

### Cursor CLI and SDK activity (`cursor_cli`)

A chat store keeps its messages as JSON blobs and lists them in order in protobuf root blobs
(field 1, 32-byte blob ids); every step writes a new root. A summarized or rewound chat starts a
new list, so older messages sit only in older roots: every root list that is no prefix of another
is read, and a message seen in several lists counts once. Messages after a prompt belong to its
turn (`turn_id` = its requestId) and take its timestamp. The store has no turn-end record, so a
turn is emitted, all its rows at once and only once (the chat's watermark entry keeps the emitted
requestIds), when a later prompt follows it or the chat has been idle for an hour. Seeds are
`session_id|...`.

| kind | Cursor CLI | Cursor SDK |
|---|---|---|
| `steer` | each human row (seed: requestId; a later prompt sent during the running turn reuses that requestId and gets its own human and steer row, seeded with its blob id too, at its own timestamp); `via` always `typed`; `words` and `chars` of the `<user_query>` text, `images` (image blocks) | not possible: run prompts are app input |
| `commit` | a `Shell` tool-call whose command is a commit call, paired by `toolCallId` with its result (`Exit code: N` then `Command output:`); the shared SHA parser; `repo_key` from the call's `working_directory`, else NULL | none |
| `rejection` | a `SwitchMode` result `Mode switch was rejected by the user` (seed: toolCallId) | none |
| `turn` | one per requestId: `tool_calls`, `by_tool` (Cursor's tool names; `mcp_<server>_<tool>` and `CallMcpTool` count as `mcp`, a name that is not a plain identifier as `other`), `tool_errors` (tool results with `highLevelToolCallResult.isError`), `hook_blocks` (Shell results opening `Rejected: Hook blocked`, also errors), `max_identical_run`, `retry_flag`, `runaway_flag`, `run_origin` (and `parent_session_id` in a subagent chat); no `duration_ms`. A tool call counts once per turn by `toolCallId`, even when a rewritten blob in another root lists it again | one per run that ended: `status` (`finished`, `cancelled`, `error`), `duration_ms` = `finished_at - started_at` (NULL without both), `run_origin` `automated`; `turn_id` = `run_id`, `session_id` = `agent_id`. Queued and running runs are sent once they end (a row is insert-only). `error_code` and `result` are free text and never read |
| `interrupt` | not possible: no record in the store. The transcript's `turn_ended` carries a status but no id or timestamp, and its count does not line up with the prompts, so it is not joined | a cancelled run is `turn` with `status` `cancelled` |
| `pr` | skipped: only `gh pr create` output text | none |
| `compaction`, `invoke` | not possible (no record; reading a skill file is a guess) | none |

Known gaps: a prompt no surviving root lists (its history was dropped) still gives its human and
steer rows but no turn row, once the chat is idle (until then it may be a new prompt whose root is
not written yet). A message after a summary and before the next prompt sits in no turn
(about 5% of tool calls when measured). A tool call logged under a prompt more than an hour after
the chat went quiet is lost.

Delivery: over `transport.pg_dsn` in the same transaction as the usage and session rows of that
batch, `ON CONFLICT (event_id, ts) DO NOTHING` (the writer has INSERT only). The webhook skips
them with the same one-line note as session rows. History: `aut backfill --activity-only
[--dry-run]` works exactly like `--sessions-only` (both flags may be given together) and never
sends usage rows. For `cursor_cli` the first normal `aut scan` also sends its human rows, which no
collector sent before.

## Active time: agents.time_interval

One row per span a person was at the keyboard (`source='aw'`, from ActivityWatch) or said they
worked (`source='manual'`, from `aut time add`). Steer gaps are not sent: the sink derives them
from `agents.activity_event`. The sink unions all three and merges across machines and agents,
so parallel agents never count twice; agent runtime is never human time.

| column | aw | manual |
|---|---|---|
| `event_id` | `make_event_id('activitywatch', machine, app, start, end, sha256(sent title))` of the clipped piece | `make_event_id('manual', machine, start, end)` |
| `ts`, `end_ts` | the window event clipped to the scan range, UTC | the typed interval, UTC |
| `app` | the ActivityWatch app name | null |
| `title` | **private**: redacted, cut to 300 chars; null when `activitywatch_titles = false` | null |
| `category` | editor, terminal, agent, comms, docs, design, media, web-work, web, other | null |
| `work_relevant` | category in editor, terminal, agent, comms, docs, design, web-work | true unless `--not-relevant` |
| `attrs` | `rule`: config_app, default_app, browser_work_title, browser_other, unknown | `{}` |

**Titles are private.** They are never in a public view (the read-only role sees
`agents.time_interval_public`, every column but `title` and `event_id`; event_id is built from
machine, app, start and end only, never the title). **Retention is off by default: titles are
kept for good**, in the sink and in ActivityWatch. Setting `activitywatch_title_days = N` turns a
limit on: the collector then deletes ActivityWatch's own window events older than N days through
its REST API, in batches of 500 until none is left or 20 s pass (a long backlog drains over
several scans), never failing the scan and logging a count only; the event that spans the cutoff
is kept until it is wholly older; a dry run purges nothing; and a title older than N days is
never sent. The sink side (nulling stored titles) is a separate homelab job, off for now.

The `activitywatch` collector is **opt-in**: it is in the valid agent list but not in the default
used when the config has no `agents`. It reads THIS host's window and AFK buckets with
aw-server's query API (`filter_keyvals(afk, "status", ["not-afk"])`, then
`filter_period_intersect`) over `[watermark, now - 5 min]`, at most one day per pass; the first
scan starts a day before the window bucket's `created` (aw-server-rust writes it in local time
labelled UTC, so it can be hours late; empty days cost one query each). The watermark is the range end, saved only after
delivery. aw-server closed or no buckets for this host: no rows, no error.

Guards, all on the machine before anything is sent:

- login window and screen saver rows are dropped (not active time);
- deny-list: password managers (1Password, Bitwarden, KeePassXC, Keychain Access, Passwords,
  Dashlane, LastPass, Proton Pass), private-browsing titles (Private Browsing, Incognito,
  InPrivate) and rows ActivityWatch flags incognito are dropped entirely. `activitywatch_deny_apps`
  and `activitywatch_deny_titles` (regexes, matched on the title and the browser URL) extend both
  lists; private names belong there, never in code. Only a count and minutes of dropped rows are
  logged. Limit: a Chromium private window (Brave, Chrome, Helium) is not detectable, because
  today's watcher sends no incognito flag and the title has no marker;
- control characters (NUL included) are removed from the app and title;
- redaction replaces with `[redacted]`: URL credentials (`user:pass@`), `password=` / `secret=` /
  `token=` / `api_key=` / `access_key=` values, also with a prefix (`PGPASSWORD=`, `DB_PASSWORD=`,
  `GITHUB_TOKEN=`, `access_token=`), `Bearer` values, JWTs, `sk-` keys, `github_pat_` and `gh[pousr]_` tokens,
  `xox[abprs]-` tokens, AWS access key ids, email addresses, and any unbroken run of 32+ base64
  characters (long hex, base64, and also long slugs or paths). The rest of the title is kept;
- the browser URL is matched against the work patterns locally and never sent.

Category: `activitywatch_categories` (app -> category) over the code defaults; an app name also
matches a known name followed by a space ("PyCharm Professional"). A browser row is `web-work`
when its title or URL matches a work pattern (code hosts, issue trackers, docs, cloud consoles,
localhost, AI chat, plus `activitywatch_work_titles`), else `web`. Unknown apps are `other`, not
relevant. `browser_category()` is the seam a model replaces later.

`aut time add --start <local> --end <local>` or `--minutes N [--at <local end>]`, plus
`--not-relevant`. No note field (nothing to redact). Refused: end at or before start, a span over
16 hours, an end in the future. Writes over `pg_dsn` only.

Delivery: over `transport.pg_dsn` in the same transaction as the usage, session and activity
rows of the batch, `ON CONFLICT (event_id, ts) DO NOTHING`. With a webhook and a `pg_dsn`, the
webhook carries the usage and the activity and time rows of the same batch still go over
`pg_dsn`. A webhook-only machine skips them with the same one-line note; there is no backfill for
time, so it does not send time.

## Derived artifact: metrics.json (not part of the event contract)

`aut report` produces `metrics.json` — an aggregate of the events above, never a passthrough.
It is read from the sink with the `project` column deliberately excluded, so the published
artifact aggregates to `agent × model × month × token-class (× kind)` and cannot carry a
project name. `$` figures are notional / API-equivalent, computed from the synced price map
(`aut sync-prices`), not from `est_cost_usd`.
