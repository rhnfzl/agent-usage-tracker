# Nightly showcase-metrics refresh (homelab)

Run this on the box that sits next to the sink Postgres (so the read is local). It refreshes
the price map, then regenerates `metrics.json` for the website to pick up. Not live — a nightly
cron is deliberate.

Install with the postgres extra:

    uv tool install "git+https://github.com/rhnfzl/agent-usage-tracker[postgres]"

Crontab (03:15 nightly), writing where the site build reads it:

    15 3 * * *  aut sync-prices && aut report --out /srv/www/data/metrics.json

`aut report` reads the DSN from `transport.pg_dsn` in the aut config on that box. Failures are
safe: `sync-prices` keeps the prior price file if the source is unreachable, and `report`
writes atomically (temp + rename), so the site never sees a half-written file.

(Alternatively, mirror the launchd/systemd units in this directory for `aut scan` and change
the command to the two above.)
