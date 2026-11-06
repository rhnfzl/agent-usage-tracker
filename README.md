# agent-usage-tracker (`aut`)

[![CI](https://github.com/rhnfzl/agent-usage-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/rhnfzl/agent-usage-tracker/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11--3.15-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Runtime dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

Coding agents on a subscription do not tell you what they cost or how you work with them.
`aut` reads the logs that **Claude Code, Codex CLI, Cursor (IDE, CLI and SDK) and Pi** already
write to disk. It turns them into a small set of rows and pushes them to a sink you host
yourself: an n8n webhook, or Postgres/TimescaleDB directly.

It records:

- **Usage**: one row per assistant turn (all four token classes, model, project basename) and
  one row per prompt you typed.
- **Sessions**: which session, turn and parent session each turn belongs to.
- **Activity**: steers, interrupts, rejections, commits, PRs, turns, compactions, skill
  invokes, and per-turn link rows that let the sink join sessions into episodes.
- **Active time** (opt-in): not-AFK window time from ActivityWatch, plus manual entries.
- **Outcomes**: what became of each agent commit (merged, reverted, reworked, open, abandoned),
  and your own verdict per session or episode.
- **Labels** (optional extra): a local model answers closed questions such as "what kind of
  steer was this", and only the answer is sent. The one exception is the `daily_note` task,
  which sends a short note the model writes.

aut itself publishes nothing. The numbers live in your sink's database views.

## Why not a proxy?

These agents log in with an **OAuth subscription, not an API key** (Claude Max/Pro, ChatGPT
Plus/Pro, Cursor Pro, Pi `/login`). A LiteLLM or Helicone style proxy only sees API key
traffic, so it sees nothing of your subscription use. Reading what each tool already records
means no key, no proxy, and it works offline: the logs are still there when you reconnect.
Every USD figure is **estimated and API-equivalent**, since subscriptions are flat-rate.

## Privacy model

**What leaves the machine** (only to the sink you configure):

- token counts, model, provider, timestamps, the `machine` label, and the project directory's
  basename in plain text (never a full path). That basename is usually the repository name;
- session and turn ids;
- activity rows: counts, flags, short enums, ids and HMAC keys. Two fields carry names as they
  are: the skill name on `invoke` rows, and the tool names in a turn's `by_tool` counts. MCP
  tools all count as `mcp`, but a skill or plugin you named after a client is sent as named;
- raw commit SHAs on commit rows and in `agents.commit_outcome`, and outcome states. For a
  public repo a SHA can be looked up;
- label answers (an enum and a probability). The `daily_note` task also sends a 3 to 5
  sentence note the local model writes from counts, into `agents.daily_note`;
- ActivityWatch intervals, only if you enable that collector: the app name (never redacted,
  and sent even with titles off), its category, the work flag and the rule that matched;
- ActivityWatch window titles, with that collector on. They are redacted on this machine first
  (see [Collectors](#collectors)) and can be turned off with `activitywatch_titles = false`;
- scan heartbeats, whose `error` field holds the exception type only.

**What never leaves**: prompt text, tool output, file paths, commit messages, branch names,
ticket ids, URLs, remote URLs, error messages, and the Cursor session token. The repository
identity (its origin remote), branches, tickets, PRs and SHAs quoted in text are sent only as
HMAC-SHA256 keys under a per-install salt, so they cannot be matched against a list of
candidate names. The project basename and commit SHAs above are the exceptions. The local review and
inspect pages read titles from this machine's logs and keep them in the page.

**Network calls aut makes, besides the sink**:

| When | Call |
|---|---|
| `cursor_api = true` | Cursor's dashboard API, with your local session token, scoped to your own user |
| `aut sync-prices` | the LiteLLM, models.dev and pydantic/genai-prices price lists |
| `aut outcomes --fetch` or `outcome_fetch = true` | `git fetch` in your clones and `gh pr list` |
| `activitywatch` in `agents` | the ActivityWatch server at `activitywatch_url` (default `http://localhost:5600`). With `activitywatch_title_days` above 0, every real scan also sends `DELETE` requests there for its window events older than that |
| `aut label teach` | Claude Sonnet through your local `claude -p`, for Claude Code text only (that text already lives with Anthropic); capped by `--budget-usd` |

`aut label` runs the model on this machine. aut never downloads a model. `llama-server`, for the
models that use it, listens on 127.0.0.1 only and stops with the run.

Every git command aut runs ignores a clone's own hooks, fsmonitor, external diff, textconv,
credential helper and ssh command, so a cloned repo cannot make git run its code.

## Install

Python 3.11 to 3.15. macOS, Linux and Windows. The base install has zero runtime dependencies.

```bash
uv tool install git+https://github.com/rhnfzl/agent-usage-tracker
```

Three optional extras:

| Extra | Adds | Needed for |
|---|---|---|
| `postgres` | `psycopg[binary]` | anything that uses `transport.pg_dsn`: sessions, activity, time, heartbeats, `outcomes`, `outcome`, `episodes review`, `inspect`, `time add`, `backfill --sessions-only/--activity-only` |
| `label` | `psycopg[binary]`, and on Apple silicon `mlx-lm` and `huggingface_hub` | `aut label` (the MLX backends run on Apple silicon only) |
| `merge` | `torch`, `peft`, `transformers`, `huggingface_hub` | one command, once, for `evalengine-4b`: see [The models](#the-models) |

```bash
uv tool install "agent-usage-tracker[postgres] @ git+https://github.com/rhnfzl/agent-usage-tracker"
uv tool install "agent-usage-tracker[label] @ git+https://github.com/rhnfzl/agent-usage-tracker"
# from a checkout: uv tool install ".[postgres]"   (or pipx install ".[postgres]")
```

On a shared machine aut reads only the running user's home. If you set `home`, point it at
your own directory.

## Quick start

```bash
mkdir -p ~/.config/agent-usage-tracker
cp config.example.toml ~/.config/agent-usage-tracker/config.toml
chmod 600 ~/.config/agent-usage-tracker/config.toml
$EDITOR ~/.config/agent-usage-tracker/config.toml   # machine, agents, [transport]

aut status             # config and watermark state
aut scan --dry-run     # parse and print per-agent, per-model totals; sends nothing
aut scan               # push new rows since the last run
aut backfill           # optional: reset the watermark and push all history
```

Then put `aut scan` on a timer (see [Scheduling](#scheduling)). A scan is safe offline: the
watermark only advances after a push succeeds, and the sink drops re-sends by `event_id`.

## Commands

`--agents a,b` (before or after the subcommand) overrides the configured agent list for one run
of `scan`, `backfill`, `export` or `status`, for example `aut --agents codex,pi scan --dry-run`. Any
other command refuses it.

| Command | What it does | Needs |
|---|---|---|
| `aut scan [--dry-run]` | read new log lines since the watermark, push, advance. `--dry-run` prints the report and sends nothing | a transport |
| `aut backfill` | reset the watermark for the selected agents, then scan all history | a transport |
| `aut backfill --sessions-only [--dry-run]` | re-read all history and send only session rows; the watermark is untouched | `pg_dsn` (not with `--dry-run`) |
| `aut backfill --activity-only [--dry-run]` | the same for activity rows (steers, commits, turns, links, ...); never usage. Both flags may be given together | `pg_dsn` (not with `--dry-run`) |
| `aut status` | show machine, agents, transports, watermarks and the age of the last good backup | |
| `aut backup [--check]` | an encrypted, deduplicated restic snapshot of the logs, config, label files and `repo_key.salt` ([docs/new-laptop.md](docs/new-laptop.md)), then the retention (`restic forget`: 14 daily, 8 weekly, 12 monthly; `--prune` once a week). Skips with exit 0 while another run holds the lock, on battery below 20%, or when the repository is unreachable. `--check` runs `restic check` and sends nothing | `[backup]` in `config.toml`, `restic` on `PATH` |
| `aut export [--out FILE]` | write all usage rows as CSV (stdout by default), deduplicated, columns in sink order for `COPY`. Exits 1 if a collector that feeds the CSV could not fully read a source, so the CSV is partial (`activitywatch` has no usage rows, so it never does); `backfill --sessions-only`/`--activity-only` do the same for the collectors with those rows | |
| `aut sync-prices [--out FILE] [--litellm-url URL] [--models-dev-url URL] [--genai-prices-url URL]` | build the API-equivalent price map, `model_prices.json` in the config dir by default; the URLs must be http(s) | network |
| `aut time add --start T --end T` | record a manual active-time interval in local time, e.g. `--start "2026-11-03 09:00"` | `pg_dsn` |
| `aut time add --minutes N [--at T] [--not-relevant]` | the same as a length ending at `--at` (default now); `--not-relevant` marks it not work time. At most 16 hours, never in the future | `pg_dsn` |
| `aut outcomes [--fetch] [--dry-run]` | score every commit row against your local clones and write `agents.commit_outcome`. `--fetch` fetches first and asks `gh`. `--dry-run` writes nothing and never fetches or asks `gh`, even with `--fetch` | `pg_dsn`, clones under `outcome_repo_roots` |
| `aut outcome shipped\|ruled-out\|open [--session ID]` | your verdict on this project's newest interactive Claude Code session (or `--session`) | `pg_dsn` |
| `aut episodes review [--month YYYY-MM]` | hand-check a sample of 30 of a month's episodes (default: last completed month) on a local page; writes `agents.episode_correction` | `pg_dsn` |
| `aut inspect --episode ID \| --day YYYY-MM-DD \| --label UNIT_ID` | local evidence page: why an episode (id or prefix), a day's episodes, or a label exists. Its linked correction form writes `agents.episode_correction` | `pg_dsn` |
| `aut label run [--task T] [--since YYYY-MM-DD] [--dry-run]` | label new units since the watermark, for `label_tasks` or one task | `label` extra, `pg_dsn`, the model in the HF cache |
| `aut history-steers [--dry-run]` | daily steer estimates for the lost 2026 logs, from the CLI history files (rows of kind `steer_estimate`, a count and no text) | `pg_dsn` |
| `aut label backfill --task T --days N` | label one task over the last N days; the nightly watermark is not moved back. `soft_outcome` always covers the last 14 days, whatever `--days` or `--since` say (it notes this on stderr) | same |
| `aut label teach --task T --n N [--budget-usd X] [--days D]` | teacher (silver) labels for a stratified sample of up to N units (default spend cap 5 USD, window 90 days) | same, plus `claude` on `PATH` |
| `aut label review [--task T] [--days D]` | answer gold labels one unit at a time on a local page | `pg_dsn` |
| `aut label eval --task T [--days D]` | score every backend on your gold labels (saved scores are re-used) and print one table: quality, speed, memory | same as `run` |
| `aut label eval --task T --final --model M [--days D]` | score M on the final test days, which nothing else reads | an eval record of M |
| `aut label choose --task T [--model M]` | with `--model`, write M's saved result into `label.json`; without it, print the table and a recommendation and write nothing | an eval record |
| `aut label train-encoder --task T [--days D]` | fit the small stdlib encoder on gold and silver labels | gold or silver labels |

Label tasks: `steer_intent`, `work_category`, `soft_outcome`, `aw_title_relevance`, and
`daily_note` (for `run` and `backfill` only).

The local pages (`episodes review`, `inspect`, `label review`) listen on 127.0.0.1 at a random
port with a random token. Their text is never stored or logged.

`packaging/claude/commands/outcome.md` is a Claude Code `/outcome` command that runs
`aut outcome <verdict> --session <this session>`. Copy it to `~/.claude/commands/`.

## Configuration

The config file is `~/.config/agent-usage-tracker/config.toml` (`$XDG_CONFIG_HOME` is honoured,
`AUT_CONFIG` overrides the path). **Secrets never live in this repo.** Any value written as
`env:VAR` is read from the environment at run time. aut warns when the file holds a secret and
is readable by group or others.

[`config.example.toml`](config.example.toml) lists every key with comments. The ones that
matter:

| Key | Default | Meaning |
|---|---|---|
| `machine` | short hostname | role-based label for this machine, e.g. `laptop` |
| `agents` | every collector except `activitywatch` | which collectors run (see below). The default turns on `cursor`, `cursor_sdk` and `cursor_api` together, so set `agents` yourself before `cursor_api = true`, or Cursor tokens count twice |
| `[transport] webhook_url` | unset | n8n ingest webhook. `https://` only (or `http://` to localhost) |
| `[transport] webhook_secret` | unset | sent as `X-AUT-Secret`; a webhook without it is refused |
| `[transport] cf_access_client_id`, `cf_access_client_secret` | unset | Cloudflare Access service token, to push from any network without a VPN. With only one of the two set, aut warns and sends no Access headers |
| `[transport] pg_dsn` | unset | direct Postgres. The webhook carries usage only, so this is also how activity, time, outcome, label and heartbeat rows arrive. It also carries session rows, and usage when the webhook fails. Add `sslmode=require` when the host is remote: without it aut warns on each run, because libpq can fall back to plaintext. `sslmode=disable` states that a private link such as Tailscale is intended and silences the warning |
| `repo_key_salt` | random `repo_key.salt` next to the state file | HMAC key for repo, branch, ticket, PR and SHA keys. Set the same value on every machine so keys match |
| `cursor_api` | `false` | opt-in accurate Cursor usage from the dashboard API |
| `cursor_api_lookback_days` | `90` | how far back the first `cursor_api` scan reaches. Older history is never fetched |
| `cursor_human_only` | `false` | the local `cursor` collector sends prompts only, so it can run beside `cursor_api` |
| `cursor_sdk_manifests` | unset | glob of Cursor SDK run `manifest.json` files |
| `cursor_db_path` | found per OS | path to Cursor's `state.vscdb` |
| `home` | the running user's home | the home directory the collectors read |
| `activitywatch_*` | see the example | URL, title retention (`0` keeps titles), titles on or off, deny lists, work title patterns, categories |
| `outcome_repo_roots` | `["~/projects"]` | where `aut outcomes` looks for clones (6 levels deep) |
| `outcome_fetch` | `false` | fetch and ask `gh` before scoring. Turn it on where the nightly job runs, or squash-merged PRs count as abandoned |
| `label_tasks` | `[]` | the tasks the nightly `aut label run` labels. Empty means it labels nothing |
| `label_model` | `mlx-community/gemma-4-31b-it-4bit` | a short name from [The models](#the-models) or a Hugging Face model id, read from the local cache only. `daily_note` needs an instruct model, so leave it on Gemma if you use that task |
| `label_llama_server` | `llama-server` | the binary for `aurelis-2b` and `kev-4b`. Use a full path if the nightly job's `PATH` lacks it |
| `label_precision`, `label_recall`, `label_coverage`, `label_budget_s` | `0.90`, `0.60`, `0.75`, `3600` | floors per label (a model that misses one is marked as failing), coverage bar, time budget of one run in seconds |
| `[backup] repository`, `password_command`, `paths`, `excludes` | unset | for `aut backup`: the restic repository, the command restic runs for its password, the paths (replace the defaults) and excludes (added to the model-cache exclude) |

Keys outside `[transport]` must come before the `[transport]` line in the file, or TOML puts
them inside that table and aut never sees them.

Files aut keeps on this machine:

| File | Where |
|---|---|
| watermarks | `~/.local/state/agent-usage-tracker/state.json` (`$XDG_STATE_HOME`, `AUT_STATE` overrides) |
| `repo_key.salt`, `outcomes_cache.json` | next to the state file |
| `state.corrupt` | next to the state file: a copy of a corrupt `state.json`, kept before aut starts clean. A `state.json` aut has no permission to read stops the run instead and is left as it is |
| `state.lock` | next to the state file: an empty file, the advisory lock for one short watermark save, so overlapping `scan` and `label` runs keep each other's watermarks |
| `model_prices.json`, `label.json` | next to `config.toml`. Nothing in aut reads `model_prices.json`; it is kept for readers outside aut |
| label gold, silver, eval and final files; saved raw scores; the trained encoder; merged models; the label run cache | `~/.local/share/aut/eval/`, `~/.local/share/aut/eval/scores/`, `~/.local/share/aut/label/encoder/`, `~/.local/share/aut/label/models/`, `~/.local/share/aut/label/cache.json` (`$XDG_DATA_HOME`) |
| `scan.env`, `scan.log`, `retry-pending`, `outcomes-day`, `label-day` | `~/.config/agent-usage-tracker/`, used by the timer wrapper. The wrapper hard-codes this path, so it ignores `$XDG_CONFIG_HOME` and `AUT_CONFIG` |
| `/tmp/aut.log`, `/tmp/aut.err` | scan output from the shipped launchd plists. `/tmp` can be read by other users; change the paths in the plists if that matters |

## Collectors

| Name | Reads | Sends | Default |
|---|---|---|---|
| `claude_code` | `~/.claude/projects/**` | usage, prompts, sessions, activity, links | on |
| `codex` | `~/.codex/sessions/**` | usage (with reasoning tokens), prompts, sessions, activity, links | on |
| `pi` | `~/.pi/agent/sessions/**` | usage (with reasoning tokens), prompts, sessions, activity, links | on |
| `cursor` | Cursor IDE `state.vscdb` (`cursor_db_path` overrides) | usage as a lower bound, prompts | on |
| `cursor_cli` | `~/.cursor/chats/*/*/store.db` and the SDK run stores | prompts, sessions, activity, links; no tokens | on |
| `cursor_sdk` | the files matched by `cursor_sdk_manifests` | per-run usage | on, idle until the glob is set |
| `cursor_api` | Cursor's dashboard API (network) | accurate usage for the IDE and the CLI | on, idle until `cursor_api = true` |
| `activitywatch` | the local aw-server (`activitywatch_url`) | not-AFK window intervals with app, category and a redacted title | **off**: add it to `agents` |

**Pick one Cursor token source.** `cursor`, `cursor_sdk` and `cursor_api` overlap, and more
than one counts the same usage twice. `cursor_api` is the accurate one. To keep Cursor prompts
from the local DB beside it, set `cursor_human_only = true`. `cursor_cli` has no tokens and
overlaps none of them.

**ActivityWatch** needs `pg_dsn`. Before anything is sent it drops login window and screen
saver rows, password managers, private browsing and anything in `activitywatch_deny_apps` or
`activitywatch_deny_titles`. Private browsing is found only by its title ("Private Browsing",
"Incognito", "InPrivate") or the watcher's `incognito` flag. A Chromium private window (Brave,
Chrome and similar) shows neither, so its title is sent after redaction. Add such browsers to
`activitywatch_deny_apps`, or set `activitywatch_titles = false`. It redacts credentials, tokens, keys, emails and long base64 runs
from titles and cuts them to 300 characters. Browser URLs are matched locally and never sent.
Put the names of private apps or banks in the config, never in code. Titles are kept for good
unless you set `activitywatch_title_days`, which also deletes ActivityWatch's own older window
events. Full rules: [CONTRACT.md](CONTRACT.md#active-time-agentstime_interval).

## Scheduling

`packaging/` ships units that run `aut scan` every 15 minutes (launchd, Task Scheduler) or 10
minutes (systemd). Setup steps are in [packaging/README.md](packaging/README.md).

The launchd scan also runs at login and on wake, so a laptop that was away for a week catches up
at once. `packaging/launchd/dev.aut.backup.plist` runs `aut backup` daily and at login. For a long
gap, a backup and a new laptop, see [docs/new-laptop.md](docs/new-laptop.md).

For more than the bare scan, run the wrapper `packaging/launchd/aut-scan.sh` instead (macOS
and Linux; on systemd set `ExecStart=/bin/bash %h/.config/agent-usage-tracker/aut-scan.sh`).
Copy it to `~/.config/agent-usage-tracker/`. It needs `nc` and `ssh` on `PATH` when a tunnel is
set. Each run it:

1. opens an SSH tunnel to the sink if one is configured, reusing one already up;
2. runs `aut scan`, and on a failed push leaves `retry-pending` and exits non-zero. On macOS
   `packaging/launchd/dev.aut.retry.plist` watches the marker to retry as soon as the network
   is back. The systemd unit retries a failed push on its own (`Restart=on-failure`);
3. once a day after a delivered scan, runs `aut outcomes`;
4. once a day after that, where `AUT_LABEL=1`, runs `aut label run`;
5. logs every step with timestamps to `scan.log`.

Each daily step is tried once per day, failed or not, and never changes the scan's exit code.

Timer jobs do not inherit your shell environment, so per-machine settings go in
`~/.config/agent-usage-tracker/scan.env`, which the wrapper sources. Keep secrets out of it;
they belong in `config.toml`.

| Key | Meaning |
|---|---|
| `AUT_BIN` | path to `aut` (default `~/.local/bin/aut`) |
| `AUT_TUNNEL_HOST` | `user@host` for `ssh -L`; unset means no tunnel |
| `AUT_TUNNEL_SPEC` | the `ssh -L` argument, e.g. `15432:10.0.0.5:5432`; required with `AUT_TUNNEL_HOST` |
| `AUT_LABEL` | `1` runs the nightly `aut label run` on this machine |

With a tunnel, point `pg_dsn` at `localhost` and the local port.

**Heartbeats.** Every real scan writes one `agents.collector_scan` row per collector over
`pg_dsn`, so a dead collector does not look like a quiet week: `status` is `degraded` when a
collector could not read its source (an expired Cursor token, a stopped aw-server), stored as
`error` on a sink whose CHECK has no `degraded`. `aut outcomes` and
`aut label run` write their own rows (collector `outcomes` and `label`). A heartbeat that
fails to send prints a warning and never fails the scan.

## Local labels (`aut label`)

Optional, Apple silicon. The model reads the text on this machine and only the label is sent.
`daily_note` is the exception: it sends the short note the model writes from a day's counts.

1. Fetch the models you want to compare once (see [The models](#the-models)).
2. `aut label teach --task steer_intent --n 200` writes silver labels.
3. `aut label review --task steer_intent` lets you answer gold labels on a local page.
4. `aut label eval --task steer_intent` scores every model that is on disk, says which it left out
   and how to fetch them, and prints a table of quality, speed and memory, with a mark on the Pareto
   frontier and on models that fail a floor. Scores are saved, so a rerun calls a model only for new
   gold and tuning needs no model.
5. `aut label choose --task steer_intent --model kev-4b` writes your pick, with its calibration and
   thresholds, into `label.json`. Without `--model` it prints the table and a recommendation and
   writes nothing. Without a choice a task runs uncalibrated and every row is marked below
   threshold. `aut label eval --task steer_intent --final --model M` is the last check, on days
   nothing else has read.
6. Add the task to `label_tasks` and set `AUT_LABEL=1` in `scan.env` for the nightly run.

Details and the stored row: [CONTRACT.md](CONTRACT.md#labels-activity_event-kind-label-aut-label).

### Provisional historical baseline

Use existing model labels to explore work patterns without treating them as calibrated results.
The import keeps primary Sonnet categories and direct agent-reviewed signals separate. Uncertain
cases stay unknown. Charts describe estimates, not verified accuracy.

```sh
aut label import-baseline /path/to/private/working-baseline/labels.json  # preview only
aut label import-baseline /path/to/private/working-baseline/labels.json --apply
```

`--apply` appends the entire validated bundle in one transaction through `transport.pg_dsn`.
Repeated imports are idempotent. Rows use `working.steer_intent` and `working.soft_outcome`, so
calibrated task choices, their shares, and daily notes do not consume these estimates. Source,
sampling weight, unknowns, question hashes and agent-review provenance are retained. Review
reasons and local file paths stay on the Mac. No model call or calibration runs during import.
Deploy the companion sink schema and dashboard changes before using `--apply`.
The sink's private `working_label_monthly` and `working_signal_monthly` views feed estimate panels.


### The models

`--model` and `label_model` take these names. Any other name is read as an MLX repo through answer
letters, the way Gemma is.

| Name | Weights | Runs on | Size | s/item | Memory | Right of 20 | Licence |
|---|---|---|---|---|---|---|---|
| `elephant-1` | community: 0x36's 4-bit port of baselabs/elephant-1 | MLX | 5.1 GB | 0.29 to 0.60 | 5.5 GB | 20 | MIT |
| `evalengine-4b` | official LoRA merged on official Qwen3.5-4B, bf16 | MLX | 8.4 GB | 0.19 to 0.35 | 8.8 GB | 20 | Apache-2.0 |
| `clef-flash` | community: mlx-community's 4-bit port of Cloudflare/clef-flash | MLX | 6.2 GB | 0.36 to 0.62 | 5.9 GB | 20 | Apache-2.0 |
| `aurelis-2b` | official q8_0 GGUF | `llama-server` | 2.7 GB | 0.09 to 0.43 | 3.1 GB | 20 | Apache-2.0 |
| `pplx-decider-flash` | official bf16 | MLX | 9.1 GB | 0.13 to 0.33 | 8.6 GB | 19 | Apache-2.0 |
| `kev-4b` | community: ggml-org's q8_0 conversion of jaredpalmer/kev-4b | `llama-server` | 4.5 GB | 0.12 to 0.28 | 5.1 GB | 20 | Apache-2.0 |
| `gemma-4-31b` | community: mlx-community's 4-bit build | MLX | 18.4 GB | 2.75 | 17.7 GB | 20 | Apache-2.0, per its card |

Measured on one Apple silicon Mac, on 20 easy synthetic cases, one model at a time. The low s/item
is aut's own backend after a warm-up; the high one is an earlier run on a busier machine. These
numbers are not a ranking: which model is best on your work is what `aut label eval` is for. Memory
of the two `llama-server` models is the server's RSS, which can miss Metal buffers.
`gemma-4-31b` scored 7 until aut turned thinking off: its chat template opens a thinking channel by
default, and the answer letters were then read at a position where the model expects `<|channel>`.

Fetch once, one at a time. A nightly run never downloads:

```bash
hf download mlx-community/gemma-4-31b-it-4bit
hf download 0x36/elephant-1-9B-MLX-4bit --revision 16de0e500eea4e44fc7db79928c03c53e324bcb0
hf download mlx-community/clef-flash-4bit --revision b5adbe2c3a95200d4bf04a0727c8ec457157d5e1
hf download perplexity-ai/pplx-decider-flash-v1.2 --revision 9ff7cf9838e59df461c276b7699d56b244d2af58
hf download Leptos-ai/Aurelis-2B aurelis-2b-q8_0.gguf --revision 1b56513618a9de9378440d3bca5e4e163f142279
hf download ggml-org/Kev-4B-GGUF Kev-4B-Q8_0.gguf --revision f4624b6bb7072f1bab726a622c1cad3f96030b9e
```

`evalengine-4b` is a LoRA, so it is merged once onto its base and saved under
`~/.local/share/aut/label/models/`. This loads the 9 GB base on the CPU and needs torch for this
command only. After it, the model needs only `mlx-lm`, and you can delete the base from the cache:

```bash
hf download evalengine/decision-4b --revision aadc55517ae8bfb1264fd5932ec452b939361e47
hf download Qwen/Qwen3.5-4B --revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a
# from a checkout:
uv run --isolated --no-project --with ".[merge]" python -m agent_usage_tracker.label.merge evalengine-4b
```

`aurelis-2b` and `kev-4b` need `llama-server` from llama.cpp, build 11669 or newer
(`brew install llama.cpp`; `llama-server --version` shows the build). The official GGUF of
`pplx-decider-flash` does not run on it (HTTP 501), so that model runs on MLX from the official
weights. The `clef-flash` GGUF does run, but its probabilities differ from the MLX build's by up to
0.27 on the same top answers, so aut uses the MLX build.

## What the sink needs

The sink stores the rows and does the reading: aggregates, episodes, active time and its
self-calibration (`time_params`, `app_learned`) all live there, not in aut. It needs:

- for the webhook: an endpoint that checks `X-AUT-Secret` and inserts usage rows
  `ON CONFLICT (event_id, ts) DO NOTHING`;
- for `pg_dsn`: a role that can `INSERT` and `SELECT` on `agents.usage_event`,
  `event_session`, `activity_event`, `time_interval`, `collector_scan`, `commit_outcome`,
  `manual_outcome`, `episode_correction` and `daily_note`, and `SELECT` on the sink-built
  `agents.episode` and `episode_member`, which the review and inspect commands read. Labels are
  read from `activity_event` rows with `kind = 'label'`.

Every table, column and key this client writes is in [CONTRACT.md](CONTRACT.md), along with the
columns it reads from the sink-built episode views. Those views and `episode_link()` are defined
only in the infrastructure repo. A deploy walkthrough is in [DEPLOY.md](DEPLOY.md).

## How it stays correct

- **One row per turn**, keyed by a deterministic `event_id`. A re-scan never double-counts.
- **All four token classes are read**: input, output, cache-read and cache-write, plus Codex
  and Pi reasoning tokens (a subset of output, never added to totals). Summing `input_tokens`
  alone is why Claude Code usage is often undercounted.
- **Only typed prompts count as human turns.** Tool results, subagent transcripts, hook output
  and scripted runs are left out.
- **Cursor's local DB is a lower bound** and often lacks the model id. Use `cursor_api` for
  accurate Cursor numbers.

## Troubleshooting

**`label: no llama-server at ...` or `too old for /v1/systemone`.** `aurelis-2b` and `kev-4b` need
`llama-server` build 11669 or newer. `brew install llama.cpp` or `brew upgrade llama.cpp`, and set
`label_llama_server` to its full path if the nightly job's `PATH` lacks it. A `label eval` leaves
those two out and goes on with the others.

**`label: <model> is not fully in the local Hugging Face cache`.** aut reads the model from the
cache only and needs its config and every weight file. Fetch it once with the `hf` CLI that
comes with `huggingface_hub`:

```bash
uvx --from huggingface_hub hf download mlx-community/gemma-4-31b-it-4bit
```

A download stopped half way fails the same check: run it again until it finishes. If you keep
the cache somewhere else (`HF_HOME`), the timer does not see your shell's value: add
`export HF_HOME=...` to `scan.env`.

**`this needs the Postgres driver`** (also inside `scan heartbeat not delivered: ...`). You
set `pg_dsn` but installed aut without the extra. Reinstall with the `[postgres]` (or
`[label]`) install line above, adding `--reinstall`.

**`note: session/activity/time rows not sent over the webhook`.** The webhook carries usage
only. Set `pg_dsn` as well, then fill history with
`aut backfill --sessions-only --activity-only`. Time rows have no backfill.

**Session rows are missing for old scans although `pg_dsn` is set.** Older versions did not send
session rows while the webhook worked. New scans do. Run `aut backfill --sessions-only` once to
fill the history.

**`webhook delivery failed, using transport.pg_dsn`.** The webhook is broken (often a rejected
secret or an expired Cloudflare Access token) and usage is going over Postgres instead. Nothing is
lost, but fix the webhook. The warning prints once per scan and names the failure.

**`transport.pg_dsn reaches <host> without requiring TLS`.** A warning only: the connection still
goes ahead, and libpq may fall back to plaintext. Add `sslmode=require` to the DSN. On a private
link such as Tailscale, `sslmode=disable` says that is intended and silences it.

**The tunnel.** `AUT_TUNNEL_HOST` without `AUT_TUNNEL_SPEC` makes the wrapper refuse to scan
(exit 2, logged in `scan.log`). An unreachable host is not a failure: the wrapper arms
`retry-pending` and exits 0. On macOS the retry job then tries again. On systemd exit 0 does not
trigger `Restart=on-failure`, so the next try waits for the next timer run. Only the wrapper opens the tunnel:
before running `aut outcomes`, `aut inspect` or another `pg_dsn` command by hand, open it
yourself (`ssh -f -N -L <spec> <host>`).

**`transport.webhook_url is set but webhook_secret is missing`.** launchd and systemd do not
see your shell's environment, so an `env:` value is empty there. Put the secret in
`config.toml` (mode 600) or in the unit's environment. The same goes for every `env:` value,
and most fail without an error: an empty `pg_dsn` quietly stops session, activity, time and
heartbeat rows, empty `cf_access_client_*` values drop the Cloudflare Access headers, and an
empty `repo_key_salt` falls back to this machine's random `repo_key.salt`, so repo, branch,
ticket, PR and SHA keys stop matching other machines.

**`... repo_key.salt is missing, but the sink already holds repo keys from machine ...`.** A new
laptop without the old salt. A new salt would make every repo key differ from the old ones, so
`aut scan` (also a dry run), `aut label` and `aut outcomes` stop and send nothing. Copy the old
`repo_key.salt` next to the state file, or set `repo_key_salt` in `config.toml` to its contents
([docs/new-laptop.md](docs/new-laptop.md)).

**`aut label run` labels nothing.** `label_tasks` is empty by default. List the tasks there or
pass `--task`.

**Squash-merged commits show as `abandoned`.** Set `outcome_fetch = true` (and have `gh`
signed in) on the machine that runs `aut outcomes`.
