# Deploy runbook

Two halves: the **collector** (this repo, per machine) and the **sink** (homelab repo,
one central install reusing n8n + Postgres/TimescaleDB + Tailscale).

## A. Sink (homelab, once)

Server-side artifacts live in the homelab repo:

| File | Purpose |
|---|---|
| `ansible/roles/infra-lxc/files/agents_schema.sql` | database `agents`, hypertable, continuous aggregate, compression + retention |
| `ansible/roles/infra-lxc/files/agents_grants.sql` | least-priv `agents_writer` + `agents_ro` grants (mirrors `whoop_grants.sql`) |
| `config/n8n/llm-usage-ingest.json` | webhook -> validate -> `ON CONFLICT` insert |
| `config/grafana/agents-usage-dashboard.json` | starter dashboard (UID `agents-usage`) |

### 1. Database + roles (CT 103, as superuser)

```sql
CREATE DATABASE agents;
\c agents
-- roles: generate strong passwords, store in CT 103 /opt/homelab-automation/.secrets
CREATE ROLE agents_writer LOGIN PASSWORD 'REDACTED';   -- used by n8n
CREATE ROLE agents_ro     LOGIN PASSWORD 'REDACTED';   -- used by Grafana
\i /path/agents_schema.sql
\i /path/agents_grants.sql
```

Or apply the two SQL files through the existing `ansible/roles/infra-lxc/tasks/postgres.yml`
pattern (add them next to the `whoop_*` applies, connected to the `agents` DB).

### 2. n8n (CT 104)

1. In the n8n UI create two credentials: **Header Auth** "LLM Usage Token"
   (header `X-AUT-Secret` = a shared secret) and **Postgres** "Postgres agents"
   (host CT 103, db `agents`, user `agents_writer`).
2. Import the workflow: `n8n import:workflow --input=config/n8n/llm-usage-ingest.json`,
   then open it and pick the two credentials (they match by name), and activate it.
3. Re-export to capture the real credential IDs: `./scripts/export-n8n-workflows.sh`, commit.
4. Gate `n8n.<domain>/webhook/llm-usage` behind Cloudflare Access with a Service Token
   (same pattern as the HA webhooks in `docs/CLOUDFLARE_ACCESS.md`).

### 3. Grafana (CT 103)

Add an `agents` datasource with fixed UID `agents-timescaledb` (provisioning YAML, never
the UI), pointing at db `agents` as `agents_ro`. Import `agents-usage-dashboard.json`.

### 4. Public stats JSON (optional)

Schedule (n8n or cron) a rollup that writes a small static JSON served through the existing
Cloudflare Tunnel; the website fetches it. Label every USD figure "estimated".

```sql
SELECT agent, coalesce(model,'unknown') AS model,
       sum(total_tokens) AS tokens, sum(turns) AS turns
FROM agents.usage_daily GROUP BY agent, model ORDER BY tokens DESC;
```

## B. Collector (every dev machine)

See `packaging/README.md`. Short version:

```bash
uv tool install .                                     # or git+https://...
cp config.example.toml ~/.config/agent-usage-tracker/config.toml   # set machine + webhook
aut scan --dry-run      # verify the numbers
aut backfill            # optional one-time history push
# then install the launchd (mac) / systemd (linux) timer
```

## C. Verify end-to-end

```bash
# 1. fake event lands a row
curl -sS -X POST https://n8n.<domain>/webhook/llm-usage \
  -H 'X-AUT-Secret: <secret>' -H 'content-type: application/json' \
  -d '{"events":[{"event_id":"t1","ts":"2026-07-18T00:00:00Z","machine":"test","agent":"codex","model":"x","provider":"openai","input_tokens":1,"output_tokens":1,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":null,"total_tokens":2,"est_cost_usd":null,"project":null}]}'
# 2. psql agents -c "select count(*) from agents.usage_event;"
# 3. a real scan shows up in Grafana within the timer interval
```
