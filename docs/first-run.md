# First run

AUT reads the logs your coding agents already save. You can try it and check local coverage before
you choose a database or send any data.

## Start with a local report

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, install the
latest release to get the `aut` command:

```sh
uv tool install agent-usage-tracker
```

Install from `git+https://github.com/rhnfzl/agent-usage-tracker` when you need a change on
`main` before the next release.

```sh
aut setup
aut demo
aut setup --apply
aut summary
aut overview
```

`aut setup` previews the config path and checks local source metadata. Use `--json` for a
receipt with separate settings, source observations and delivery state. A scan receipt
describes locally emitted rows. The local queue can show pending rows, while delivery remains
unverified until you check the sink. A missing source can mean that the agent has yet to log
any work. Run `aut doctor --json` for the full private coverage report.

`aut setup --apply` creates the starter config only when the file is missing. The starter
uses local logs and leaves delivery off. The [config example](../config.example.toml) describes
optional sources and transport settings. The demo uses sample logs in a temporary directory.
It needs no provider account, database or previous setup.

The overview opens a private loopback page. Choose a date range and group by agent, model
or project. It shows when usage was last seen and whether scan evidence is fresh. If an
improvement store exists, the page links to its private report card. Use
`aut overview --no-browser` to get the local URL and Ctrl-C to close the page.
Quota appears after a [local capture](quota.md). The overview reads the cache for each page
request and shows freshness, the reset time and any supported pace estimate.

Use `aut improve inspect --case CASE_ID` to review the private context for a saved case.
The view links text sizes, exact repeat reads and observed compactions to their frozen events.
Captured text sizes use UTF-8 bytes and can be clipped. Actual model context tokens are unknown.

## Export usage and compare models

```sh
aut --agents claude_code,codex,cursor_cli,pi export --out usage.csv
aut summary --period week --group-by model
aut summary --since 2026-06-01 --until 2026-07-01 --group-by project --json
```

Local export needs neither a config file nor a database. Cursor CLI contributes prompt rows. For Cursor
token counts, choose one source from the [collector reference](reference.md#collectors).
An unreadable source makes export exit nonzero. Check the exit code before treating a CSV as complete.

Dates use the configured ledger time zone. The end date is exclusive. JSON reports unknown counts as
`null` and lists records with missing values in `unknown_events`. Project summaries can contain names.

`aut sync-prices` refreshes local API list rates. The summary shows the known estimated subtotal, the
total estimate when coverage allows it, and any costs recorded in the logs.
Use `aut summary --compare-prices MODEL_A MODEL_B` to price the same observed token mix at two rates.

Use `aut statusline` for cached quota, reset times, and scan freshness. See the [quota guide](quota.md)
for Claude Code statusline input and optional local notices.

## Keep history in your own database

Install the PostgreSQL extra:

```bash
uv tool install "agent-usage-tracker[postgres]"
```

Create `~/.config/agent-usage-tracker/config.toml` with your machine label and database connection:

```toml
machine = "laptop"
agents = ["claude_code", "codex", "cursor_cli", "pi"]

[transport]
pg_dsn = "env:AUT_PG_DSN"
```

Set `AUT_PG_DSN` to your provisioned database connection.
The database needs AUT's tables and views. The [sink stack](../src/agent_usage_tracker/sink_stack/README.md) provides them with one `docker compose up`;
the [deploy runbook](../DEPLOY.md) describes the sink contract.
Set `ledger_timezone` to the sink's `AUT_TZ`.
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
Native JSONL collection drains a finite file snapshot, including all capped chunks present when each file is admitted.
Later appends, new files and replaced files wait for the next run.
On POSIX, acknowledged closed Cursor CLI stores skip SQLite reads while database and WAL metadata stay unchanged.

Webhook-only scans keep session, activity, and time rows in a private local delivery queue.
`aut status` shows pending rows. After configuring PostgreSQL, scans replay them automatically;
`aut retry-delivery` also retries the queue directly. Keep the state directory in your backups.

For a full configuration, start from [`config.example.toml`](../config.example.toml).
Put secrets in your local configuration or environment, never in this repository.
Scheduled jobs do not inherit your shell environment; configure their secrets separately.

See the [configuration reference](reference.md#configuration) for default paths. Keep the same
`repo_key_salt` across machines when you want their repository identities to match. Back up that salt.

## Schedule delivery to your own sink

Set the transport in your local config. The scheduled job needs access to any environment
variables that the config names. AUT leaves credentials out of scheduler entries. Use a stable
`aut` executable, such as one from `uv tool install agent-usage-tracker`.

```sh
aut setup --schedule
aut setup --apply --schedule
```

The first command previews. The second enables a per-user scan timer, which can start a scan
as soon as the scheduler loads it. It uses launchd on macOS, a systemd user timer on Linux,
and a scheduled task for the current interactive user on Windows. It keeps existing config and
refuses to replace a schedule created outside guided setup. On macOS the scheduled logs are
under `~/Library/Logs`. Linux uses the user journal and Windows uses Task Scheduler history.

On Windows, setup uses a process-local execution policy for the packaged script. Group Policy
can still block it. Use Task Scheduler to run `aut scan` if your administrator permits it.

The guided timer uses the default AUT config and state paths. For a custom setup, an existing
manual timer, or a system without a supported user scheduler, see
[the scheduler templates](../packaging/README.md). Use `aut summary` for local reporting on demand.

`aut setup --uninstall` removes only the scan entry created by guided setup. It keeps config,
scan state and logs. It can remove the managed entry when the config is unreadable.
Custom daily jobs and manual scan timers have their own removal procedures.
