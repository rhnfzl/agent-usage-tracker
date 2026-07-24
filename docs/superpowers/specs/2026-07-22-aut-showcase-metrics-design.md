# AUT showcase-metrics capability — design

- **Date:** 2026-07-22
- **Status:** approved design, pre-implementation
- **Branch / worktree:** `feat/showcase-metrics` (worktree `agent-usage-tracker-showcase-metrics`), based on `human-turns`
- **Author:** Rehan Fazal (with Claude Code)

## 1. Why

The tracker (`aut`) already collects normalized per-turn usage across Claude Code, Codex,
Cursor, and Pi and pushes it to a self-hosted sink. The goal here is a **derivation +
publish** capability that turns that raw event stream into a small, honest set of
**showcase metrics** for a personal/professional website.

The framing is deliberate and ranked (this is the anti-flex spine — see §3):

1. **The instrument, not the readout** — lead with the fact that this was *built*; numbers are a byproduct.
2. **Fluency / experience** — depth of hands-on agentic experience, told as a *trend*, not a lifetime total.
3. **Craft** — "I care enough about my practice to instrument it."
4. **Efficiency & cost discipline** — ratios (cache-hit %, cost-per-outcome), never volume.

Sequence does the anti-flex work: by the time a viewer reaches any raw number, they have
already been told it is a byproduct of an engineering artifact.

## 2. Goals / non-goals

**Goals**
- A read-side `aut report` command that computes **all** metric families (§4) from the
  unified sink and emits a deterministic `metrics.json`.
- An `aut sync-prices` step that maintains an API-equivalent price map from an open-source
  source (§6), so `$`-denominated metrics are real rates, not guesses.
- A nightly/weekly homelab job that runs sync-then-report and drops `metrics.json` where a
  site build can read it.

**Non-goals (explicitly out of scope for this spec)**
- The website's layout/design. Undecided by intent — decided *after* we see real output.
- Summary prose generation (hand-written vs one LLM call). A publish-layer detail (§7).
- Any live / real-time serving. Refresh is a cron, not a request path.
- A public repo link or any raw-data export to the site (contract boundary — see §3, §8).

## 3. Constraints (locked)

- **Contract-safe by construction.** Published metrics aggregate to
  `agent × model × month × token-class (× kind)`. The `project` column is **never selected**
  into any output. The showcase is safe because the aggregate *cannot* emit a project name —
  not because redaction was remembered.
- **No public repo, no raw data on the site.** The instrument is *narrated* (static facts in
  §4.D), never linked.
- **Not live.** Nightly/weekly cron.
- **Everything or nothing.** No phased rollups — the full metric set is the deliverable.

## 4. The metric set (all of it)

All families are computed over sink rows (`agents.usage_event`). `input` below means the
**fresh, non-cached** prompt tokens (the tracker's normalization already guarantees this
meaning across agents).

### 4.A Efficiency ratios
| Metric | Formula | Needs price map? |
|---|---|---|
| Cache-hit rate | `cache_read / (cache_read + input)` | no (pure token ratio) |
| Cache-savings multiple | `naive_cost / actual_cost` | yes |
| Output leverage | `output / input` | no |
| Reasoning share (Codex) | `reasoning / total` | no |
| Cost per active-day | `total_cost / active_days` | yes |

Where `actual_cost = input·p_in + output·p_out + cache_read·p_cacheR + cache_write·p_cacheW`
and `naive_cost` reprices `cache_read` at the full input rate `p_in` (i.e. "what caching
saved"). Reported overall and trended by month.

### 4.B Fluency / experience trends
- **Adoption timeline:** `first_seen(agent) = min(ts)`; `months_active(agent) = span(first→last)`.
- **Active days:** distinct calendar days with ≥1 event, total and per-month.
- **Sessions:** gap-based sessionization (§5) — count total and per-month.
- **Breadth:** distinct agents, distinct models; model-mix share per month (stacked trend).

### 4.C Collaboration (newly enabled by `kind='human'`)
- **Human turns:** count, total and per-month.
- **Agent-turns-per-human-turn:** `agent_turns / human_turns` — leverage per instruction.
- **Human-turn share** over time.

### 4.D Instrument / craft facts (static constants, not computed from data)
Emitted verbatim so the summary/site can cite them: *4 agents normalized (+ Cursor variants),
5 token classes captured, passive & subscription-safe, deterministic idempotency, both human
and agent turns tracked, and the cache-undercount correctness insight.* Zero new data — this
describes the tool that was built.

### 4.E Contextual volume (shown once)
Lifetime `total_tokens` and lifetime API-equivalent `cost`, each surfaced **once**, framed as
"work done on a flat-rate subscription." Present-once so it can't become a scoreboard.

## 5. Session derivation (decision: derive, don't collect)

Sessions are **derived at report time** by clustering each `(agent, machine)`'s events in `ts`
order, opening a new session whenever the gap to the previous event exceeds `IDLE_GAP`
(default 30 min, configurable). Rationale:

- Works **retroactively on all existing history** — a stored `session_id` would only fill
  go-forward (the sink's `ON CONFLICT DO NOTHING` won't backfill it without a migration).
- **Zero collector change** — the collector stays a pure collect-and-push tool.
- Honest: a "session" = a burst of activity, which is what a viewer understands anyway.

**Optional precision upgrade (deferred):** each source log file already *is* a true session,
so a `session_id` column (hash of file identity) would give exact boundaries. Not needed for
v1; recorded here so the choice is explicit.

## 6. Price map — synced from LiteLLM

Reuses the usual price-registry sync pattern, **extended**
to also extract the two cache-cost fields such syncs normally skip.

- **Source:** LiteLLM `model_prices_and_context_window.json` (raw GitHub, primary) →
  `models.dev/api.json` (fallback). Confirmed to carry, per model, `input_cost_per_token`,
  `output_cost_per_token`, `cache_read_input_token_cost`, `cache_creation_input_token_cost`
  (verified 2026-07-22: e.g. `claude-opus-4` = in $15/M, out $75/M, cacheR $1.5/M, cacheW $18.75/M;
  700 of ~2970 models carry cache-read pricing).
- **Shape:** a pure `build_rate_map(litellm, models_dev)` → `{normalized_key: {input_cost,
  output_cost, cache_read_cost, cache_write_cost}}`, plus a thin `httpx` fetch and an atomic
  temp-file → `os.replace` write. Model-key normalization mirrors the registry (lowercase, strip
  provider path + version suffix) so the tracker's `model` values match.
- **Separation (writer vs reader, as in the registry):** `aut sync-prices --out model_prices.json`
  writes the file; `aut report` only **reads** it, so the report core stays offline and
  deterministic.
- **Graceful degradation, no guessing:** token-count ratios (cache-hit %, leverage, session
  counts) need no pricing. Only `$` figures need the map; a model absent from it is reported
  `priced: false` and excluded from `$` aggregates — a price is never invented. `metrics.json`
  carries a `priced_coverage` block (models priced / unpriced, unpriced token share) so any
  gap is visible, not silent.

## 7. Output & publish

- **`aut report`** is a pure function over `(rows, price_map, now)` → `metrics.json`, so tests
  pin exact outputs. `as_of` / period stamps are passed in (or derived from `max(ts)`), not
  read from a wall clock inside the tested core.
- **`metrics.json`** (sketch): `{ as_of, efficiency{}, fluency{}, collaboration{}, volume{},
  instrument{}, trends{ by_month: [...] }, priced_coverage{} }`.
- **Homelab cron:** installs `aut`, runs `aut sync-prices` then `aut report` against localhost
  Postgres (the sink) nightly/weekly, and drops `metrics.json` where the site build reads it.
- **Summary prose (deferred):** `aut report` emits numbers only. The prose ("here's what a
  year of this looked like") is a separate publish step — hand-written, or one LLM call fed
  `metrics.json` — kept out of the deterministic core.

## 8. Privacy enforcement (recap)

The `project` column is never read into any aggregate or output. Granularity ceiling is
`agent × model × month × token-class (× kind)`. This is enforced in the SQL/selection layer,
not by post-hoc redaction, so it cannot regress silently.

## 9. Testing (ponytail-minimal, no framework)

- **Golden fixture:** a fixed set of known rows → asserted metric outputs (ratios exact).
- **Sessionization:** a synthetic `ts` sequence → expected session count at a given `IDLE_GAP`.
- **Pricing math:** `build_rate_map` on a small LiteLLM sample → expected rates incl. cache
  fields; then `actual_cost` / `naive_cost` / savings-multiple assertions.
- **Degradation:** a model missing from the price map → `priced:false`, `$` metrics excluded,
  token-ratio metrics still present.

## 10. Open questions / future

- **Read access:** `aut report` needs a read DSN to the sink. Reuse the existing optional
  `pg_dsn` config (already present for the direct-push fallback) as the report's read source.
- **True session ids** (§5) if per-session precision ever matters.
- **Human-vs-agent** could grow into a richer collaboration view later; v1 keeps the three
  ratios in §4.C.
