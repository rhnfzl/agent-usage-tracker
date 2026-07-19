# Coding-agent SDK usage/token capture survey

Purpose: decide which coding-agent SDKs a passive log-parsing tracker can support "out of the box" (tail a file that already exists) vs which need a small in-process `emit()` hook, vs which are best caught generically via an OTLP receiver.

Research method: ~17 Tavily searches across official docs, SDK source/READMEs, GitHub issues, and vendor integration guides. Bounded, 2 waves.

---

## 1. Claude Agent SDK (Python `claude-agent-sdk`, TS `@anthropic-ai/claude-agent-sdk`)

**Usage shape.** The final `ResultMessage` in the `query()` async-generator stream carries `usage`:
```
usage={'input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
       'output_tokens', 'server_tool_use': {...}, 'service_tier',
       'cache_creation': {'ephemeral_1h_input_tokens', 'ephemeral_5m_input_tokens'}}
```
plus `total_cost_usd`. The TS SDK additionally exposes a per-model breakdown via `message.modelUsage[modelName]` → `{inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD}`.
(Official docs: https://code.claude.com/docs/en/agent-sdk/cost-tracking ; field shape confirmed in https://github.com/anshumanbh/securevibes/blob/main/docs/references/claude-agent-sdk-guide.md)

Known accuracy bugs: `output_tokens` has been reported wrong/non-cumulative (https://github.com/anthropics/claude-agent-sdk-python/issues/112) and thinking tokens get folded into `output_tokens` with no separate breakdown (https://github.com/anthropics/claude-agent-sdk-python/issues/540).

**Default persistence.** The `claude` CLI (which the SDK drives) writes a full per-session JSONL transcript to `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, one JSON object per line, with `message.usage` embedded on every assistant record — no setup required (https://claude-dev.tools/docs/jsonl-format, https://www.tokenforest.com.au/en/track-claude-code-usage). Caveat: a currently-open bug reports that the JSONL's `output_tokens` field is a placeholder (1–2) — the real value only appears in the streaming `result` event, never persisted to disk (https://github.com/anthropics/claude-code/issues/25941). `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` are reported accurate.

**Auth.** Both: `ANTHROPIC_API_KEY` (pay-per-token) or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (Claude Pro/Max subscription quota, no API billing). API key takes precedence if both are set. OAuth tokens are policy-restricted (Feb 2026) to official Anthropic clients only. (https://lalatenduswain.medium.com/claude-code-on-claude-max-plan-understanding-oauth-token-vs-api-key-authentication-in-2026-96a6213d2cde)

**OTel.** Opt-in via `CLAUDE_CODE_ENABLE_TELEMETRY=1` + `OTEL_METRICS_EXPORTER=otlp` / `OTEL_LOGS_EXPORTER=otlp`. Emits `claude_code.token.usage` metric and `claude_code.api_request` log events; trace support is in beta (https://signoz.io/docs/claude-code-monitoring, https://opentelemetry.io/blog/2026/genai-observability).

**Verdict: OUT-OF-THE-BOX PARSEABLE** (JSONL transcript, default-on) **+ OTEL-CAPABLE** (opt-in, metrics/logs GA, traces beta). For output-token accuracy, prefer the OTel metric or CLI cost-tracking output over the raw JSONL field.

---

## 2. Cursor Agent SDK / `cursor-agent` CLI

**Usage shape.** Cloud Agents API returns per-run token usage in its run object (zeros until the run produces usage) (https://cursor.com/docs/cloud-agent/api/endpoints). The TS SDK (`@cursor/sdk`, community `@nothumanwork/cursor-agents-sdk`) returns a result/stream-json object per run; exact field names for local CLI usage weren't pinned down beyond the Cloud API shape — treat as in-process return value, not a stable public schema.

**Default persistence.** `cursor-agent` CLI does persist local session data, but it's **chat/prompt history, not usage**: transcripts at `~/.cursor/chats/` (or `~/.config/cursor/chats/`, XDG-dependent) and a separate `prompt_history.json` MRU list (https://forum.cursor.com/t/cursor-cli-past-chats-not-showing-up/152450, https://forum.cursor.com/t/a-bug-on-cursor-agent-cli/161598). No evidence these files carry token counts. The Cloud Agents API tracks usage server-side, fetchable by polling, not a local artifact.

**Auth.** Both: `CURSOR_API_KEY` (automation/CI) or browser OAuth login tied to a Cursor subscription (Pro/Business token pool) (https://cursor.com/docs/cli/reference/authentication).

**Verdict: NEEDS EMIT HOOK.** Local chat storage doesn't reliably carry usage; usage lives in the SDK's in-process result or behind the Cloud Agents API (pollable, not a file to tail).

---

## 3. OpenAI Agents SDK (Python `openai-agents`, JS equivalent)

**Usage shape.** `result.context_wrapper.usage` → `Usage(requests, input_tokens, input_tokens_details.cached_tokens, output_tokens, output_tokens_details.reasoning_tokens, total_tokens, request_usage_entries[])`, with a `request_usage_entries` list giving per-request input/output tokens. Also reachable inside `RunHooks.on_agent_end(context, ...)` via `context.usage`. (https://openai.github.io/openai-agents-python/usage, https://openai.github.io/openai-agents-python/ref/usage)

**Default persistence.** None. This is purely an in-process return value / hook payload. `Session` implementations (e.g. `SQLiteSession`) persist conversation history for context continuity, but usage is returned per-run and not written into the session store.

**Auth.** API key only (`OPENAI_API_KEY`), standard pay-per-token billing.

**Verdict: NEEDS EMIT HOOK.**

---

## 4. Codex SDK (`@openai/codex-sdk`) / `codex exec`

**Usage shape.** `codex exec --json` emits a `turn.completed` event with `usage: {input_tokens, cached_input_tokens, output_tokens}`. Reasoning tokens (`reasoning_output_tokens`) are tracked internally and written to the session rollout file but — as of an open bug — never surfaced in the `--json` stdout stream (https://github.com/openai/codex/issues/19022). The `@openai/codex-sdk` (via promptfoo's integration) surfaces `tokenUsage` including `completionDetails.reasoning` when available (https://www.promptfoo.dev/docs/providers/openai-codex-sdk).

**Default persistence.** **Yes**, unless run with `--ephemeral`: session rollout files at `~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl`, containing `event_msg` records with `payload.type == "token_count"` carrying `info.total_token_usage` (`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`), `last_token_usage`, `model_context_window`, `rate_limits` (https://github.com/openai/codex/issues/19022, https://www.tokenforest.com.au/en/track-claude-code-usage). This is the one place `reasoning_output_tokens` is actually recorded on disk.

**Auth.** Both: ChatGPT subscription login (default for humans / required for Codex cloud) or `CODEX_API_KEY` (recommended for CI/automation, pay-per-token). Cached OAuth token in `~/.codex/auth.json` goes stale after ~8 days on unattended runners (https://learn.chatgpt.com/docs/auth, https://www.developersdigest.tech/blog/codex-exec-ci-headless-guide).

**OTel.** Opt-in via `config.toml` `[otel.trace_exporter.otlp-http]` block — exports structured log events and OTel metrics for API requests, tool calls, and sessions (https://www.comet.com/docs/opik/latest/integrations/openai-codex, https://opentelemetry.io/blog/2026/genai-observability).

**Verdict: OUT-OF-THE-BOX PARSEABLE** (rollout JSONL, default-on unless `--ephemeral`) **+ OTEL-CAPABLE** (opt-in). Best source for reasoning-token counts specifically.

---

## 5. Google ADK (Agent Development Kit) / Gemini SDK

**Usage shape.** `event.usage_metadata` (a Gemini `GenerateContentResponseUsageMetadata`) → `prompt_token_count, candidates_token_count, total_token_count, thoughts_token_count` (reasoning), `cached_content_token_count`, per-modality breakdowns (`prompt_tokens_details`/`candidates_tokens_details` for TEXT/AUDIO), `tool_use_prompt_token_count`. (https://github.com/google/adk-python/discussions/97, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/list-token)

**Default persistence.** In-process only, and this is the sharpest gap in the survey: even when a durable `SessionService` is configured (`DatabaseSessionService`, `VertexAiSessionService`), **`usage_metadata` is explicitly not part of the persisted event schema** — a maintainer response confirms it must be captured via a custom `EventConverter` before it reaches storage (https://github.com/google/adk-python/discussions/2434). The default `InMemorySessionService` (local dev) persists nothing across restarts anyway (https://adk.dev/sessions/session).

**Auth.** Google API key or Vertex AI service-account credentials — no consumer "subscription" equivalent to Claude/Cursor/Codex; billing is straight Gemini API/Vertex usage-based.

**Verdict: NEEDS EMIT HOOK.** Not just "no default file" — even opting into DB-backed sessions doesn't rescue usage data without custom code.

---

## 6. Vercel AI SDK (`ai` package)

**Usage shape.** `generateText`/`streamText` return a `usage` object on the result (seen as `promptTokens`/`completionTokens`/`totalTokens` in older docs/third-party wrappers — https://revstack-dev-revstack-os.mintlify.app/sdks/ai; check current SDK version for `inputTokens`/`outputTokens` naming as the SDK evolves).

**Default persistence.** None — plain in-process return value. OTel is fully opt-in per call via `experimental_telemetry: { isEnabled: true, recordInputs, recordOutputs }`, requiring an OTel SDK/exporter to be registered separately (e.g. `@vercel/otel`). When enabled it emits `ai.*` and `gen_ai.*` span attributes including token counts and is already supported out of the box by Pydantic Logfire, Sentry, SigNoz, Langfuse (https://pydantic.dev/articles/vercel-ai-sdk-logfire-otel, https://sentry.io/cookbook/vercel-ai-sdk-otel-sentry, https://signoz.io/blog/opentelemetry-vercel-ai-sdk).

**Auth.** Bring-your-own provider API key (OpenAI, Anthropic, etc.) — no subscription concept at the SDK level.

**Verdict: NEEDS EMIT HOOK by default / OTEL-CAPABLE** (opt-in, unusually well standardized — same `gen_ai.*` names as everything else).

---

## 7. LangChain / LangGraph

**Usage shape.** `AIMessage.usage_metadata` is LangChain's standardized cross-provider `UsageMetadata`. Captured across multiple calls via `get_usage_metadata_callback()` context manager or `UsageMetadataCallbackHandler` (https://reference.langchain.com/python/langchain-core/callbacks/usage/get_usage_metadata_callback).

**Default persistence.** None for usage specifically. LangSmith tracing is opt-in (`LANGSMITH_TRACING=true`) and ships full traces — including token usage — to LangSmith's hosted or self-hosted backend, not a local file (https://docs.langchain.com/langsmith/trace-with-langchain). LangGraph checkpointers (`InMemorySaver`, SQLite, Postgres) persist **graph/thread state**, not usage as a first-class field — though since checkpoints serialize the full `AIMessage` objects, `usage_metadata` technically rides along inside the checkpoint blob (msgpack in a `checkpoints` table). That's a DB blob to deserialize, not a friendly log — not recommended as a primary capture path.

**Auth.** Per-provider API key (LangChain sits above whatever model client is configured); LangSmith itself has a separate API key for its tracing backend.

**Verdict: NEEDS EMIT HOOK** (callback) — LangSmith is a usable secondary path if the user already has a LangSmith account, but it's a proprietary hosted backend, not raw OTLP.

---

## 8. Pydantic AI

**Usage shape.** `result.usage()` → `RunUsage(requests, input_tokens, cache_write_tokens, cache_read_tokens, output_tokens, input_audio_tokens, output_audio_tokens, details)`. Field aliasing accepts both `input_tokens`/`request_tokens` and `output_tokens`/`response_tokens` naming. (https://pydantic.dev/docs/ai/api/pydantic-ai/usage)

Known bug: `RunUsage` reports 0 tokens for some Anthropic models while the real counts sit in the `details` sub-field (open issue: https://github.com/pydantic/pydantic-ai/issues/3223).

**Default persistence.** None — in-process only. Pydantic ships its own OTel-based observability product (Logfire) with opt-in instrumentation for Pydantic AI agents, emitting `gen_ai.*`/`pydantic_ai.*` spans (evidenced by Pydantic's own Vercel-AI-SDK-to-Logfire OTel writeup, same conventions apply to their own SDK).

**Auth.** Bring-your-own per-provider API key.

**Verdict: NEEDS EMIT HOOK / OTEL-CAPABLE** (opt-in via Logfire instrumentation).

---

## 9. CrewAI

**Usage shape.** `crew.usage_metrics` / `result.token_usage` → `total_tokens, prompt_tokens, completion_tokens, cached_prompt_tokens, successful_requests`.

**Default persistence.** None — in-process only, no file/trace.

**Reliability flag.** Multiple community reports (including an open GitHub issue) show these fields return all zeros, especially with non-default LLM providers (https://github.com/crewAIInc/crewAI/issues/1606, https://community.crewai.com/t/does-token-usage-works/503). Treat CrewAI's usage numbers as unreliable even when present.

**Auth.** Bring-your-own API key (LiteLLM-backed under the hood, so whatever provider LiteLLM is pointed at).

**Verdict: NEEDS EMIT HOOK** — and flag as the least trustworthy of the surveyed SDKs even after hooking it.

---

## 10. OpenTelemetry GenAI semantic conventions (cross-cutting)

Standard attribute names: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` (current; `gen_ai.usage.prompt_tokens`/`completion_tokens` are deprecated aliases), plus `gen_ai.system`/`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.operation.name`. Status is "experimental," targeting stability in 2026 (https://www.sitepoint.com/opentelemetry-genai-instrument-ai-agents-without-sending-data-to-the-cloud, https://github.com/open-telemetry/opentelemetry-python/blob/main/opentelemetry-semantic-conventions/src/opentelemetry/semconv/_incubating/attributes/gen_ai_attributes.py).

Confirmed emitters (opt-in unless noted): **Claude Code** (metrics/logs GA, traces beta), **OpenAI Codex** (config.toml block), **Vercel AI SDK** (`experimental_telemetry`), **Pydantic AI/Logfire**, **VS Code Copilot**, plus community auto-instrumentors for raw OpenAI/Anthropic clients (`opentelemetry-instrumentation-openai-v2`, etc.) that several LangChain/CrewAI/Agents-SDK stacks can be layered with. Multiple backends (PostHog, Sentry, SigNoz, Datadog, Grafana) already know how to render `gen_ai.*` spans out of the box (https://opentelemetry.io/blog/2026/genai-observability, https://posthog.com/docs/llm-analytics/installation/opentelemetry).

This is the generic "receive one OTLP stream, cover many SDKs at once" path: a single lightweight OTLP receiver picks up Claude Code, Codex, Vercel AI SDK, and Pydantic AI without writing four bespoke parsers.

---

## Verdict table

| SDK | Usage fields (key names) | Default persisted artifact | Auth | Verdict |
|---|---|---|---|---|
| Claude Agent SDK (CLI-backed) | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `modelUsage` (TS) | `~/.claude/projects/<proj>/<session>.jsonl` (default-on; `output_tokens` field is a known-buggy placeholder) | API key or subscription OAuth (`setup-token`) | **PARSEABLE + OTEL** |
| Cursor Agent SDK / `cursor-agent` | Cloud API usage report; CLI shape unconfirmed | `~/.cursor/chats/` = chat text only, no usage | API key or subscription OAuth | **EMIT HOOK** |
| OpenAI Agents SDK | `Usage(requests, input_tokens, output_tokens, total_tokens, request_usage_entries[])` | none (in-process / `RunHooks`) | API key only | **EMIT HOOK** |
| Codex SDK / `codex exec` | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` (rollout only) | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (default-on unless `--ephemeral`) | Subscription (ChatGPT login) or `CODEX_API_KEY` | **PARSEABLE + OTEL** |
| Google ADK / Gemini | `usage_metadata`: `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `total_token_count` | none — excluded from persisted event schema even with DB session service | API key / Vertex service account | **EMIT HOOK** |
| Vercel AI SDK | `usage`: prompt/completion/total tokens | none (in-process); OTel fully opt-in | Bring-your-own API key | **EMIT HOOK / OTEL** |
| LangChain / LangGraph | `AIMessage.usage_metadata` via callback | none for usage; LangSmith (hosted) opt-in; checkpoint DB has it buried in a blob | Per-provider key + separate LangSmith key | **EMIT HOOK** |
| Pydantic AI | `RunUsage(input_tokens, output_tokens, cache_read_tokens, ...)` (buggy 0s for some Anthropic models) | none; Logfire OTel opt-in | Bring-your-own API key | **EMIT HOOK / OTEL** |
| CrewAI | `usage_metrics`/`token_usage`: prompt/completion/total tokens (often reported as 0) | none | Bring-your-own (LiteLLM) API key | **EMIT HOOK** (low trust) |
| OTel GenAI conventions | `gen_ai.usage.input_tokens` / `output_tokens` | N/A — the generic receive path | N/A | **cross-cutting capture strategy** |

---

## Recommendation: 3 highest-leverage capture strategies

1. **Tail the two default-on-disk transcripts first — Claude Code and Codex.** Both already write full JSONL session logs to disk with no setup (`~/.claude/projects/**/*.jsonl`, `~/.codex/sessions/**/rollout-*.jsonl` unless `--ephemeral`). This is free coverage for the two SDKs the user is most likely to run daily. Watch the two known field bugs: Claude Code's JSONL `output_tokens` is a placeholder (cross-check against the OTel metric or `cost-tracking` output if precision matters); Codex's `--json` stdout omits `reasoning_output_tokens` even though the rollout file has it — the rollout file is the accurate one.

2. **Stand up one small local OTLP receiver for the opt-in-OTel cluster.** Claude Code, Codex, Vercel AI SDK, and Pydantic AI (via Logfire instrumentation) all speak the same `gen_ai.usage.*` semantic-convention names once their opt-in telemetry flag is flipped on. A single lightweight OTel Collector (or a minimal custom OTLP/HTTP receiver writing to a local file) captures all four with zero per-SDK parsing code — the highest-leverage single investment given how many SDKs already standardized on this wire format.

3. **Ship a tiny reusable `emit()` helper for the in-process-only stragglers.** OpenAI Agents SDK, Cursor Agent SDK, Google ADK, and CrewAI have no default on-disk artifact and no OTel path without extra instrumentation work. A ~5-line helper (take a normalized `{provider, model, input_tokens, output_tokens, ...}` dict, append one JSON line to a local log) that users wire into `RunHooks`/callbacks/`EventConverter` covers all four with the same shape. Treat ADK and CrewAI as needing this hook non-optionally: ADK explicitly strips `usage_metadata` from its persisted session schema by design, and CrewAI's usage fields are reported as unreliable/zero by its own community even before persistence is considered.
