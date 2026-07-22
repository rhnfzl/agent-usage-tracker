# Data contract

One normalized row per turn: an **assistant turn** (`kind:"agent"`, carries tokens) or a
**human turn** (`kind:"human"`, a prompt you sent, zero tokens). The collector emits it, the
n8n webhook validates it, and the sink stores it. Keep this file and `push.py::COLUMNS` and
the homelab `agents.usage_event` schema in sync.

## Event

```jsonc
{
  "event_id": "<sha256(agent|...)[:32]>",  // deterministic idempotency key
  "ts": "2026-07-18T20:11:04Z",            // ISO-8601, from the source log line (NOT NULL)
  "machine": "laptop",                      // role-based label, stamped by the runner
  "agent": "claude_code|codex|cursor|pi",
  "kind": "agent",                          // 'agent' = assistant turn (tokens); 'human' = a prompt you sent (0 tokens, model null)
  "model": "claude-opus-4-8",               // may be null (esp. cursor)
  "provider": "anthropic|openai|cursor|...",
  "input_tokens": 0,                        // FRESH (non-cached) prompt tokens
  "output_tokens": 0,
  "cache_read_tokens": 0,                   // prompt tokens served from cache
  "cache_write_tokens": 0,                  // prompt tokens written to cache
  "reasoning_tokens": null,                 // codex only; null elsewhere
  "total_tokens": 0,
  "est_cost_usd": null,                     // NOTIONAL / API-equivalent (subs are flat-rate)
  "project": "agent-usage-tracker"          // working-dir basename only, never a full path
}
```

## Webhook payload

`POST <webhook_url>` with header `X-AUT-Secret: <shared secret>`:

```json
{ "events": [ { ...event... }, ... ] }
```

The sink upserts with `ON CONFLICT (event_id, ts) DO NOTHING`, so re-delivery (retries,
both transports, re-scans) never double-counts.

## Normalization rules (why counts are comparable across agents)

- `input_tokens` means **fresh, non-cached** prompt tokens everywhere. Claude Code and Pi
  already separate cache fields. **Codex reports `input_tokens` inclusive of cached**, so
  its collector stores `input = input - cached_input` and `cache_read = cached_input`.
- One row per assistant response / `token_count` event, not per session. Codex uses the
  per-turn `last_token_usage` delta (not the cumulative total) to avoid double counting.
- `est_cost_usd` is only populated where the source logs a cost (Pi, Cursor). It is an
  API-equivalent estimate, never a real bill.
- `ts` is required at the sink (hypertable partition column). The collector **drops** any
  record whose source line has no timestamp (they are counted as skipped, never pushed), so
  null never reaches the sink even though `UsageEvent.ts` is typed `str | None`.

## Per-agent reality (measured on real logs, 2026-07-18)

| Agent | Source | Model | Timestamp | Notes |
|---|---|---|---|---|
| **claude_code** | `~/.claude/projects/**/*.jsonl` `assistant.message.usage` | yes | record `timestamp` | Most tokens are in the **cache** fields; summing only `input_tokens` undercounts ~460x. Globs all files (20k+); an inode+offset watermark reads only changed ones. |
| **codex** | `~/.codex/sessions/**/rollout-*.jsonl` `token_count` | `turn_context.model` | event `timestamp` | `input` includes cached; we split it. Captures `reasoning_tokens`. |
| **pi** | `~/.pi/agent/sessions/**/*.jsonl` `message.usage` | `message.model` / `model_change` | record `timestamp` | Cleanest schema; older sessions may log zeros. |
| **cursor** | `state.vscdb` `cursorDiskKV bubbleId:*` | **often null** (not in DB) | bubble `createdAt` or joined `composerData` | EXPERIMENTAL: a LOWER BOUND. For complete + model-attributed + costed data, use the dashboard API with `cursorAuth/accessToken` (follow-up, not in v1). Full-scans each run + dedups at the sink. |
