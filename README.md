# agent-usage-tracker (`aut`)

[![CI](https://github.com/rhnfzl/agent-usage-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/rhnfzl/agent-usage-tracker/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Runtime dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

Coding agents on a subscription do not tell you what they cost. There is no per-session
invoice line, no dashboard that adds up across tools, and the flat monthly fee hides
everything underneath it.

This reads the logs **Claude Code, Codex CLI, Cursor, and Pi already write to disk**,
normalises every assistant turn into one event, and pushes it to a sink you host yourself
(an n8n webhook by default, or Postgres/TimescaleDB directly over Tailscale).

Two collectors are opt-in because they need something the others do not:

- **`cursor_sdk`**, for agents built on the Cursor Agent SDK. Those write no session logs,
  because the SDK returns usage in-process and the app persists it. Point
  `cursor_sdk_manifests` at the run `manifest.json` files such an app writes and their
  per-run tokens flow through the same pipeline.
- **`cursor_api`**, for accurate Cursor usage. The local `cursor` collector is only a lower
  bound, and the `cursor-agent` CLI writes no token counts to disk at all. This one pulls
  your own per-event usage from Cursor's dashboard API using the locally stored session
  token, which the tool never persists. On a team plan it scopes strictly to your own user
  and re-checks every event, so a teammate's usage is never captured. It makes authenticated
  network calls, so it stays off until you set `cursor_api = true`.

## Why not a proxy?

Because these agents log in with an **OAuth subscription, not an API key** (Claude Max/Pro,
ChatGPT Plus/Pro, Cursor Pro, Pi `/login`). A LiteLLM or Helicone style proxy only sees
traffic that routes through an API key, so it sees *nothing* of your subscription usage.

The durable answer is to read what each tool already records locally. That buys three things:

- No API key, no proxy, nothing sitting in the middle of your traffic.
- It works fully offline. The on-disk logs are still there when you reconnect.
- Every USD figure is **estimated and API-equivalent**, since subscriptions are flat-rate.

## Install (per machine)

```bash
uv tool install git+https://github.com/rhnfzl/agent-usage-tracker      # or: pipx install .
```

Requires Python 3.11+. **Runs on macOS, Linux, and Windows**, resolving agent log paths
per-OS. The base install has **zero runtime dependencies**. Only the direct-Postgres fallback
adds one: `uv tool install "agent-usage-tracker[postgres]"`.

On a **shared machine** the collector reads the running user's `$HOME` and never scans other
users' homes. If you set a `home` override, point it only at your own directory. See
`config.example.toml`.

## Configure

Copy `config.example.toml` to `~/.config/agent-usage-tracker/config.toml` and edit it.
**Secrets never live in this repo.** Put them in that local file or in the environment, where
`env:VAR` values are resolved at run time.

```toml
machine = "laptop"                       # role-based label for this machine
agents  = ["claude_code", "codex", "cursor_api", "pi"]   # pick ONE Cursor source (they overlap)
# cursor_api = true                      # accurate Cursor usage (CLI+IDE) via the dashboard API
# cursor_sdk_manifests = "/home/you/projects/**/.state/runs/*/manifest.json"  # or SDK manifests

[transport]
webhook_url    = "https://n8n.example/webhook/llm-usage"
webhook_secret = "env:AUT_WEBHOOK_SECRET"
# cf_access_client_id     = "env:CF_ACCESS_CLIENT_ID"      # sync from ANYWHERE via Cloudflare
# cf_access_client_secret = "env:CF_ACCESS_CLIENT_SECRET"  #   Access, no VPN (see below)
# pg_dsn       = "env:AUT_PG_DSN"        # optional direct-Postgres fallback
```

**Syncing from anywhere, without a VPN.** If the webhook sits behind Cloudflare Access, set a
service token (`cf_access_client_id` plus `cf_access_client_secret`). The tracker sends them as
`CF-Access-Client-Id` and `CF-Access-Client-Secret`, so a scheduled push clears Access from any
network. On your own LAN or Tailscale you can skip both and point `webhook_url` straight at the
internal n8n.

## Use

```bash
aut scan --dry-run     # parse + report, send NOTHING (also your accuracy check)
aut scan               # push new usage since the last run, advance the watermark
aut backfill           # reset the watermark, then push all history
aut status             # config + watermark state
aut export --out f.csv # write all usage as CSV (offline bulk-load / bridge)
aut sync-prices        # refresh the API-equivalent model price map (LiteLLM -> models.dev)
aut report             # compute showcase metrics from the sink -> metrics.json (needs [postgres])
```

Run it on a timer. See `packaging/` for launchd, systemd, and Task Scheduler units.

Offline runs are safe. The watermark only advances after a push succeeds, so nothing is lost,
and the sink dedups re-sends by `event_id`.

## How it stays correct

- **One row per assistant turn**, keyed by a deterministic `event_id`. The sink dedups via
  `ON CONFLICT`, so a re-scan never double-counts.
- **All four token classes are read**: input, output, cache-read, and cache-write, plus
  reasoning tokens for Codex. Claude Code records cache reads and cache writes as fields
  separate from `input_tokens`, so summing `input_tokens` alone reports a number well under
  the real one. That is where the reported Claude Code undercount comes from.
- **Cursor is experimental.** The local DB is a lower bound and often lacks the model id. See
  `CONTRACT.md`.

`CONTRACT.md` has the event schema and the webhook payload.

## Showcase metrics (derived, opt-in)

`aut report` reads the **unified sink** through `transport.pg_dsn`, which doubles as the
report's read source, and writes a small `metrics.json` for a personal-site showcase.

It is a pure derivation over data you already hold. The collector is unchanged, and **`project`
is never read**, so no project name can reach the output. That is aggregate by construction
rather than aggregate by redaction.

- **Framing.** It reports *ratios and trends*: cache-hit rate, output leverage, adoption
  timeline, agent turns per human turn. Volume shows up once, framed as API-equivalent work
  done on a flat-rate subscription, not as a token scoreboard.
- **Pricing.** `aut sync-prices` builds `model_prices.json` from LiteLLM (primary) and
  models.dev (fallback), both open source, extended here to carry the cache-read and
  cache-write rates this tool needs. Every `$` figure is **notional and API-equivalent**. A
  model missing from the map is reported under `priced_coverage`, never guessed.
- **Sessions** are derived at report time by idle gap, 30 minutes by default via `--idle-gap`,
  so the count covers all history without touching the collector.
