# agent-usage-tracker (`aut`)

Passive, subscription-safe token & model usage tracker for coding agents. It reads the
logs **Claude Code, Codex CLI, Cursor, and Pi already write to disk**, normalizes every
assistant turn into one event, and pushes to a self-hosted sink (an n8n webhook by
default, or Postgres/TimescaleDB directly over Tailscale).

It also has an opt-in `cursor_sdk` collector for **agents built on the Cursor Agent SDK**
(which don't write those logs - the SDK returns usage in-process and the app persists it):
point `cursor_sdk_manifests` at the run `manifest.json` files such an app writes and their
per-run token usage flows through the same pipeline. Off unless configured.

## Why not a proxy?

Because these agents log in with an **OAuth subscription, not an API key** (Claude Max/Pro,
ChatGPT Plus/Pro, Cursor Pro, Pi `/login`). A LiteLLM/Helicone-style proxy only sees
traffic routed through an API key, so it sees *nothing* of your subscription usage. The
durable answer is to read what each tool already records locally. That also means:

- No API key, no proxy, no man-in-the-middle.
- Works fully offline; the on-disk logs are still there when you reconnect.
- Every USD figure is **estimated / API-equivalent** (subscriptions are flat-rate).

## Install (per machine)

```bash
uv tool install git+https://github.com/<you>/agent-usage-tracker      # or: pipx install .
```

Requires Python 3.11+. **Runs on macOS, Linux, and Windows** (agent log paths are resolved
per-OS). Base install has **zero runtime dependencies**. The direct-Postgres fallback is
optional: `uv tool install "agent-usage-tracker[postgres]"`.

On a **shared machine**, the collector reads the running user's `$HOME` by default (it never
scans other users' homes); if you set a `home` override, point it only at your own user's
directory. See `config.example.toml`.

## Configure

Copy `config.example.toml` to `~/.config/agent-usage-tracker/config.toml` and edit it.
**Secrets never live in this repo** - put them in that local file or the environment
(`env:VAR` values are resolved at run time).

```toml
machine = "laptop"                       # role-based label for this machine
agents  = ["claude_code", "codex", "cursor", "cursor_sdk", "pi"]
# cursor_sdk_manifests = "/home/you/projects/**/.state/runs/*/manifest.json"  # opt-in, SDK agents

[transport]
webhook_url    = "https://n8n.example/webhook/llm-usage"
webhook_secret = "env:AUT_WEBHOOK_SECRET"
# pg_dsn       = "env:AUT_PG_DSN"        # optional Tailscale fallback
```

## Use

```bash
aut scan --dry-run     # parse + report, send NOTHING (also your accuracy check)
aut scan               # push new usage since the last run, advance the watermark
aut backfill           # reset the watermark, then push all history
aut status             # config + watermark state
aut export --out f.csv # write all usage as CSV (offline bulk-load / bridge)
```

Run it on a timer (see `packaging/` for launchd + systemd units). Offline runs are safe:
the watermark only advances after a successful push, so nothing is lost and the sink
dedups re-sends by `event_id`.

## How it stays correct

- **One row per assistant turn**, keyed by a deterministic `event_id`; the sink dedups via
  `ON CONFLICT`, so re-scans never double-count.
- **All token classes captured** (input / output / cache-read / cache-write, plus reasoning
  for Codex). The infamous Claude Code "undercount" is just people summing `input_tokens`
  and ignoring the cache fields - we don't.
- **Cursor is experimental** (local DB is a lower bound and often lacks the model id); see
  `CONTRACT.md`.

See `CONTRACT.md` for the event schema and webhook payload.
