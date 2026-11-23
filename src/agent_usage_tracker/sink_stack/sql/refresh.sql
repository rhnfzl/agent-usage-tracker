-- Refresh job for the agents database (hourly in the Compose stack). Run it as postgres (psql -f,
-- ON_ERROR_STOP), so any error exits non-zero and the scheduler can alert on the failure.
-- Idempotent.
\set ON_ERROR_STOP on

-- One statement preserves both cron atomicity and a caller's transaction boundary.
DO $refresh$
DECLARE
    refresh_started_at timestamptz;
    extra record;
BEGIN
PERFORM pg_advisory_xact_lock(hashtext('agents.derived_refresh'));
refresh_started_at := clock_timestamp();

-- Claude Code sometimes re-logs one message under a second ts. The (event_id, ts) key keeps both
-- rows, so every raw sum counts that turn twice. Keep the row agents.usage_event_dedup keeps and
-- delete the other copies: Grafana's raw panels and usage_daily (its hourly policy re-materializes
-- the changed days) then count it once. Only that known case is settled here; any other duplicate
-- stays, for audit_daily.dup_unexplained to report.
-- One DELETE per copy, with literal values: TimescaleDB then decompresses only the compressed
-- batch that holds the row. A joined DELETE decompresses whole chunks of old history.
FOR extra IN
    SELECT event_id, ts FROM (
        SELECT event_id, ts,
               row_number() OVER (PARTITION BY event_id ORDER BY total_tokens DESC, ts DESC) AS rn
        FROM agents.usage_event
        WHERE event_id IN (SELECT event_id FROM agents.usage_event GROUP BY event_id
                           HAVING count(*) > 1 AND bool_and(agent = 'claude_code')
                              AND count(DISTINCT machine) = 1)
    ) ranked
    WHERE rn > 1
LOOP
    -- ts written as UTC ISO text: the session's DateStyle and zone abbreviations cannot shift it.
    EXECUTE format('DELETE FROM agents.usage_event WHERE agent = %L AND event_id = %L AND ts = %L::timestamptz',
                   'claude_code', extra.event_id,
                   to_char(timezone('UTC', extra.ts), 'YYYY-MM-DD HH24:MI:SS.US') || '+00');
END LOOP;

-- Window titles are kept indefinitely by default. To turn a retention limit on, add here:
--   UPDATE agents.time_interval SET title = NULL
--    WHERE ts < now() - interval '<N> days' AND title IS NOT NULL;
-- (time_interval_title_ts_idx keeps that UPDATE cheap.)

-- Calibrate the steer rules first, so the refresh below already uses the newest parameters.
-- 01_schema.sql explains the rules and the guard.
PERFORM agents.time_calibrate();

-- Rebuild the per-day totals Grafana reads (01_schema.sql explains why it is materialized).
REFRESH MATERIALIZED VIEW CONCURRENTLY agents.active_daily;

-- Rebuild episodes within the same transaction. A failed rebuild publishes no partial snapshot.
PERFORM agents.episode_link();

-- Rebuild the phase 5 views Grafana reads (03_effort.sql). After the episodes, which
-- effort and episode cost count; the model rollup first, coverage_monthly reads it.
REFRESH MATERIALIZED VIEW CONCURRENTLY agents.tokens_cost_by_model_daily;
REFRESH MATERIALIZED VIEW CONCURRENTLY agents.effort_period;
REFRESH MATERIALIZED VIEW CONCURRENTLY agents.tokens_cost_by_episode;
REFRESH MATERIALIZED VIEW CONCURRENTLY agents.audit_daily;

INSERT INTO agents.collector_scan (machine, collector, started_at, finished_at, status)
VALUES ('sink', 'episode_link', refresh_started_at, clock_timestamp(), 'ok');

END
$refresh$;
