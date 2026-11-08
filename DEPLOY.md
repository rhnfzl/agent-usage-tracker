# Deploy runbook

Two halves: the **collector** (this repo, on every machine) and the **sink** (one central
install of Postgres/TimescaleDB). Collectors write to it over `pg_dsn`. An n8n webhook in front
is optional: it carries usage rows only, for machines with no route to Postgres. The sink's own
files live in a separate infrastructure repo; this page names what they must provide.

## A. Sink (once)

| Piece | Purpose |
|---|---|
| schema SQL | database `agents`: every table in [CONTRACT.md](CONTRACT.md) (`usage_event` as a hypertable, `event_session`, `activity_event`, `time_interval`, `collector_scan`, `commit_outcome`, `manual_outcome`, `episode_correction`, `daily_note`, `git_commit_day`), the views built from them (`usage_event_dedup`, `time_interval_public`, `episode`, `episode_member`, `label_latest`, `git_commits_daily`, ...), compression and retention |
| grants SQL | least-privilege `agents_writer` (INSERT and SELECT, used by `pg_dsn`, and by n8n if you run the webhook) and `agents_ro` (read-only, used by Grafana; sees `time_interval_public`, never titles) |
| n8n workflow (optional) | webhook -> check `X-AUT-Secret` -> `INSERT ... ON CONFLICT (event_id, ts) DO NOTHING` into `agents.usage_event` |
| Grafana dashboard | reads the views as `agents_ro` |

### 1. Database and roles (as superuser)

```sql
CREATE DATABASE agents;
\c agents
-- generate strong passwords and keep them out of any repo
CREATE ROLE agents_writer LOGIN PASSWORD 'REDACTED';   -- the collectors' pg_dsn, and n8n if you run the webhook
CREATE ROLE agents_ro     LOGIN PASSWORD 'REDACTED';   -- Grafana
\i /path/agents_schema.sql
\i /path/agents_grants.sql
```

Before upgrading a collector that writes partial Git counts, add
`agents.git_commit_day.complete boolean NOT NULL DEFAULT true` and update
`agents.git_commits_daily` to return `day, commits, complete`.
The view must retain partial increases, preserve unchanged complete counts, and let a newer complete
scan reset the baseline. See [the Git count contract](CONTRACT.md#commits-per-day-agentsgit_commit_day-aut-git-days).
Upgrade website readers before publishing missing or partial Git coverage.

### 2. n8n (optional: webhook transport, carries usage rows only)

Skip this when every machine can reach Postgres, directly or through a tunnel: `pg_dsn` alone
carries every row. If you change the `agents_writer` password, also update this n8n credential
when you run the webhook.

1. In the n8n UI create two credentials: **Header Auth** "LLM Usage Token" (header
   `X-AUT-Secret` = a shared secret) and **Postgres** "Postgres agents" (db `agents`, user
   `agents_writer`).
2. Import the workflow (`n8n import:workflow --input=<workflow>.json`), pick the two
   credentials and activate it.
3. Put `https://n8n.<domain>/webhook/llm-usage` behind Cloudflare Access with a service token,
   so machines can push from any network.

Everything that is not a usage row (sessions, activity, links, time, heartbeats, outcomes,
labels) goes over `pg_dsn`, and usage falls back to it when the webhook fails. Use
`sslmode=require` for a remote host, or `sslmode=disable` on a private link such as Tailscale;
with neither, aut warns on each run, because libpq can fall back to plaintext. A machine that should send those rows needs a route to Postgres:
Tailscale, a VPN, or the wrapper's SSH tunnel (`AUT_TUNNEL_HOST`, `AUT_TUNNEL_SPEC`).

### 3. Grafana

Add an `agents` datasource with a fixed UID (provisioned, not made in the UI), pointing at db
`agents` as `agents_ro`, and import the dashboard. aut publishes nothing itself; every
aggregate is a view in the sink.

### 4. Public stats JSON (optional)

Schedule a rollup that writes a small static JSON from a usage view and serve it through your
tunnel. Label every USD figure "estimated". Never expose titles or anything only
`agents_writer` can read.

## B. Collector (every dev machine)

See [README.md](README.md) and [packaging/README.md](packaging/README.md). Short version:

```bash
uv tool install "agent-usage-tracker[postgres] @ git+https://github.com/rhnfzl/agent-usage-tracker"
mkdir -p ~/.config/agent-usage-tracker
cp config.example.toml ~/.config/agent-usage-tracker/config.toml   # machine, agents, [transport]
chmod 600 ~/.config/agent-usage-tracker/config.toml
aut status                                   # transports and watermarks
aut scan --dry-run                           # check the numbers, sends nothing
aut backfill                                 # optional one-time history push
aut backfill --sessions-only --activity-only # optional: session and activity history over pg_dsn
# then install the timer, or the aut-scan.sh wrapper for the daily outcomes and label steps
```

On the machine with the clones, set `outcome_fetch = true`. On the Apple silicon machine with
the label model, install the `[label]` extra and set `AUT_LABEL=1` in `scan.env`.

## C. Verify end-to-end

```bash
# 1. webhook only: a fake event lands a row (the secret comes from the env, so it stays out of shell history)
curl -sS -X POST https://n8n.<domain>/webhook/llm-usage \
  -H "X-AUT-Secret: $AUT_WEBHOOK_SECRET" -H 'content-type: application/json' \
  -d '{"events":[{"event_id":"t1","ts":"2026-07-18T00:00:00Z","machine":"test","agent":"codex","kind":"agent","model":"x","provider":"openai","input_tokens":1,"output_tokens":1,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":null,"total_tokens":2,"est_cost_usd":null,"project":null}]}'
# 2. psql agents -c "select count(*) from agents.usage_event;"
# 3. after a real scan, one heartbeat row per collector:
#    psql agents -c "select collector, status, finished_at from agents.collector_scan order by finished_at desc limit 10;"
```
