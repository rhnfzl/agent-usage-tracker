-- Use current list prices to estimate API-equivalent USD for normalized usage.
-- Missing rates leave their tokens unpriced. Reasoning is already part of output.
BEGIN;

CREATE TABLE IF NOT EXISTS agents.model_price (
  model_key         text PRIMARY KEY,
  input_cost        numeric,
  output_cost       numeric,
  cache_read_cost   numeric,
  cache_write_cost  numeric,
  synced_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE agents.model_price IS
  'API-equivalent USD per token. Refresh explicitly with aut sync-prices --sink.';

-- Apply the same provider and version normalization as pricing.normalize_model_key.
CREATE OR REPLACE FUNCTION agents.price_model_key(model_id text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(
    regexp_replace(
        regexp_replace(
            regexp_replace(lower(model_id), '^[[:space:]]+|[[:space:]]+$', '', 'g'),
            '^(.+)@[0-9]+$', '\1'),
        '^.*@([^@]+)$|^(.*)@$', '\1\2'),
    '^.*/([^/]+)$', '\1');

CREATE OR REPLACE VIEW agents.usage_price AS
SELECT u.event_id, u.ts, u.machine, u.agent, u.model,
       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
       u.reasoning_tokens, u.total_tokens, u.est_cost_usd AS logged_cost_usd,
       p.synced_at AS rate_synced_at,
       totals.coverage_tokens,
       c.priced_tokens,
       totals.coverage_tokens - c.priced_tokens AS unpriced_tokens,
       totals.coverage_tokens - c.component_tokens AS unattributed_tokens,
       CASE WHEN totals.coverage_tokens = 0 THEN 0 ELSE c.known_cost END AS known_api_cost_usd,
       CASE WHEN totals.coverage_tokens = c.priced_tokens
            THEN coalesce(c.known_cost, 0) END AS estimated_api_cost_usd
FROM agents.usage_event_dedup u
LEFT JOIN agents.model_price p ON p.model_key = agents.price_model_key(u.model)
CROSS JOIN LATERAL (
    SELECT sum(tokens) AS component_tokens,
           coalesce(sum(tokens) FILTER (WHERE rate >= 0 AND rate < 'Infinity'::numeric), 0)
               AS priced_tokens,
           sum(tokens * rate) FILTER (WHERE tokens > 0 AND rate >= 0 AND rate < 'Infinity'::numeric)
               AS known_cost
    FROM (VALUES (u.input_tokens, p.input_cost), (u.output_tokens, p.output_cost),
                 (u.cache_read_tokens, p.cache_read_cost), (u.cache_write_tokens, p.cache_write_cost))
         AS components(tokens, rate)
) c
CROSS JOIN LATERAL (SELECT greatest(u.total_tokens, c.component_tokens) AS coverage_tokens) totals
WHERE u.kind = 'agent';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_ro') THEN
        GRANT SELECT ON agents.model_price, agents.usage_price TO agents_ro;
    END IF;
END $$;

COMMIT;
