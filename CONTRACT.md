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
| **cursor** | `state.vscdb` `cursorDiskKV bubbleId:*` | **often null** (not in DB) | bubble `createdAt` or joined `composerData` | EXPERIMENTAL: a LOWER BOUND on tokens. For complete + model-attributed + costed data use `cursor_api`. Full-scans each run + dedups at the sink. **The only source of Cursor IDE human turns** (CLI prompts come from `cursor_cli`); see below. |
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
| `status` | `ok`; `degraded` if the collector ran but could not read a source that is there (an expired Cursor token, a stopped aw-server, a locked or corrupt store: what it could read is still delivered, the reason is on local stderr, and the scan goes on to the next collector); or `error` if the collector raised (the scan still fails exactly as before). A source that is simply absent (the tool is not installed) stays `ok` |
| `events`, `human` | new agent / human turns this scan, the same counts the scan report prints |
| `newest_ts` | latest valid event timestamp seen this scan, null if none |
| `error` | the exception type only (for example `OperationalError`, or `HTTPError` on a `degraded` row; `CollectorUnavailable` when no exception caused it). The message never leaves the machine: it can name a project path or part of a credential, so it stays on local stderr |
| `aut_version` | the client version |

All rows of a scan go out in one `INSERT ... ON CONFLICT (machine, collector, finished_at) DO
NOTHING` over `transport.pg_dsn`, also when a collector raised. A webhook-only config skips it
with one stderr note. The heartbeat **never blocks usage delivery**: any failure sending it
prints one warning (type and redacted message, on local stderr) and is swallowed, so usage rows,
the watermark and the exit status are the same as without it. Ctrl-C leaves at once and sends no
heartbeat, in `aut scan`, `aut outcomes` and `aut label` alike.

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
neither and the watermark does not move. The webhook carries usage only: with a `pg_dsn` too, the
session rows of the batch go over `pg_dsn` in one transaction with its activity and time rows, and
a failure there leaves the watermark in place. A webhook-only machine skips them and says so once
on stderr.

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
| `link` | closed turn: a `turn_duration` record, else (a subagent or scripted file, or a final turn with no `turn_duration`) the file once read to its end and idle for an hour, or the next record an hour or more after the turn's last one | the turn's uuid, else the last record's uuid | `repo_key`, `branch_key`, `ticket_keys`, `sha_refs`, `commit_keys`, `pr_keys`, `role` (see Link rows below) |

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
  branch names are free text (the link row carries it as `branch_key`).
- A commit's `repo_key` comes from the directory the command moved to (`cd X && git commit`,
  `git -C X commit`, relative to the record's `cwd`; a relative move with no `cwd` is ignored),
  else the record's `cwd`, else the session's worktree (`worktree-state.originalCwd`, for a `cwd`
  that is gone); NULL when none resolves. A move through a variable (`cd $DIR`) is not followed.
  These rules apply to rows first sent by this version: a backfill re-sends a stored commit row
  with the same `(event_id, ts)`, so the sink keeps the key it first got, and a turn's stored
  commit row and its link row can name different repos. Consumers must not assume they agree.
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

### Link rows (`link`, all four agents)

One row per closed turn, the raw material for linking sessions into episodes. Every value is a
key, an enum or a list of those (no raw SHA: a quoted hex string can be a secret); branch names, ticket ids, URLs and paths stay on the
machine. Keys are HMAC-SHA256 under the same salt as `repo_key`.

| attr | value |
|---|---|
| `repo_key` | the turn's repo, by source: the commit's own directory (rules above), the worktree's original checkout, the record's `cwd`, Codex `session_meta.git.repository_url` (normalised like a remote) |
| `branch_key` | `HMAC(repo_key \|\| branch)` from, in order, git's `[branch sha]` commit line, `worktree-state.worktreeBranch`, Codex `session_meta.git.branch`, the record's `gitBranch`, taken only from a source in the same repo as `repo_key` (a branch key never pairs one repo with another's branch). `main`, `master`, `develop`, `trunk` and `HEAD` get none, and a source that names one ends the search (a commit on `main` does not borrow the record's `gitBranch`) |
| `ticket_keys` | up to 5 `HMAC(ticket)` for strict ids `[A-Z][A-Z0-9]{1,9}-\d+` (not inside a longer word; `UTF-8`, `SHA-256`, `ISO-8601` and similar standards excluded) in the turn's branches, commit subjects and Claude Code session titles (`ai-title`, `custom-title`) |
| `sha_refs` | up to 50 commit-like SHAs quoted in the turn's tool output or message text: 7 to 40 lowercase hex with at least one digit and one letter, not part of a longer word, uuid or hash, each kept only as `sha_key = HMAC("sha7:" + first 7 hex, lowercased)` (none without a salt), so a hex secret that passes the filter is never kept, not even a prefix |
| `commit_keys` | the `sha_key` of every commit the turn's commit calls printed (the SHAs the `commit` rows carry: git's `[branch sha]` line, else the structured or condensed form); a quoted SHA meets a commit where its `sha_refs` key is in some turn's `commit_keys` |
| `pr_keys` | up to 10 `HMAC(host/owner/repo\|number)` from GitHub `https://github.com/<o>/<r>/pull/<n>` and Bitbucket `https://bitbucket.org/<ws>/<repo>/pull-requests/<n>` URLs in the same text, plus the turn's `pr-link` records (the same PR gives the same key either way) |
| `role` | `child` for a subagent or a scripted run (`run_origin` subagent or automated), else `seed` |

Turn close per agent: Claude Code as in the table above; Codex `task_complete` or
`turn_aborted` (seed `session_id|turn_id`); Pi the derived turn (seed `session_id|<prompt id>`);
Cursor CLI each emitted turn (seed `session_id|requestId`), SDK runs get none. A Claude Code
worktree holds for every turn until `worktree-state` reports it left. Codex takes the repo and
branch from the first `session_meta` (its `cwd`, else the remote URL) for every turn. A Claude
Code turn with no `turn_duration` closes at its last record once the file sits idle for an hour,
or, read in one go, once the next record is an hour or more later, so both cut the same place.
Seeds are deterministic, so `aut backfill --activity-only` fills history and a live scan split
anywhere gives the same rows as one full read (assuming a record reaches the file within the
hour of its timestamp); the dry run prints the link rows per role and how many carry each key.
Turns already open when this version is first deployed are the exception: their link row is
built from the records read after the upgrade only (a Claude Code worktree entered before it is
not seen until the next `worktree-state`), and it lands before a backfill's complete row with
the same `(event_id, ts)`, so the partial row stays. A Codex watermark from before link fields
existed gets its `session_meta` link fields rebuilt from the file's first record.

### Codex and Pi activity

Same kinds and attrs as Claude Code where the source has them. Seeds start with the session id
(`session_id|...`). A forked Codex child (first `session_meta` has `forked_from_id`) opens with a
copy of its parent's history, the lines before `subagent_history_start_ordinal` (0-based); they
give no activity rows and no `turn_id` (the copy ends in a `turn_aborted` that is not a real
interrupt). A spawned child without `forked_from_id` can carry an ordinal too (measured: equal to its line count, with no copy): it is
read in full. The commit rules (command segments, SHA parsing, `repo_key`) and the turn
aggregates (`by_tool` with the MCP collapse, `max_identical_run`, `retry_flag`, `runaway_flag`)
are the shared code in `collectors/commits.py`, `collectors/activity_common.py` and `repo_keys.py`.

| kind | Codex | Pi |
|---|---|---|
| `steer` | each prompt counted as human (seeded like it); `via` always `typed`; `words`, `chars`, `images` (attached images), `after_interrupt` (an interrupt since the previous prompt) | each user message: `via` `skill` (text opens `<skill name="`), `queued` (the previous message is a toolResult: typed while the agent ran), else `typed`; `words`, `chars`, `after_interrupt` (the previous assistant record was aborted) |
| `interrupt` | `turn_aborted` with reason `interrupted`; no attrs | assistant `stopReason == "aborted"`; `during_tool` (the entry before it is a toolResult) |
| `turn` | `task_complete`: `duration_ms` from the record, else `task_started` to `task_complete`; `tool_calls` and `by_tool` from the model's tool calls (`function_call`, `custom_tool_call`; an `mcp__` namespace or a name containing `__`, the older CLI form, counts as `mcp`) plus `McpToolCall` items whose id matches no MCP call of the turn (Codex Desktop logs most MCP calls only as items); `tool_errors` from `CommandExecution` items with `exit_code != 0` and `McpToolCall` items with status `failed` (command items run inside a tool call, so they are not counted as calls) | derived: one per user message, from it to the first `stop` / `aborted` / `length` assistant record, else to the last entry before the next user message (an `error` followed by another assistant record is a retry, not an end); `duration_ms` = end ts minus prompt ts, `message_count` (the prompt, assistant and toolResult messages), tool aggregates from `toolCall` blocks, `tool_errors` from `toolResult.isError`. The last open turn of a file is emitted on a later scan |
| `commit` | `CommandExecution` item: its shell script and `aggregated_output`, `repo_key` from the command's own directory, else the item's `cwd` | `bash` toolCall paired with its toolResult by `toolCallId`; `repo_key` from the command's own directory, else the header `cwd` |
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
| `commit` | a `Shell` tool-call whose command is a commit call, paired by `toolCallId` with its result (`Exit code: N` then `Command output:`); the shared SHA parser; `repo_key` from the command's own directory (relative to the call's `working_directory`), else the `working_directory`, else NULL | none |
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
batch, `ON CONFLICT (event_id, ts) DO NOTHING` (the writer has INSERT only). A webhook-only machine
skips them with the same one-line note as session rows. History: `aut backfill --activity-only
[--dry-run]` works exactly like `--sessions-only` (both flags may be given together) and never
sends usage rows. For `cursor_cli` the first normal `aut scan` also sends its human rows, which no
collector sent before.

## Outcomes: agents.commit_outcome, manual_outcome, episode_correction

Written by three commands on the machine that holds the clones and the logs. Every value is a
SHA, a key, a session id, an enum, a number or a time; no URL, path, branch, title, subject or
file name is stored. All three tables are insert-only: readers take the latest row.

`aut outcomes [--fetch] [--dry-run]` reads the `commit` rows and the git clones under
`outcome_repo_roots` (one per git common dir; repo_key -> clone is held in memory for the run)
and writes `agents.commit_outcome (sha, repo_key, state, survival, decided_at)` only when a
commit's `(state, survival)` differs from its latest stored row.

| state | rule |
|---|---|
| `merged` | ancestor of `origin/HEAD` (fallback `origin/main`/`master`/`develop`, never a local branch), or a single-commit patch-id twin on it, or (with `--fetch`) a merged GitHub PR that `gh pr list --search <sha>` finds; `survival` = share (0..1, 4 decimals) of its added lines that `git blame` still attributes to it (or its twin) on the default branch 14 days after the merge, first 30 files, NULL for a merge commit, a commit adding no lines or a gh-only merge |
| `reverted` | merged, and a commit on any branch says `This reverts commit X` for it or its twin within 14 days of the merge |
| `reworked` | merged and `survival < 0.5` |
| `open` | reachable from a local or remote branch, not from the default branch |
| `abandoned` | reachable from no branch, no twin, no merged PR; also a commit no local clone holds |
| `superseded` | reachable from no branch and the session's next commit row was an `--amend` (the pre-amend SHA) |
| `undecided` | under 14 days since the merge (merged) or since the commit row (everything else); also a merged commit whose default ref was last updated before merge + 14 days, a commit gh could not answer for, and a commit whose clone git could not read or whose survival it could not measure |

- `sha` is the commit row's form (often abbreviated); `repo_key` is the row's, else the key of the
  clone that holds the SHA, else `''`. A clone counts as holding a SHA only when its committer
  time is within a day of the row, so a short SHA never matches another repo's commit.
- The merge time is when the commit (or its twin) landed: the committer time of the first-parent
  commit of the default branch that brought it in. Survival is blamed at the newest first-parent
  commit before merge + 14 days, only once the default ref was updated after that time (its tip
  is newer, its FETCH_HEAD is newer, or this run fetched it). It is computed once and cached by
  full SHA in `outcomes_cache.json` next to the state file (saved before the sink is written),
  with gh answers (a "no" only once the commit is 60 days old). A failed git read is never
  cached or scored; the next run measures again. A file gone from the base commit counts as 0
  lines alive.
- When several clones hold a commit, the one where it is furthest along wins (on the default
  branch, then on a branch, then unreachable).
- A commit this run cannot see or decide (no clone holds it any more, gh failed, a stale default
  ref, a failed or timed-out git read of a clone that holds or might hold it) never replaces a
  stored row of its sha: decided states stay decided. Such a git failure is counted as
  `repo_unreadable` or `survival_failed` in the run's report.
- `--fetch` (or `outcome_fetch = true`) runs `git fetch --upload-pack=git-upload-pack --quiet
  --no-tags --no-auto-maintenance --no-write-fetch-head --no-recurse-submodules origin` only in
  a clone whose origin's repo_key a commit row carries (30 s
  each, then SIGTERM so git removes its locks; failures are skipped and counted) and turns on the
  gh lookup. Without it, a commit whose GitHub PR was squash-merged ends `abandoned`, so set
  `outcome_fetch = true` on the machine that runs the nightly job. A dry run reads the sink and the clones as they are and changes nothing: no
  rows, no cache, no heartbeat, no fetch and no gh even with `--fetch` or `outcome_fetch` (so a
  squash-merged commit gh would settle previews as `abandoned`). A real run sends one `agents.collector_scan` row, collector `outcomes`.

- Every git command aut runs (here, and `git remote get-url origin` for repo keys) goes through
  `git_safe`: `-c core.fsmonitor= -c core.hooksPath=/dev/null -c protocol.ext.allow=never
  -c diff.external= -c core.sshCommand=ssh -c core.askPass= -c gc.auto=0
  -c maintenance.auto=false`, `--no-textconv --no-ext-diff` on `log`/`show`/`diff`,
  `--no-textconv` on `blame`, with `GIT_CONFIG_NOSYSTEM=1` and `GIT_TERMINAL_PROMPT=0`, so a
  clone's own config, attributes or hooks never make git run a command. The user's global config
  (HOME) still applies, so fetch keeps their ssh and credential setup.

`aut outcome <shipped|ruled-out|open> [--session ID]` writes `agents.manual_outcome (session_id,
outcome, ts, repo_key)` for `--session ID`, else the newest interactive (entrypoint `cli`) Claude
Code session whose project directory is the current directory or a parent up to the repo top
level; `repo_key` is the current directory's. `packaging/claude/commands/outcome.md` wraps it as
`/outcome` and passes the invoking session's id.

`aut episodes review [--month YYYY-MM]` serves the month's `agents.episode` rows (default: the
last completed month) on `127.0.0.1` at a random port and token. Each seed session shows what the
local Claude Code, Codex, Pi and Cursor CLI logs say: agent, project folder (last two parts of the
cwd), local start and run time (first to last record), number of human prompts, the first and
last prompt (redacted with the ActivityWatch title rules, about 200 characters), up to 5 commit
subjects (git's commit line, else the `-m` message), up to 3 PR titles (`gh pr create --title`),
the 3 files it edited most, its titles and branch names; a line per episode sums it up. The page
and its text never leave the machine and are never logged or stored. Episodes whose first seed
session already has a `confirm` or `reject` row are left out (and counted); the page shows a
sample of 30 spread over (outcome, size) with a link to all. Answers become
`agents.episode_correction (id, ts, action, a_session, b_session, attrs)`: don't know (the
default) -> nothing, right -> `confirm`, should split -> one `split` per session taken out,
should merge with #n -> `merge` (b = that episode's first seed session), not building work ->
`reject`; `attrs` holds `repo_keys` (and `b_repo_keys`) copied from `episode_member`. It prints
the share of reviewed episodes marked right.

`aut inspect --episode ID | --day YYYY-MM-DD | --label UNIT_ID` is the read-only evidence page
for the same episodes, served the same way (127.0.0.1, random port and token, local titles and
branch names). Per episode it shows the member sessions, the seed sessions' link keys by type
(commit, pr, branch, ticket; a key two sessions share is what joined them), each commit's latest
`commit_outcome`, the outcome rule of `episode_link()` that fired, the current label per unit (as
`label_latest` picks it, below-tau labels shown as unknown) and the `episode_correction` rows of
its sessions. `--label` also lists every label row of that unit. Its only write is the
`aut episodes review` form it links to, for the episodes on the page.

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
| `category` | editor, terminal, agent, dev, comms, docs, design, media, web-work, web, other | null |
| `work_relevant` | category in editor, terminal, agent, dev, comms, docs, design, web-work | true unless `--not-relevant` |
| `attrs` | `rule`: config_app, default_app, macos_category, browser_work_title, browser_other, unknown | `{}` |

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
localhost, AI chat, plus `activitywatch_work_titles`), else `web`. `browser_category()` is the
seam a model replaces later.

An app in neither mapping gets the category its macOS bundle declares (rule `macos_category`):
the collector finds the bundle with `mdfind` (`kMDItemContentType ==
'com.apple.application-bundle' && kMDItemDisplayName == '<app>'`), reads
`LSApplicationCategoryType` from its `Contents/Info.plist`, once per app per scan, with a 2 s
timeout. `developer-tools` is `dev`, `business` is `comms`, `productivity` is `docs`,
`graphics-design` is `design` (all relevant); `music`, `video`, `entertainment`, every games
category, `social-networking`, `sports`, `lifestyle`, `news` and `photography` are `media`; any
other declared category is `other` (both not relevant). Off macOS, with no bundle, no declared
category, a name holding `'`, `\` or `*`, or on any error or timeout, the app stays `other`
with rule `unknown`. The config mapping and the code defaults always win and are never looked
up. Limit: a browser missing from the defaults is classified by its bundle (often
`productivity`, so `docs`); add it to `activitywatch_categories` as `browser`.

`aut time add --start <local> --end <local>` or `--minutes N [--at <local end>]`, plus
`--not-relevant`. No note field (nothing to redact). Refused: end at or before start, a span over
16 hours, an end in the future. Writes over `pg_dsn` only.

Delivery: over `transport.pg_dsn` in the same transaction as the usage, session and activity
rows of the batch, `ON CONFLICT (event_id, ts) DO NOTHING`. With a webhook and a `pg_dsn`, the
webhook carries the usage and the activity and time rows of the same batch still go over
`pg_dsn`, with the batch's session rows. A webhook-only machine skips them with the same one-line note; there is no backfill for
time, so it does not send time.

## Labels: activity_event kind `label` (`aut label`)

`aut label run [--task T] [--since YYYY-MM-DD] [--dry-run]` and `aut label backfill --task T
--days N` (optional extra `aut[label]`, Apple silicon) let a local model answer one closed
question per unit. The text it reads (Claude Code prompts, ActivityWatch titles) comes from this
machine's logs at run time, goes through the ActivityWatch redaction, and is never stored, sent
or logged; only the answer is (the one exception: `aut label teach` asks Claude Sonnet about
Claude Code text, which already lives with Anthropic; see below). One `agents.activity_event` row per decision, `agent = 'label'`,
`ts` = the unit's own time, `session_id`/`turn_id` null:

| task | unit (`unit_id`) | candidates |
|---|---|---|
| `steer_intent` | `steer` (the steer's event_id), Claude Code steers with local text | framing, instruction, clarification, correction, redirection, review, acceptance, stop, other |
| `work_category` | `episode_day` (`<episode_id>:<local date>`), `aw_block` (5+ min of one ActivityWatch category with no steer inside) | planning, research, building, debugging, review, learning, maintenance, collaboration, other |
| `soft_outcome` | `episode` (episode_id), only `no_code` episodes touched in the last 14 days | ruled_out, blocked, still_open, resolved |
| `aw_title_relevance` | `aw_title` (HMAC of app + title under the repo-key salt), browser rows only, asked once per title | relevant, not_relevant |

`attrs`: `task`, `task_ver`, `unit`, `unit_id`, `label`, `p` (calibrated probability of the
label), `dist` (calibrated probability per candidate), `below_tau`, `model_id`, `model_rev` (the
Hugging Face snapshot), `prompt_ver`, `calib_ver`. `event_id` = `make_event_id('label', task,
task_ver, unit_id, model_id, model_rev, prompt_ver, calib_ver)`: a rerun is a no-op, a new model,
prompt or calibration adds a row and never overwrites one. Readers take the newest `task_ver` and
model per unit, and treat `below_tau = true` as unknown (left out of shares, numerator and
denominator).

Backends return a distribution over the supplied candidates: `mlx_letter` reads an instruct model
(default `label_model`, from the local Hugging Face cache only) through the logits of the answer
letters, scoring the original and the reversed candidate order and averaging them; `encoder` is
the slot for a small classifier trained on the labels. The choice per task (backend, model,
temperature, tau per label) is `label.json` next to `config.toml`; without one a task runs
uncalibrated and every row is `below_tau`. tau = the smallest calibrated p at which the label
reaches `label_precision` (default 0.90) on the gold labels, at least 0.5.

The nightly run (`aut-scan.sh`, once a day after `outcomes`, where `AUT_LABEL=1`) labels the tasks
in `label_tasks`, incrementally from a watermark in the state file (`collectors.label`), inside
`label_budget_s`, in batches that advance the watermark only once delivered over `pg_dsn`. It
sends one `agents.collector_scan` row, collector `label`. `daily_note` drafts one private note per
past day from counts only (that day's confident labels and episode facts, never text) into
`agents.daily_note (day, text, model_id, created_at)`, first draft kept. A dry run writes nothing.

### The eval set and the bake-off (`aut label teach | review | eval | choose | train-encoder`)

Three local files per task under `~/.local/share/aut/eval/` (`$XDG_DATA_HOME/aut/eval/`), never
sent and never holding text; a unit's text is rebuilt from this machine's logs when needed:

| file | row |
|---|---|
| `<task>.silver.jsonl` | `unit_id`, `teacher` (a model id), `dist` (probability per candidate), `date`, `task_ver` |
| `<task>.jsonl` (gold) | `unit_id`, `gold` (a candidate), `labeller` (`human`), `date`, `task_ver`; a later row for a unit wins |
| `<task>.eval.jsonl` | one bake-off record per backend (below), with its `ts` |

`aut label teach --task T --n N [--budget-usd X] [--days D]` gives up to N units of the last D
days (default 90; stratified by unit type and day) the local model's raw distribution, and, for
units whose text comes from Claude Code logs only (`steer`, `episode_day`, `episode`; never an
ActivityWatch unit), Claude Sonnet's (`claude-sonnet-5-5`) through the local `claude -p` (no
tools, no saved session, no user settings or MCP servers), asked for a probability per candidate.
Sonnet spend stops at X USD (default 5) per run; a rerun adds only the missing (unit, teacher)
rows.

`aut label review [--task T] [--days D]` serves one unit at a time on 127.0.0.1 (random port,
random token) with its redacted local text and the candidates: keys 1-9 answer, s skips, u undoes,
q ends. The teachers' answers show only after the answer. Units come round-robin over their silver
stratum (the teachers' common top label, `disagree`, or `unlabelled`), so rare labels and teacher
disagreement come up as often as common labels; the page shows gold per label against the floor
(30 per label; `soft_outcome` 30; `aw_title_relevance` 150 per class; target 50 where it differs).

`aut label eval --task T [--days D]` scores every backend (each local model with an official MLX
build, and the encoder once trained) on the gold units. Gold is split once by the unit's local
day: one half fits the temperature and tau, the other half reports. The record (in
`<task>.eval.jsonl`, and as one `agents.activity_event` row per backend, `agent = 'label'`,
`kind = 'eval'`, `ts` = the eval time) holds numbers and model ids only:

`task`, `task_ver`, `backend`, `model_id`, `model_rev`, `size_mb`, `prompt_ver`, `calib_ver`,
`temperature`, `n`, `n_cal`, `n_held`, `macro_f1`, `kappa` (Cohen's, vs gold), `ece_raw`, `ece_cal`
(before and after temperature scaling), `coverage` (share of reported units at or above their
label's tau), `s_per_decision`, and `per_label: {<label>: {n, precision, recall, f1, tau, coverage,
precision_at_tau, precision_lb, share_error_pp}}`. `precision_lb` is the 95% Wilson lower bound at
tau; `share_error_pp` compares the label's share among units at or above tau with its share in
gold, both re-weighted from the stratified sample to the silver label mix.

`aut label choose --task T` takes each model's newest record and writes the smallest (`size_mb`)
that passes the bar into `label.json`: a held-out report, `coverage` at least `label_coverage`
(default 0.75), and no `share_error_pp` above 5. Labels without a tau stay unknown. `aut label
train-encoder --task T` fits the `encoder` backend (logistic regression over hashed word and fact
features, stdlib only) on gold (three times the weight) and silver, never on a held-out gold unit,
and reports its held-out accuracy; its file (`~/.local/share/aut/label/encoder/<task>.json`) holds
bucket ids and weights, no words.

## Re-logged messages: one event_id, several rows (read-side rule)

The sink's key for usage rows is `(event_id, ts)` and cannot be `event_id` alone: it is a
TimescaleDB hypertable, so the partitioning column must be in the unique index. Claude Code logs
the same message more than once, sometimes a thousand-plus lines later in the same file, sometimes
minutes apart, and a scan that sees only part of that spread takes the timestamp from the lines it
read. A later scan can then insert a second row with the same `event_id`, another `ts` and a
partial token total. Token counts per message are cumulative, so readers keep, per `event_id`, the
row with the largest `total_tokens` (the sink's `agents.usage_event_dedup`); any smaller row is a
prefix of it. The collector cannot settle this per scan (which lines a scan sees is what varies)
and cannot clean up (its role holds INSERT and SELECT only).

`aut report` and `metrics.json` are retired: aggregates are the sink's private database views.
`aut report` still runs for one release, prints that note and writes nothing.
