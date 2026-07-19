# v2 OTel lane: is it worth it?

Purpose: decide whether to add a second, OpenTelemetry-based collection path (Claude Code,
Codex, Cursor, Pi all speak — or claim to speak — OTel) as a cross-check on the existing
log-parsing collectors (`claude_code.py`, `codex.py`, `cursor.py`, `pi.py`), landing in the
same TimescaleDB sink via the same `agents.usage_event` contract (`CONTRACT.md`, `push.py`).

Research method: WebFetch on the four sources named in the brief + ~9 Tavily searches to
fill gaps and to verify one claim that turned out to be false (see §2).

**Bottom line up front: skip it as scoped.** The premise — "OTel matters most for Cursor,
the weakest collector" — is wrong: Cursor's hook payloads structurally cannot carry token
counts, OTel or not. Codex's exec-mode token metric is currently broken upstream. Pi's two
candidate packages are unofficial, mutually inconsistent, and redundant with the collector's
"cleanest schema" already on disk. The only agent where OTel adds real, non-redundant value
is Claude Code (accurate `output_tokens` + skill/agent/plugin attribution) — worth doing only
as a ~100-line custom receiver bolted onto the existing webhook, not a full Collector
deployment with a Postgres exporter (that exporter doesn't exist upstream — see §5).

---

## 1. Claude Code — OTel-capable, GA, well-documented

Source: https://code.claude.com/docs/en/monitoring-usage

**Enable:**
```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp        # or: prometheus, console, none (comma-separable)
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json   # grpc | http/json | http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

**Metric:** `claude_code.token.usage` (unit: tokens). Attributes:
- `type`: `input` | `output` | `cacheRead` | `cacheCreation`
- `model`, `query_source` (`main`/`subagent`/`auxiliary`), `speed` (`fast`, if used),
  `effort` (`low`/`medium`/`high`/`xhigh`/`max`, if applicable)
- `agent.name`, `skill.name`, `plugin.name`, `marketplace.name`, `mcp_server.name`,
  `mcp_tool.name` — **none of this attribution exists in the JSONL transcript** the
  `claude_code` collector already parses.

**Cadence:** `OTEL_METRIC_EXPORT_INTERVAL` default 60000ms (60s); logs default 5s.

**Signals:** metrics ✅ GA, logs ✅ GA (`OTEL_LOGS_EXPORTER`), traces ✅ beta
(`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`).

**Privacy:** token metrics carry no content by design. `OTEL_LOG_TOOL_DETAILS=1` and
`OTEL_LOG_USER_PROMPTS=1` are separate opt-ins that only affect logs/traces, not the
metric — leave both unset for a token-only lane.

**Verdict: real marginal value, but narrow.** The existing `claude_code` collector already
gets `input_tokens`/cache fields accurately from the JSONL transcript (per
`docs/research/sdk-usage-capture.md` and `CONTRACT.md`); its one known weak spot is
`output_tokens` (documented as a buggy/placeholder field in some CLI versions). OTel fixes
exactly that, plus gives attribution (which skill/agent/plugin/MCP tool burned the tokens)
that no on-disk artifact carries. **Worth building only if that attribution matters** — not
a blanket "yes."

---

## 2. Cursor — the premise is false: hooks cannot carry token usage, OTel or not

Sources: https://github.com/LangGuard-AI/cursor-otel-hook, https://cursor.com/docs/hooks,
Cursor community forum.

**What `cursor-otel-hook` does:** wraps Cursor's native hook system (`~/.cursor/hooks.json`,
scripts under `~/.cursor/hooks/`) and converts hook events into OTel **traces only** (no
metrics, no logs), using LangSmith/LangChain-flavored span attributes:
`gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.operation.name`, plus `langsmith.metadata.*`. No Cursor API key required — it runs
purely off local hook stdin/stdout, compatible with a subscription seat. Setup:
`setup.sh` generates the wrapper + registers `~/.cursor/hooks.json`.

**Why token counts are absent, confirmed two ways:**
1. Cursor's own hooks reference doc (`https://cursor.com/docs/hooks`) gives the exact `stop`
   hook input schema: `{"status": "completed"|"aborted"|"error", "loop_count": 0}`, plus the
   common per-hook fields (`conversation_id, generation_id, model, model_id, model_params,
   hook_event_name, cursor_version, workspace_roots, user_email, transcript_path`). **No
   token/usage field anywhere in the documented schema.**
2. Cursor staff, directly, on the forum (https://forum.cursor.com/t/cursor-hooks-token-usage-support/147216):
   *"we haven't added token usage metadata to hook inputs yet, so current hook payloads
   don't include it... token breakdown still isn't included."*

(One conflicting data point — a GitHub issue on a third-party CLI,
`entireio/cli#1264` — asserts the `stop` payload *does* include
`input_tokens`/`output_tokens`/`cache_read_tokens`/`cache_write_tokens` and blames a parsing
bug. That claim contradicts Cursor's own published schema and Cursor staff's direct
statement, so it's treated as the less credible source — possibly conflating the CLI's
`--output-format stream-json` result event, which *does* carry a final token-usage result per
a separate forum thread, with the hook payload, which does not.)

**Verdict: OTel adds nothing for Cursor today.** Since the only interception point
(`cursor-otel-hook`, or any hook-based approach) is capped by what Cursor's own hook schema
exposes, and that schema has no token fields, no amount of OTel plumbing recovers what
Cursor withholds. The `cursor` collector's "lower bound" problem (per `CONTRACT.md`: model
often null, no cost) is a Cursor-side data-availability problem, not a collection-mechanism
problem — the already-flagged fix is the dashboard API with `cursorAuth/accessToken`
(a **separate, non-OTel** follow-up), not this lane.

---

## 3. Pi — two unofficial, inconsistent third-party packages; redundant either way

Sources: https://pi.dev/packages/pi-telemetry-otel, https://github.com/devkade/pi-opentelemetry

| | `pi-telemetry-otel` | `@devkade/pi-opentelemetry` |
|---|---|---|
| Maintainer | pi.dev package listing (registry entry, not confirmed first-party) | individual GitHub user (`devkade`), not the Pi org |
| Signals | traces only (`pi.session`/`pi.agent`/`pi.turn`/`pi.tool` spans) | traces + **metrics** (duration, prompt/token counts, cost) |
| Token attributes | **none documented** | prompt/token counts + cost, but no field-name schema published |
| Exporter | OTLP/HTTP hardcoded toward Jaeger | OTLP traces + metrics endpoints, configurable |
| Enable | `pi install npm:pi-telemetry-otel` | `pi install npm:@devkade/pi-opentelemetry` |
| Notable | — | ships `/otel-status`, `/otel-open-trace` diagnostics; `OTEL_PAYLOAD_MAX_BYTES` (32KB) cap; `strict` vs `detailed-with-redaction` privacy profiles |

Neither is the Pi CLI's own first-party telemetry; they disagree on whether tokens are
emitted at all. Meanwhile the existing `pi` collector already parses
`~/.pi/agent/sessions/**/*.jsonl` `message.usage` — per `CONTRACT.md`, "cleanest schema" of
the four agents, with `input/output/cacheRead/cacheWrite/totalTokens` on every assistant turn.

**Verdict: skip.** No first-party OTel path, two mutually-inconsistent third-party ones, zero
marginal value over a collector already rated cleanest.

---

## 4. Codex — first-party OTel exists, but the metric path is broken for `codex exec`

Sources (Tavily): AWS CloudWatch Codex OTel setup docs, `openai/codex#33668`, `openai/codex#12913`.

**Enable** via `~/.codex/config.toml`:
```toml
[otel]
environment = "dev"
exporter = { otlp-http = { endpoint = "http://localhost:4318", protocol = "binary" } }
# or: exporter = { otlp-http = { endpoint = ".../v1/metrics", headers = { Authorization = "Bearer ..." } } }
```
Also supports separate `trace_exporter` and a bearer-token-direct-to-CloudWatch path (no
Collector needed) via `OTEL_RESOURCE_ATTRIBUTES` for identity/org tagging.

**Signal support by entry point** (from the docs + two open bugs):

| Mode | Traces | Logs | Metrics |
|---|---|---|---|
| `codex` (interactive TUI) | done | done | done |
| `codex exec` (headless/CI) | done | done | **broken** — `codex.turn.token_usage` histogram never emitted (open, `#33668`, filed 3 days before this research) |
| `codex mcp-server` | — | — | — (fixed in later builds per `#12913`, treat as version-dependent) |

Per-turn token counts **are** present as span/log attributes even under `codex exec` — just
not as a metric instrument, so anything wired to consume the metric stream (Prometheus,
Grafana-native, etc.) sees nothing for headless runs.

**Verdict: skip.** The existing `codex` collector already parses the rollout JSONL
accurately (including `reasoning_tokens`, which OTel doesn't expose any better) for both
interactive and exec-mode sessions. OTel's metrics path is actively unreliable for exactly
the mode (`codex exec`) most likely to be run unattended, and adds no field the log parser
lacks even when it does work.

---

## 5. The Postgres/TimescaleDB sink problem: no native exporter exists

There is **no upstream OTel Collector exporter that writes to PostgreSQL/Timescale.**
- `open-telemetry/opentelemetry-collector-contrib#35451` — the original ask (2024), closed
  as inactive / no sponsor.
- `open-telemetry/opentelemetry-collector-contrib#46501` — a newer "component donation" for
  exactly this (explicitly names Timescale as a target), still a proposal, not merged/GA.

The realistic options today, in order of how well they fit this repo:

1. **Skip the Collector binary; write a tiny custom OTLP/HTTP-JSON receiver.** Claude
   Code's `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` sends canonical OTLP-JSON (plain JSON,
   `resourceMetrics → scopeMetrics → metrics → dataPoints`, no protobuf library needed). A
   ~100-line endpoint decodes that, filters for `claude_code.token.usage` data points, maps
   `type`/`model`/`query_source`/etc. attributes onto the existing `UsageEvent` dataclass
   (`event.py`), and calls the **already-built** `push.py::make_sender()` → the same n8n
   webhook → the same `agents.usage_event` hypertable, `ON CONFLICT (event_id, ts) DO
   NOTHING`. Zero new schema, zero new sink, zero new dependency. This is the only option
   that doesn't add an extra moving part beyond what already exists.
2. Run the real `otelcol-contrib` binary as a receiver + privacy-stripping processor, but
   still terminate it in a `file` exporter (NDJSON to disk) that a tailer script (mirroring
   option 1's mapping logic) picks up. Strictly more infrastructure than option 1 for the
   same output — a Collector process, its config, and a second small script — for no
   accuracy gain. Only worth it if the team wants the Collector's batteries (TLS, retry,
   batching) in front of many *non*-agent OTel sources too, which is out of scope here.
3. `prometheusremotewrite` exporter → a Prometheus remote-write-compatible endpoint. Wrong
   shape for this project: our schema is relational, per-turn, per-model/agent rows, not
   Prometheus's timeseries-per-label-set model, and would need its own translation layer
   back into `agents.usage_event` anyway — no simpler than option 1.

**Recommended architecture, if built at all** (Claude-Code-only, per §1's verdict):

```
Claude Code (OTEL_METRICS_EXPORTER=otlp, http/json, localhost:4318)
        |  OTLP/HTTP JSON POST /v1/metrics
        v
tiny custom receiver (new module, e.g. collectors/claude_code_otel.py or a standalone script)
  - decode OTLP-JSON body
  - keep ONLY claude_code.token.usage data points (drop everything else — no prompt/response
    content ever reaches this metric anyway, per §1's privacy note)
  - map attributes -> UsageEvent(agent="claude_code", provider="anthropic", ...)
  - dedupe key: hash(model, type, query_source, data-point start/end timestamps) since OTel
    metrics don't carry a natural per-turn event id the way the JSONL transcript does
  - push.py.make_sender()(batch)  # reuse, don't reinvent
        |
        v
same n8n webhook -> agents.usage_event (TimescaleDB), tagged e.g. provenance="otel" so it's
queryable as a cross-check against the log-parsed claude_code rows, not silently merged.
```

No standalone `otelcol` config is included here because §5 shows the natural next step in a
"real Collector" pipeline (an exporter to Postgres) doesn't exist upstream — a from-scratch
YAML would just end at a placeholder `debug`/`file` exporter, which is strictly worse than
receiving OTLP directly in the mapping script above.

---

## 6. Decision table

| Agent | Can emit OTel token usage today? | Enable | Attribute names | Verdict vs current collector |
|---|---|---|---|---|
| **Claude Code** | Yes (GA metric) | `CLAUDE_CODE_ENABLE_TELEMETRY=1` + `OTEL_METRICS_EXPORTER=otlp` | `claude_code.token.usage{type,model,query_source,effort,speed,skill.name,agent.name,...}` | Real but narrow value: fixes the one buggy field (`output_tokens`) + adds attribution the JSONL lacks. Worth a ~100-line custom receiver, not a full Collector. |
| **Codex** | Partially — traces/logs yes, **metrics broken for `codex exec`** (open bug) | `[otel]` block in `config.toml` | `codex.turn.token_usage` (histogram; not emitted headless), token fields present only as span/log attributes | Redundant + currently unreliable for the mode most used unattended. Skip. |
| **Cursor** | **No** — hook schema has no token fields (confirmed via official docs + Cursor staff) | N/A | `gen_ai.request.model` etc. (model/tool only, no usage) | Structurally can't help. The real fix for Cursor's lower-bound gap is the dashboard API (separate, already-flagged, non-OTel follow-up). Skip. |
| **Pi** | Via 2 unofficial third-party packages, mutually inconsistent on whether tokens are emitted at all | `pi install npm:...` | undocumented / not first-party | Redundant with the collector's already-"cleanest" JSONL schema. Skip. |

**Overall verdict: skip the v2 OTel lane as scoped.** If anything is built, build only the
Claude-Code-specific custom receiver in §5 option 1 as an optional cross-check for
`output_tokens` accuracy and skill/agent attribution — everything else in the original
4-agent scope is either redundant or structurally impossible.
