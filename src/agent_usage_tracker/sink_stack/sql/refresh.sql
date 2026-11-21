-- Refresh job for the agents database (hourly in the Compose stack). Run it as postgres (psql -f,
-- ON_ERROR_STOP), so any error exits non-zero and the scheduler can alert on the failure.
-- Idempotent.
\set ON_ERROR_STOP on

-- One statement preserves both cron atomicity and a caller's transaction boundary.
DO $refresh$
DECLARE
    refresh_started_at timestamptz;
BEGIN
PERFORM pg_advisory_xact_lock(hashtext('agents.derived_refresh'));
refresh_started_at := clock_timestamp();

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

-- Labels may use only scans completed before this refresh began.
INSERT INTO agents.collector_scan (machine, collector, started_at, finished_at, status)
VALUES ('sink', 'episode_link', refresh_started_at, clock_timestamp(), 'ok');

END
$refresh$;
