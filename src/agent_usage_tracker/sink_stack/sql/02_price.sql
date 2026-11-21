-- agents.model_price — API-equivalent per-token USD rates for the Grafana "$" cost view.
--
-- Source: LiteLLM's model_prices_and_context_window.json (+ models.dev fallback), produced by
-- a price sync job. aut does not fill this table yet. Rates are NOTIONAL / API-equivalent — the
-- tracked coding-agent subscriptions are flat-rate, so this is "what the same tokens would cost
-- on metered APIs", never a real bill.
--
-- Apply this schema during database bootstrap, then let a price sync job
-- refresh canonical and observed alias rows.
--
-- A one-time load is not enough. Rates change slowly; the MODEL LIST does not. A new model
-- with no row joins to NULL and costs exactly $0 in the Grafana panels. A cost view that
-- silently under-reports is worse than one that is obviously empty, because the number
-- still looks like an answer.
--
-- A monitoring check should flag any model with >100M tokens in 7 days and no price row,
-- so the next new model surfaces instead of quietly costing nothing. The threshold is high
-- on purpose: junk labels (empty strings, "codex-auto-review", grok-bot-*) should not page.
-- UI display names ("GPT-5.6 Sol (Auto Intelligence)") are aliased onto the canonical
-- LiteLLM key. Cursor Composer 2.5 uses published list prices when LiteLLM has no row.
-- The detector still does not invent a rate for an unknown model.
--
-- Grafana ($ panels) reads this via the read-only agents_ro role; the SELECT grant below is
-- what lets the dashboard cost panels join usage rows to prices.

BEGIN;

CREATE TABLE IF NOT EXISTS agents.model_price (
  model_key         text PRIMARY KEY,   -- normalized model id (lowercased, provider/version stripped)
  input_cost        numeric,            -- USD per input (fresh) token
  output_cost       numeric,            -- USD per output token
  cache_read_cost   numeric,            -- USD per cache-read token (falls back to input_cost if null)
  cache_write_cost  numeric,            -- USD per cache-write token (falls back to input_cost if null)
  synced_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE agents.model_price IS
  'API-equivalent per-token USD rates (LiteLLM via aut sync-prices); NOTIONAL, subscriptions are flat-rate.';

-- Guarded like 01_schema.sql: agents_ro is created outside this file. This file is safe
-- to rerun, so the grant lands on the first run after the role exists.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_ro') THEN
        GRANT SELECT ON agents.model_price TO agents_ro;
    END IF;
END $$;

COMMIT;

-- Cost of one row (API-equivalent), used by the Grafana $ panels:
--   input_tokens*input_cost
--   + output_tokens*output_cost
--   + cache_read_tokens*coalesce(cache_read_cost, input_cost)
--   + cache_write_tokens*coalesce(cache_write_cost, input_cost)
-- reasoning_tokens is NOT added: it is a subset of output_tokens (Codex and Pi both report
-- reasoning <= output and a total that already excludes it), so pricing it again double-charges.
-- joined: agents.usage_event u JOIN agents.model_price p ON lower(u.model) = p.model_key
-- Unmatched models (e.g. some Cursor "-thinking" variants absent from LiteLLM) simply drop out
-- of the $ figure — never priced with a guess (mirrors the tracker's priced_coverage semantics).
