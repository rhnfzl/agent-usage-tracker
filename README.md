# agent-usage-tracker (`aut`)

[![CI](https://github.com/rhnfzl/agent-usage-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/rhnfzl/agent-usage-tracker/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11--3.15-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

**Stop guessing how many tokens your coding agents use and what they actually do.**

`aut` reads existing Claude Code, Codex, Cursor, and Pi logs. Export usage to a local CSV, or keep usage and sessions across machines in a database you control.
Optional local models classify the work as building, debugging, planning, or reviewing.

Use it to answer:

- Which agents and models do I use, and how many tokens do they consume?
- How much work involves building, debugging, planning, or reviewing?
- Which sessions belong to the same piece of work, and what became of it?
- Do I have a copy of my session logs if this laptop fails?

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, try a local export:

```bash
uvx --from git+https://github.com/rhnfzl/agent-usage-tracker aut --agents claude_code,codex,cursor_cli,pi export --out usage.csv
```

AUT is a collector and CLI. Dashboards and websites read your database; this repository does not ship a hosted dashboard.

[Quick start](#quick-start) · [Collectors](#collectors) · [Local labels](#local-labels-aut-label) · [Privacy](#privacy-model) · [Documentation](#documentation)

## What you can track

| Area | What AUT records | What you need |
|---|---|---|
| Usage | Input, output, cache-read, and cache-write tokens; model; project basename | Agent logs, or the optional Cursor API |
| Sessions and activity | Human prompts, turns, interrupts, tool counts, commits, PR links, and related sessions | PostgreSQL |
| Active time | ActivityWatch intervals and manual time entries | PostgreSQL; ActivityWatch is opt-in |
| Outcomes | Commit merge/revert/rework states and your own session verdicts | PostgreSQL and local Git clones |
| Local labels | Work categories, instruction types, and inferred outcomes | Saved model setup and local weights |
| Daily commits | Your commits across local and remote clones, deduplicated across copies | PostgreSQL and configured Git roots |
| Backups | Encrypted, deduplicated copies of logs, AUT configuration, and label files | A configured restic repository |

**Token counts are usage, not your subscription bill.** Dollar figures derived from them are estimated API-equivalent costs.
Local collection works with subscription logs without adding a proxy or requiring an API key.

## Quick start

Python 3.11 to 3.15; macOS, Linux, and Windows.
The base install uses the standard library, plus `tzdata` on Windows.

### Try a local export

Install with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install git+https://github.com/rhnfzl/agent-usage-tracker
aut --agents claude_code,codex,cursor_cli,pi export --out usage.csv
```

No database or configuration file is required for this export.
It reads your current user's logs and writes usage rows to a local CSV.
Cursor CLI contributes prompt rows, not token counts; choose a Cursor token source below for those.
An unreadable source makes export exit nonzero, so a partial file cannot silently count as a complete export.

### Keep history in your own database

Install the PostgreSQL extra:

```bash
uv tool install "agent-usage-tracker[postgres] @ git+https://github.com/rhnfzl/agent-usage-tracker"
```

Create `~/.config/agent-usage-tracker/config.toml` with your machine label and database connection:

```toml
machine = "laptop"
agents = ["claude_code", "codex", "cursor_cli", "pi"]

[transport]
pg_dsn = "env:AUT_PG_DSN"
```

Set `AUT_PG_DSN` to your provisioned database connection.
The database needs AUT's tables and views; the [deploy runbook](DEPLOY.md) describes the sink contract.
On macOS and Linux, protect the configuration with `chmod 600`.
On Windows, restrict its directory permissions to your account.

```bash
aut status
aut scan --dry-run
aut scan
```

The dry run parses logs and reports totals without sending rows.
Real scans advance their watermark only after delivery succeeds.
Repeated delivery is safe because the sink deduplicates event IDs.

Webhook-only scans keep session, activity, and time rows in a private local delivery queue.
`aut status` shows pending rows. After configuring PostgreSQL, scans replay them automatically;
`aut retry-delivery` also retries the queue directly. Keep the state directory in your backups.

For a full configuration, start from [`config.example.toml`](config.example.toml).
Put secrets in your local configuration or environment, never in this repository.
Scheduled jobs do not inherit your shell environment; configure their secrets separately.

### Configuration

The [configuration reference](docs/reference.md#configuration) lists defaults, environment overrides, and local state files.
Use the same secret `repo_key_salt` across machines if their repository identities must match.
Preserve that salt in backups; replacing it breaks those cross-machine matches.

## Collectors

| Agent | Source | Coverage and limits |
|---|---|---|
| Claude Code | `~/.claude/projects/` | Usage, prompts, sessions, activity, and links |
| Codex | `~/.codex/sessions/` | Usage, reasoning tokens, prompts, sessions, activity, and links |
| Pi | `~/.pi/agent/sessions/` | Usage, reasoning tokens, prompts, sessions, activity, and links |
| Cursor CLI | `~/.cursor/chats/` and SDK run stores | Prompts, sessions, activity, and links; no token counts |
| Cursor IDE | Local `state.vscdb` | Prompt records and a lower bound on usage |
| Cursor API | Your dashboard account, explicitly enabled | Token usage for the IDE and CLI; no prompt records |
| Cursor SDK | Configured run manifests | Per-run usage from your SDK applications |
| ActivityWatch | Your local server, explicitly enabled | Active-window intervals; review title privacy settings first |

**Choose one Cursor token source:** `cursor`, `cursor_api`, or `cursor_sdk`.
Combining them can double-count usage. `cursor_cli` has no tokens and can run beside any of them.
To collect IDE prompts beside API usage, add `cursor` with `cursor_human_only = true`.

The [collector reference](docs/reference.md#collectors) covers source paths, defaults, and ActivityWatch filtering.
Collectors use the running user's home; they do not scan other users' accounts.

## Local labels (`aut label`)

Optional decision models turn session evidence into categories such as building, debugging, planning, and review.
They can also classify your instructions and infer whether a piece of work is resolved, blocked, or still open.

Local estimates read Claude Code, Codex, Cursor CLI, and Pi inputs.
Prompts and final replies stay inside the episode's time bounds.
Daily work also stops at local midnight, so shared sessions cannot mix separate work days.
Cursor replies are omitted because their stores lack reliable per-reply timestamps.
Cases without sufficient evidence stay unclassified; uncertain model decisions stay unknown.

| Workflow | Use it for | Setup |
|---|---|---|
| Calibrated labels | Evaluating models against reviewed examples | Review examples, evaluate, and save a model choice |
| Provisional local estimates | Ongoing classification with an existing local recipe | Save `label-local.json`, install the required runtime, and cache its exact weights |

Collection does not require models, annotation, or cloud spending.
Saved local recipes can combine models; they are not automatically fitted or selected for you.
The MLX backends require Apple silicon. GGUF backends use a separately installed `llama-server`.
AUT never downloads weights during classification.
An optional [writing attribute](docs/local-labels.md#optional-writing-attribute) records explicit
requests for prose deliverables alongside the main work category, using a separate local recipe.

With a saved local recipe:

```bash
aut label run --local-estimates --task work_category
```

For historical work, prepare a job and resume its saved ID:

```bash
aut label replay --task work_category --since 2026-01-01 --until 2026-02-01
aut label replay --job JOB_ID --apply --budget-s 600
```

Preparation previews coverage and freezes private inputs without running models or sending labels.
The end date is exclusive. Resume the same job until `complete` is true; progress follows successful batch delivery.
Frozen jobs keep their original inputs when new readers are installed.
Mature-outcome catch-up is separately opt-in through `label_mature_outcomes`; it is disabled by default.

See the [local-label guide](docs/local-labels.md) for model setup, ensembles, cutoffs, calibration, and replay privacy.
Provisional charts describe estimates, not verified classification accuracy.

### The models

The [model reference](docs/local-labels.md#the-models) lists supported backends, pinned downloads, runtime needs, and synthetic timing measurements.
Choose and evaluate models for your own tasks; the timing table does not establish classification accuracy.

## Private improvement (`aut improve`)

Optional local diagnosis finds evidence of avoidable recovery and unsupported completion claims.
It saves private evidence, provisional model judgments, scoped patch proposals, and controlled command checks.
These records stay separate from the normal sink. Installed instructions do not change automatically.

```bash
aut improve audit --days 7 --limit 30
aut improve status
```

Local judging uses cached models through the existing label extra.
Read the [private improvement guide](docs/improvement.md) before judging, staging changes, or executing trials.

## Scheduling

Run AUT on each machine whose logs you want to collect.
The supplied timers scan every 15 minutes on macOS and Windows, or every 10 minutes on Linux.
The macOS scan also catches up at login and after sleep.

The optional wrapper adds SSH tunnelling, retries, daily commit outcomes, and local labels.
Update the installed package before copying a new wrapper; the wrapper calls `aut scan-job`.
The scheduler prevents overlapping runs and records daily success only after each step completes.
Failed or unfinished daily steps retry after the next successful scan.
Local scheduled labels require `AUT_LABEL=1` and `AUT_LABEL_MODE=local` in `scan.env`, plus saved recipes.
An interrupted historical replay resumes when you invoke its saved job again; preparing a job does not install a scheduler.

Follow the [timer setup](packaging/README.md) and [wrapper reference](docs/reference.md#scheduling).

### Keep scripted Cursor work out of your human counts

Use `aut cursor-run --model MODEL --workspace /path/to/workspace < prompt.txt` for scripted requests.
It registers the chat as automated before sending the prompt through your installed `cursor-agent` account.
Registered prompts do not increase human steer counts; automated turns and child links remain available.
This command can spend your account's credits. Normal collection never invokes it.
Direct Cursor calls bypass this registration; existing database records require a separate audited repair.

### Back up your logs

`aut backup` uses restic to copy agent logs, AUT configuration, label files, and the repository-key salt.
The shipped macOS backup job runs daily at 02:30 and at login.
Backup destinations, passwords, retention, and any offsite copy require your own setup.
The database needs its own backup.

Follow [backup and laptop recovery](docs/new-laptop.md) before relying on recovery.
`aut status` reports the age of the last good backup; `aut backup --check` checks the repository.

## Privacy model

AUT sends rows only to the sink you configure. It publishes nothing itself.
Normal collection sends counts and identifiers, not prompt text, tool output, commit messages, or full local paths.
Repository identities and references use salted HMAC keys.

Some metadata remains readable: project basenames, session IDs, commit SHAs, skill names, and tool counts.
ActivityWatch can send redacted window titles unless you turn titles off; app names are still sent.
The optional `daily_note` task sends a generated note.

Local replay jobs retain redacted input text on disk. Keep them and your evaluations outside the public source repository.
The optional teacher command sends sampled Claude Code text through `claude -p`; local classification does not need it.
Cursor API collection and `cursor-run` also make account-authenticated network calls when explicitly used.

Read the [full privacy and network-call reference](docs/reference.md#privacy-model) before enabling optional collectors or model workflows.

## Commands

| Start with | Purpose |
|---|---|
| `aut status` | Check configuration, progress, and backup age |
| `aut scan --dry-run` | Inspect parsed totals without sending rows |
| `aut scan` | Deliver new usage and activity |
| `aut export --out usage.csv` | Export local usage without a sink |
| `aut git-days --dry-run` | Preview daily commit counts across configured clones |
| `aut git-days --local-only` | Collect available local commits while remote machines are offline; mark incomplete coverage |
| `aut inspect --day YYYY-MM-DD` | Inspect the local evidence behind a day's episodes |
| `aut backup --check` | Verify your configured restic repository |

Use `aut --help` or `aut <command> --help` for options.
The [full command reference](docs/reference.md#commands) includes backfills, outcomes, review pages, and model evaluation.

## Documentation

| Guide | Read it when you want to... |
|---|---|
| [Configuration example](config.example.toml) | Choose sources, transports, and optional features |
| [Command and configuration reference](docs/reference.md) | Look up commands, defaults, local files, privacy rules, or errors |
| [Local labels and models](docs/local-labels.md) | Configure decisions, evaluate models, or classify historical work |
| [Scheduling](packaging/README.md) | Install macOS, Linux, or Windows timers |
| [Backup and recovery](docs/new-laptop.md) | Preserve logs and recover on another machine |
| [Deploy runbook](DEPLOY.md) | Provision a self-hosted sink and connect collectors |
| [Data contract](CONTRACT.md) | Build database views, dashboards, or a website from AUT rows |

## Development

From a checkout:

```bash
uv venv
uv pip install -e ".[test,postgres]" ruff==0.15.11 mypy==2.4.1
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync mypy
```

The [CI workflow](.github/workflows/ci.yml) tests Linux, macOS, and Windows across supported Python versions.
SQL integration tests require an isolated PostgreSQL 16 database through `AUT_TEST_PG_DSN`; otherwise those tests skip.
Use synthetic fixtures in public contributions. Keep session logs, personal labels, and private benchmarks out of commits.

MIT licensed. See [LICENSE](LICENSE).
