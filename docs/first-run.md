# First run

See usage from the coding agents you already run. Start with a report on the machine and
user account where those agents save their logs. Local reporting needs no database,
provider account, or model.

## Choose what you need

| I want to… | Start here |
| --- | --- |
| See what AUT shows | [Try the demo](#try-the-demo) with sample logs. |
| See or export usage from my agents | [Run a local report](#run-a-local-report). |
| Get help with my shell, agent profiles, or existing install | [Use the setup prompt](#set-up-with-a-coding-assistant). |
| Keep session history across machines or use Grafana | [Connect your own database](#keep-history-in-your-own-database), then [schedule delivery](#schedule-delivery-to-your-own-sink) if you need it. |
| Classify work or investigate recurring problems | See [local labels](local-labels.md) or the [private improvement workflow](improvement.md). |

See [How I run AUT](how-i-run-aut.md) for one Apple silicon Mac setup with a
self-hosted database, local labels, and proposal review.

## Try the demo

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, run:

```sh
uvx agent-usage-tracker demo
```

Look for two sample records totaling 1,140 tokens. The demo uses synthetic logs in a
temporary directory. It works even when your own agent logs are missing.

## Run a local report

If you already have a working `aut` command, use it. To install the released command
with [uv](https://docs.astral.sh/uv/getting-started/installation/), run:

```sh
uv tool install agent-usage-tracker
```

If the shell cannot find `aut`, run `uv tool dir --bin` and check that directory is on
your shell's `PATH`. To run the report once without installing an `aut` command, replace
`aut` with `uvx agent-usage-tracker`.

```sh
aut setup
aut summary --period week --group-by agent
```

`aut setup` previews the config path and shows which log sources it can find. A source marked
`available` means AUT found the log location. Use the summary to check whether it contains usage.
Use `aut setup --json` to see settings, local scan evidence, and delivery status as separate fields.
A scan receipt records rows AUT produced locally. Check the receiving database to confirm delivery.

Look for a row from an agent you used during the report period. If it is missing:

- If the report has zero events, try `aut summary --period month` or choose dates that include your work.
- If the source is missing, finish one session in the agent and rerun the report.
  For a custom profile, WSL, or a remote machine, check where the agent actually saved its logs.
- If the source is unreadable or still missing, use `aut doctor --json` to check the log paths
  privately. See the [collector reference](reference.md#collectors) for supported paths and overrides.
- Cursor CLI can show activity with unknown token counts. Use the
  [collector reference](reference.md#collectors) to choose one Cursor token source.

If you need a config file for custom settings, `aut setup --apply` creates a starter only
when the file is missing. The starter uses local logs and leaves delivery off.
See the [config example](../config.example.toml) for available settings.

## Set up with a coding assistant

Paste this into a coding assistant that can run commands on the machine with your logs.
If it cannot access that machine, it can give you the commands to run yourself.

```text
Help me set up Agent Usage Tracker (AUT) for my existing workflow.
Read https://github.com/rhnfzl/agent-usage-tracker/blob/main/docs/first-run.md.
If you cannot open the link, ask me for the relevant steps from that page.

Check my OS, shell, and whether this terminal is local, remote, WSL, or in a container.
Check for aut and uv before suggesting an install. Reuse a working AUT install.
If aut is available, use aut setup --json to preview the config and available log sources.
Keep diagnostics brief. Do not print secrets, config contents, or session transcripts.
Ask only for decisions you cannot infer, such as which agents I use or whether I want history across machines.

Recommend one path that fits my goal. Default to a local report.
Show me any install or config changes before applying them. Preserve my existing config,
agent profiles, and timers. Add a database, scheduled delivery, or models only if I choose them.

Verify the setup with aut summary --period week --group-by agent.
Confirm which of my agents show records. If none appear, check the report dates and log location.
If I have no logs yet, use the demo and tell me that it checks AUT with sample data.
End with the command I can use next time and anything still unverified.
```

## Open the private browser overview

Run `aut overview` to open a private page on this computer. Choose a date range and group by agent, model
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

Install the PostgreSQL extra. Keep the same install method you use for AUT:

```bash
uv tool install "agent-usage-tracker[postgres]"
```

Run `aut setup --apply` if you need a config file. Add your machine label and database
connection to the file shown by `aut setup`. Keep any existing source settings.

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
