---
title: CodexBar learnings for agent-usage-tracker
type: reference
status: draft
last_reviewed: 2026-07-19
---

# What CodexBar does, and what we can borrow

Source: [steipete/CodexBar](https://github.com/steipete/CodexBar) (macOS menu-bar app, Swift). Researched via
DeepWiki (`mcp__deepwiki__ask_question` / `read_wiki_structure` against `steipete/CodexBar`) and
`docs/providers.md` / `README.md` on GitHub, 2026-07-19.

CodexBar is a **live quota/usage dashboard** (polls provider APIs and local files, shows menu-bar
icon + countdowns). We are a **passive log-parser** (turn local session logs into token-usage
events, no live polling, no OAuth). The overlap is: both read Claude Code's and Codex's local
JSONL session logs. CodexBar goes further by also hitting OAuth usage endpoints for quota-window
countdowns, and by shipping a maintained cost table. Both of those are directly portable to us
without turning into a live-polling app — we already do a single per-run "log parse", we can just
add "log parse of a couple more, well-documented, log formats" and "look up a token→\$ table"
rather than staying at "no cost data" and "6 formats" forever.

## Architecture overview

CodexBar registers **~57-63 provider IDs**, each tagged with a fetch *strategy*:

- **`web`** — browser cookies (Safari `.binarycookies`, Chrome/Firefox `cookies.sqlite`) or an
  off-screen `WKWebView` scrape.
- **`cli`** — spawn the agent's own CLI (PTY session, or JSON-RPC over stdin/stdout against an
  `app-server` subprocess).
- **`oauth`** — read the CLI's own OAuth token file, call the provider's authenticated usage API.
- **`api`** — user-supplied API key against a provider REST endpoint.
- **`local`** — parse a local log file or SQLite DB directly, no network call.

For Claude Code and Codex specifically, CodexBar tries strategies **in priority order** (best
first, falling back down): OAuth API → web/cookies → CLI PTY/RPC → local file scan. The **local
file scan is the *only* one of these strategies that's just "read a file we already have
read access to"** — everything above it needs either a live subprocess, a browser cookie jar, or
network egress from the collector. That's exactly the strategy our tracker already uses everywhere
(passive, no live calls, no credentials touched), so it's the only tier of CodexBar's design worth
copying wholesale; the OAuth/web/CLI tiers are a different product shape (live quota dashboard)
that would take us out of "safe local log parser" territory.

## Per-agent table

| Agent | How CodexBar reads it | Local-file-parseable? | Can we add a collector? | Effort |
|---|---|---|---|---|
| **Claude Code** | OAuth API (`~/.claude/.credentials.json` → `GET api.anthropic.com/api/oauth/usage`) for quota; **local JSONL scan of `~/.config/claude/projects` and `~/.claude/projects`** for cost | Yes (cost path) | We already have this (`collectors/claude_code.py`) | done — but see quota-window gap below |
| **Codex** | OAuth API (`~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage`) for quota; **local JSONL scan of `~/.codex/sessions/YYYY/MM/DD/*.jsonl` + `~/.codex/archived_sessions/*.jsonl`** for cost | Yes (cost path) | We already have this (`collectors/codex.py`) | done — but see quota-window gap below |
| **Pi (agent sessions)** | Local JSONL scan, `~/.pi/agent/sessions/**/*.jsonl` | Yes | We already have this (`collectors/pi.py`) | done |
| **Cursor (IDE)** | Web cookies (`cursor.com`/`cursor.sh`) → dashboard API | No (needs a live browser session) | We already have a local-DB collector (`collectors/cursor.py`, `cursor_sdk.py`) that CodexBar doesn't attempt — ours is arguably better for local-only use | n/a |
| **Windsurf** | Web localStorage/SQLite session bundle, falls back to reading `state.vscdb` (VS Code state DB) directly | Partially — `state.vscdb` fallback is a local SQLite file | New collector: parse `state.vscdb` (path varies by OS/edition) | Medium — schema for the usage blob inside `state.vscdb` isn't documented publicly; needs reverse-engineering a key |
| **opencode / opencode Go** | Web cookies → `opencode.ai` dashboard RPC; **opencode Go falls back to local SQLite `~/.local/share/opencode/opencode.db`** | Yes (Go variant, local fallback) | New collector: read `opencode.db` directly | Medium — SQLite is friendlier than JSONL scraping, but table/column names need inspection |
| **Gemini CLI** | OAuth-backed Google quota API (`retrieveUserQuota`) using the CLI's own gcloud/OAuth credentials | No (API only) | Not without touching stored OAuth creds — out of scope for passive parsing | High / skip |
| **Copilot** | GitHub device-flow OAuth → `api.github.com/copilot_internal/user` | No | Same as Gemini — API-only | High / skip |
| **Amp** | Local `amp usage` CLI output (parsed), or API token, or browser-cookie fallback | Partially (CLI output, not a file) | Could shell out to `amp usage --json` if it exists, but that's "run a CLI" not "parse a log" — different shape than our other collectors | Medium, different pattern |
| **JetBrains AI** | Local XML file `AIAssistantQuotaManager2.xml`, auto-detects most recent IDE | Yes | New collector: parse that XML for quota (no local cost/token log though — JetBrains AI doesn't expose a token-level JSONL like Claude Code/Codex do) | Low-Medium, quota-only |
| **Kiro** | Shells out to `kiro-cli chat --no-interactive "/usage"` and parses ANSI text output | No (no log file, CLI-invocation only) | Skip — fragile ANSI-scraping pattern CodexBar itself treats as a last resort | Skip |
| **Aider, Windsurf-web, most others (Devin, z.ai, Kimi, OpenRouter, LiteLLM, Zed, Warp, etc.)** | All API-key or browser-session based | No | Out of scope — these need live credentials, not log parsing | Skip |

Full 63-provider table is in `docs/providers.md` on the CodexBar repo if we ever want to revisit;
not reproduced here since almost all of it is API/cookie-based and out of scope for a passive
parser.

## Quota-window tracking (the standout feature we don't have)

We only count tokens. CodexBar additionally tracks **subscription quota windows** — "37% of your
5-hour Claude window used, resets in 2h14m" — which is the number people actually watch day to
day.

- **Claude**: `GET https://api.anthropic.com/api/oauth/usage` (Bearer token from
  `~/.claude/.credentials.json`, same file our collector could read) returns a JSON blob keyed by
  window name: `five_hour`, `seven_day`, `seven_day_sonnet`, `seven_day_opus`,
  `seven_day_routines`, plus `extra_usage` (overage spend, in cents) and `limits` (model-scoped
  weekly caps). Each window has `utilization` (0-100%) and `resetsAt` (ISO8601). CodexBar maps
  these into a generic `RateWindow{usedPercent, windowMinutes, resetsAt, resetDescription}` struct
  and just formats `resetsAt` as a countdown.
- **Codex**: same shape via `GET chatgpt.com/backend-api/wham/usage` +
  `.../wham/rate-limit-reset-credits`, auth from `~/.codex/auth.json`.
- Both of these are a **single authenticated GET** using a token we already have on disk (same
  credentials file our existing collectors could read for identity) — this is not "log parsing"
  but it's also not a heavyweight integration; it's one HTTP call per agent per poll, using a
  token file that's already local and already trusted (the CLI itself reads it to authenticate
  every session).

**High value, but note the boundary shift.** This is authenticated OAuth polling, not passive
file-parsing — adopting it moves the tracker past its "no live calls, no credentials" default (the
same boundary the opt-in `cursor_api` collector already crosses, off by default). So treat quota
tracking as a **separate opt-in feature / future product decision**, not a drop-in to the passive
core. Given that, it's a small, well-scoped addition (2 read-only GETs, reusing credential files we
can already locate) that gives something no amount of JSONL parsing can: "how much of my 5-hour
window is left right now."

## Cost / pricing table

We currently have **no pricing table at all** — `est_cost_usd` exists as a field on `Event`
(`src/agent_usage_tracker/event.py:53`) but nothing populates it from token counts. CodexBar:

- Prioritizes pricing metadata from **[models.dev](https://models.dev)** (a community-maintained,
  freely-fetchable JSON catalog of model pricing/context-window/capabilities — no auth needed).
- Falls back to a **bundled pricing table** (`Sources/CodexBarCore/Vendored/CostUsage/CostUsagePricing.swift`)
  when models.dev doesn't have an entry, with `inputCostPerToken` / `outputCostPerToken` /
  `cacheReadInputCostPerToken` (and tiered thresholds for models with volume pricing).
- Normalizes model name strings before lookup: strips date suffixes
  (`claude-3-5-sonnet-20241022` → `claude-3-5-sonnet`, regex `-\d{8}$`; `gpt-5.4-pro-2026-03-05` →
  `gpt-5.4-pro`, regex `-\d{4}-\d{2}-\d{2}$`), strips vendor prefixes (`anthropic.`, `openai/`),
  and handles Vertex AI's `@version` suffix.

**Second thing worth borrowing**: fetch models.dev's JSON (or vendor litellm's
`model_prices_and_context_window.json`, which is the other common community source and is what
models.dev itself is partly seeded from) as a maintained pricing table, with the same
date-suffix/vendor-prefix normalization regexes before lookup, and a small bundled fallback JSON
for offline use. This is a few-hours job, not a redesign — it plugs straight into the existing
`est_cost_usd` field.

## Local JSONL parsing details worth copying directly

These map 1:1 onto our existing `collectors/claude_code.py` and `collectors/codex.py` — worth
diffing against to see if we're missing any of these edge cases:

- **Claude dedup**: CodexBar keys rows by `(message.id, requestId)` and keeps only the final
  cumulative chunk per key (Claude Code streams partial `usage` objects; only the last one per
  message is the real total). Entries missing either ID are treated as always-distinct
  (`unkeyedRows`). Worth checking our collector dedupes the same way — if it sums every streamed
  usage chunk instead of taking the last one, token counts will be inflated.
  Fields consumed: `type == "assistant"` + `message.usage.{input_tokens, cache_creation_input_tokens,
  cache_read_input_tokens, output_tokens}`, plus `model`, `timestamp`, `message.id`, `requestId`,
  `sessionId`.
  There's a further wrinkle we should check we handle: `cache_creation_input_tokens` isn't a flat
  number in newer Claude Code releases — Anthropic split it into a 5-minute-TTL and 1-hour-TTL
  bucket (`cacheCreate1h` in CodexBar's model), each priced differently. If our collector still
  treats it as one field it may be reading a shape that's since changed upstream, or under/over
  costing that portion once we add pricing.
- **Codex dedup**: tracks `seenFileIds` (don't reprocess a file already ingested), `lastCodexTurnID`
  (resume incremental parsing instead of rescanning), and a `forkedFromId` + `codexShouldPreferTotalDelta`
  heuristic for **forked sessions** (Codex lets you fork a conversation; naive parsing double-counts
  the shared prefix unless you resolve the baseline first). If our Codex collector doesn't handle
  forked sessions, this is a concrete, named bug class to check for, not a hypothetical.
  Fields consumed: `event_msg` entries with `token_count.{total_token_usage, last_token_usage}` for
  `input_tokens`/`cached_input_tokens`/`output_tokens`, model from `turn_context` entries.
- **Pi**: CodexBar's own `PiSessionCostScanner` also parses `~/.pi/agent/sessions/**/*.jsonl` (same
  path our `pi.py` reads) and dedupes via `seenTurnIDs` — good cross-check for our own dedup logic
  since it's an independent implementation of the same format.

## Ranked top things to borrow

1. **Quota-window tracking (Claude 5-hour/weekly, Codex rate limits)** — one authenticated GET per
   agent against an endpoint that already exists, using credential files we can already locate
   (`~/.claude/.credentials.json`, `~/.codex/auth.json`). Biggest capability gap vs. the phrase
   "usage tracker" — this is the number people actually watch. Moderate effort, high value.
2. **A maintained cost/pricing table (models.dev, or litellm's JSON) wired to `est_cost_usd`** —
   we have the field, we have zero rows in it. Fetch + cache the JSON, normalize model strings with
   the same date-suffix/vendor-prefix strip CodexBar uses, bundle a small offline fallback. Low
   effort, closes an existing hole rather than adding a new one.
2a. Do #1 and #2 together first — they're both "read a file/endpoint we already have access to
    and stop leaving a struct field at `None`," not new architecture, so they're the two highest
    value-per-hour items on this whole list.
3. **Audit our Claude/Codex dedup logic against CodexBar's named edge cases** — streamed-chunk
   over-counting (Claude), forked-session double-counting (Codex), and the split 5-min/1-hour
   cache-creation-token buckets. These are concrete correctness bugs to check for, not
   speculative hardening — 30-60 minutes of reading our own collectors against this list is worth
   doing before adding new collectors.
4. **New collector: opencode Go's local SQLite** (`~/.local/share/opencode/opencode.db`) — same
   "local file, no credentials" shape as our existing collectors, medium effort (needs schema
   inspection since it's undocumented).
5. **New collector: JetBrains AI's local XML** (`AIAssistantQuotaManager2.xml`) — quota-only (no
   token-level log to parse), low-medium effort, lower priority than #4 since it doesn't feed
   token-usage events, only a quota gauge.

Deliberately **not** recommending: Windsurf (`state.vscdb` fallback is undocumented and lower
value), Amp (`amp usage` CLI-output scraping is a different collector shape than "parse a log
file"), Gemini CLI/Copilot/Kiro/everything else in the 63-provider table (all API-key, OAuth, or
browser-cookie based — out of scope for a passive local-log parser without changing what kind of
tool this is).

## Which new agents we could support next

**opencode (via its Go variant's local SQLite `opencode.db`)** is the only clearly local-file-parseable
agent CodexBar supports that we don't already cover — everything else in its 63-provider list
needs either a live API key, OAuth token, or browser cookie jar, which would turn us from a
passive log-parser into something closer to CodexBar itself.
