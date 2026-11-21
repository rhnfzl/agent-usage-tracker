# The aut sink

One Docker Compose stack holds your history and dashboards:

- `db`: TimescaleDB with the aut schema.
- `schema`: applies the schema on every `up`, then exits. A newer aut release updates an existing database.
- `refresh`: refreshes the derived views (active time, episodes, cost) every hour.
- `grafana`: the two aut dashboards, read through the `agents_ro` role, which never sees window titles.

The same stack runs in two places:

- **This machine only.** Keep the default `AUT_BIND=127.0.0.1`.
- **A homelab.** Run it on the server. Set `AUT_BIND` to the address your other machines reach it on, such as a LAN or Tailscale address.
  Every laptop then sends its rows there. Never expose it to the internet.

## Start it

You need Docker with Compose v2.

```bash
git clone https://github.com/rhnfzl/agent-usage-tracker
cd agent-usage-tracker/src/agent_usage_tracker/sink_stack
cp env.example .env && chmod 600 .env
$EDITOR .env          # four passwords (openssl rand -hex 24) and AUT_TZ
docker compose up -d --wait
```

Grafana is at `http://<AUT_BIND>:<AUT_GRAFANA_PORT>` (default port 3000). Sign in as `admin` with `AUT_GRAFANA_ADMIN_PASSWORD`.
The dashboards are in the "Coding agents" folder.

## Connect each collector

Install aut with the PostgreSQL extra on every machine you code on:

```bash
uv tool install "agent-usage-tracker[postgres] @ git+https://github.com/rhnfzl/agent-usage-tracker"
```

In `~/.config/agent-usage-tracker/config.toml`:

```toml
machine = "laptop"
ledger_timezone = "Europe/Berlin"   # the same value as AUT_TZ

[transport]
pg_dsn = "env:AUT_PG_DSN"
```

Set `AUT_PG_DSN=postgresql://agents_writer:<AUT_WRITER_PASSWORD>@<host>:<AUT_PG_PORT>/agents?sslmode=disable`.
`sslmode=disable` is only safe on this machine or a private network such as Tailscale.
Then run `aut scan --dry-run` and `aut scan`.
`aut git-days` and `aut history-steers` stop if `ledger_timezone` differs from the sink's `AUT_TZ`.

## Know this

- **Upgrade:** pull the new aut version, then `docker compose up -d --wait`. The `schema` service applies the changes and updates the TimescaleDB extension.
  A change of Postgres major version (for example `pg17` to `pg18` in the image tag) needs a `pg_dump` and a restore; aut's release notes will say so.
- **Time zone:** `AUT_TZ` decides what a "day" is in the views and in Grafana's hour and day panels. Choose it once.
  Rows already written keep the days of the zone they were counted in, so `apply.sh` refuses a new `AUT_TZ`; `AUT_TZ_FORCE=1` switches anyway.
  The `usage_daily` rollup always counts UTC days.
- **Passwords:** the writer and read-only passwords follow `.env` on every `up`.
  The Postgres admin and Grafana admin passwords apply on the first start only. To change them later, use
  `docker compose exec db psql -U postgres -c "ALTER ROLE postgres PASSWORD '...'"` and
  `docker compose exec grafana grafana cli admin reset-admin-password ...`, then update `.env`.
- **Network:** with a LAN or Tailscale `AUT_BIND`, every host that reaches it can try to log in to Postgres, including as `postgres`, and Grafana runs over plain HTTP.
  Docker's published ports also bypass host firewalls such as ufw. Prefer a Tailscale address over a LAN address.
- **Cost panels:** the cost panels stay empty until `agents.model_price` has rows. aut does not fill that table yet.
- **Backups:** your history is in the `pgdata` volume. Back it up with `docker compose exec db pg_dump -U postgres agents`.
- **Your own Postgres:** skip Compose and run `apply.sh` with the libpq environment (`PGHOST`, `PGUSER`, ...) of a superuser on a TimescaleDB server.
  It needs `psql` 15 or newer. Without `AUT_WRITER_PASSWORD` and `AUT_RO_PASSWORD`, it leaves existing role passwords alone.
  Run `sql/refresh.sql` as that superuser at least once a day.
