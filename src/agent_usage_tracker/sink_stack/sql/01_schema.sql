-- agents.usage_event: coding-agent token + model usage (Claude Code, Codex, Cursor, Pi).
--
-- Fed by the standalone `agent-usage-tracker` (aut) collector on each dev machine.
-- Isolation-first: this lives in its own database `agents` (its own failure domain), not
-- co-tenanted with unrelated data.
--
-- Idempotent + safe to re-run. Applied via psql -f (see the grants file for roles).
-- One-time bootstrap as superuser:
--     CREATE DATABASE agents;
--     \c agents
--     then apply the files with apply.sh (sink_stack/), which also creates the roles
--
-- Note: every USD figure is an estimate (API-equivalent). Subscriptions are flat-rate, so
-- these numbers are notional, never a real bill.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS agents;

CREATE TABLE IF NOT EXISTS agents.usage_event (
    event_id            text        NOT NULL,   -- deterministic idempotency key from the collector
    ts                  timestamptz NOT NULL,   -- from the source log line (hypertable partition col)
    machine             text        NOT NULL,   -- role-based label (laptop, buildbox, ...)
    agent               text        NOT NULL,   -- claude_code | codex | cursor | pi
    kind                text        NOT NULL DEFAULT 'agent',  -- 'agent' = assistant turn (tokens); 'human' = a prompt the user sent (0 tokens)
    model               text,
    provider            text,
    input_tokens        bigint      NOT NULL DEFAULT 0,   -- fresh (non-cached) prompt tokens
    output_tokens       bigint      NOT NULL DEFAULT 0,
    cache_read_tokens   bigint      NOT NULL DEFAULT 0,
    cache_write_tokens  bigint      NOT NULL DEFAULT 0,
    reasoning_tokens    bigint,                            -- codex only; null elsewhere
    total_tokens        bigint      NOT NULL DEFAULT 0,
    est_cost_usd        numeric(12,6),                     -- NOTIONAL / API-equivalent
    project             text,                              -- working-dir basename, never a full path
    -- Timescale requires the partition column in any unique index, so the dedup key is
    -- (event_id, ts). event_id is deterministic + ts is immutable, so this is stable.
    PRIMARY KEY (event_id, ts)
);

SELECT create_hypertable('agents.usage_event', 'ts', if_not_exists => TRUE);

-- Upgrade path: CREATE TABLE IF NOT EXISTS above is a no-op on an existing DB, so the kind
-- column defined in it never lands there without this idempotent ADD COLUMN.
ALTER TABLE agents.usage_event ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'agent';

-- Compression keeps disk use and WAL churn low. That is what protects the disk; retention
-- never was.
--
-- Raw rows are now kept FOREVER. The old policy dropped them after 1825 days, which was only
-- ever a stand-in for "lifetime-ish". Two things make it wrong to keep:
--
--   * It cannot be undone. Everything raw carries and the daily rollup does not -- the hour of
--     day, the project, reasoning tokens, one row per turn -- is gone the moment a chunk is
--     dropped. The rollup buckets to a whole day, so it can say "41 human turns on the 3rd" but
--     never when the first one landed. That per-turn timestamp is the presence signal behind
--     active-time reporting, so a silent drop at year five would quietly destroy the evidence.
--   * It buys little. Compressed raw rows are small, so keeping them forever costs little disk.
--
-- remove_retention_policy is idempotent via if_exists, so this also retires the policy on hosts
-- that already have it rather than only affecting fresh installs.
ALTER TABLE agents.usage_event SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'agent, model',
    timescaledb.compress_orderby = 'ts DESC, event_id'
);
SELECT add_compression_policy('agents.usage_event', INTERVAL '14 days', if_not_exists => TRUE);
SELECT remove_retention_policy('agents.usage_event', if_exists => TRUE);

-- Daily rollup for Grafana and other stats readers. Cheap to query and kept forever; raw is
-- kept forever too now, so this is a convenience, no longer the only long-term survivor.
-- Upgrade path: a continuous aggregate cannot ALTER its GROUP BY to add a column, and the
-- CREATE ... IF NOT EXISTS below is a no-op if an OLD (pre-kind) usage_daily already exists.
-- So drop the old one first, but ONLY if it lacks the kind column (fresh installs have no
-- view yet; already-migrated DBs keep theirs untouched). DROP is fine in plpgsql; the cagg
-- CREATE stays at top level (TimescaleDB disallows it inside a transaction block).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'agents' AND table_name = 'usage_daily' AND column_name = 'day')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'agents' AND table_name = 'usage_daily' AND column_name = 'kind')
    THEN
        EXECUTE 'DROP MATERIALIZED VIEW agents.usage_daily CASCADE';
    END IF;
END $$;

CREATE MATERIALIZED VIEW IF NOT EXISTS agents.usage_daily
WITH (timescaledb.continuous) AS
-- kind is a grouping dimension so "turns" can be split into agent vs human; human turns
-- carry zero tokens, so the token sums are unaffected by their presence.
SELECT time_bucket('1 day', ts) AS day,
       machine, agent, model, kind,
       sum(input_tokens)       AS input_tokens,
       sum(output_tokens)      AS output_tokens,
       sum(cache_read_tokens)  AS cache_read_tokens,
       sum(cache_write_tokens) AS cache_write_tokens,
       sum(total_tokens)       AS total_tokens,
       sum(est_cost_usd)       AS est_cost_usd,
       count(*)                AS turns
FROM agents.usage_event
GROUP BY day, machine, agent, model, kind
WITH NO DATA;

-- Real-time aggregation (NOT materialized_only). With a 1-day bucket and a 1h
-- end_offset, the scheduled policy only materializes buckets that are fully
-- enclosed by [start, now-1h], so TODAY's bucket is never materialized until
-- after midnight UTC. materialized_only=true would then hide the current day
-- from every Grafana panel that reads this view (a "Last 24 hours" overview
-- would blank out all day). Real-time mode computes today's partial bucket live
-- from the raw hypertable, so the dashboard always reflects current usage.
-- Idempotent: safe to re-run.
ALTER MATERIALIZED VIEW agents.usage_daily SET (timescaledb.materialized_only = false);

-- Refresh window covers ALL history (start_offset NULL). Rows can arrive days or months late
-- with old timestamps: a machine whose logs sync after days offline, or a backfill. With a
-- bounded (e.g. 3-day) window those rows never reach the rollup. The policy re-materializes
-- only invalidated ranges, so the wide
-- window costs nothing when nothing changed. if_not_exists keeps an old policy as it is, so an
-- existing policy with a bounded start_offset is removed first. Idempotent.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM timescaledb_information.jobs j     -- lists a cagg job under the view's own name
         WHERE j.hypertable_schema = 'agents' AND j.hypertable_name = 'usage_daily'
           AND j.proc_name = 'policy_refresh_continuous_aggregate'
           AND j.config->>'start_offset' IS NOT NULL)
    THEN
        PERFORM remove_continuous_aggregate_policy('agents.usage_daily');
    END IF;
END $$;

SELECT add_continuous_aggregate_policy('agents.usage_daily',
    start_offset      => NULL,
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE);

-- Materialize ALL existing history once. The hourly policy above refreshes only invalidated
-- ranges, and a fresh CREATE / recreate (WITH NO DATA) invalidates nothing, so older buckets would never be materialized and
-- the rollup would silently start at "three days ago" while Grafana showed it as the full
-- history. Raw is no longer dropped, so this is recoverable now rather than terminal -- still
-- do it, or every dashboard reading usage_daily is wrong until someone notices. No-op on an
-- empty DB. Must run outside a txn block: psql -f autocommits each statement, so this is fine.
CALL refresh_continuous_aggregate('agents.usage_daily', NULL, NULL);

-- One row per event_id. Claude Code sometimes re-logs the same message with a different ts,
-- and the hypertable key is (event_id, ts) (Timescale needs ts in it), so both rows persist and
-- every sum over usage_event counts that turn twice. This view settles every duplicate at read
-- time: it keeps the row with the largest total_tokens (ts DESC breaks ties, and is unique
-- per event_id because of the primary key); agents.audit_daily checks what it settles. Derived views and new queries should
-- read this view, not usage_event. usage_daily sums raw rows (a continuous aggregate
-- cannot read a view); the refresh job (refresh.sql) deletes Claude Code's re-logged copies from
-- the table, so it and usage_daily count them once after the next run. Idempotent: CREATE OR REPLACE.
CREATE OR REPLACE VIEW agents.usage_event_dedup AS
SELECT DISTINCT ON (event_id) *
FROM agents.usage_event
ORDER BY event_id, total_tokens DESC, ts DESC;

-- Re-grant Grafana read: the DO-block recreate above uses DROP ... CASCADE, which removes the
-- agents_ro grant. The grants file is normally applied right after this file; this block
-- covers a manual `psql -f` of this file on its own. Guarded so a first-time install
-- does not error here: the roles may not exist yet when this file runs.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_ro') THEN
        GRANT SELECT ON agents.usage_daily TO agents_ro;
        GRANT SELECT ON agents.usage_event_dedup TO agents_ro;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_writer') THEN
        GRANT SELECT ON agents.usage_event_dedup TO agents_writer;
    END IF;
END $$;

-- One row per aut scan, per machine and collector: proof that the collector RAN, and how it
-- ended. usage_event alone cannot tell a machine whose aut died from a quiet week; this can.
-- aut writes it after each scan over the same DSN as usage_event, best effort: a failed
-- heartbeat insert never blocks or fails usage delivery. error is short (the client truncates
-- it to 300 chars and strips DSN/URL credentials). A freshness monitoring check can watch it.
-- Plain table, not a hypertable: one row per collector per scan, so roughly 4 to 600 rows per
-- machine per day depending on the scan interval (hours, or 10 to 15 min with the shipped
-- timers), on the order of 200k rows per machine per year.
-- shortcut: no retention; add a delete of rows older than 90 days (or make it a hypertable)
-- once the table passes about 1M rows.
CREATE TABLE IF NOT EXISTS agents.collector_scan (
    machine      text        NOT NULL,
    collector    text        NOT NULL,   -- claude_code | codex | cursor | pi | episode_link
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL,
    status       text        NOT NULL CHECK (status IN ('ok', 'error', 'degraded')),
    events       integer     NOT NULL DEFAULT 0,   -- new events this scan
    human        integer     NOT NULL DEFAULT 0,   -- of which human prompts
    newest_ts    timestamptz,                      -- newest valid event ts seen this scan
    error        text,
    aut_version  text,
    PRIMARY KEY (machine, collector, finished_at)
);
-- 'degraded': the collector ran but could not read a source that is there (a stopped aw-server, an
-- expired token). CREATE above does not change a table that exists, so widen the check here.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'agents.collector_scan'::regclass
                     AND conname = 'collector_scan_status_check'
                     AND pg_get_constraintdef(oid) LIKE '%degraded%') THEN
        ALTER TABLE agents.collector_scan DROP CONSTRAINT IF EXISTS collector_scan_status_check;
        ALTER TABLE agents.collector_scan ADD CONSTRAINT collector_scan_status_check
            CHECK (status IN ('ok', 'error', 'degraded'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS collector_scan_finished_at_idx ON agents.collector_scan (finished_at);

-- Session identity per event, keyed by event_id. It cannot live on usage_event: those rows are
-- never updated (the writer is INSERT only and the sink is ON CONFLICT DO NOTHING), so a column
-- added there would stay NULL on every existing row forever. Join it to agents.usage_event_dedup
-- on event_id. Nothing writes it yet; the upcoming aut steps do.
CREATE TABLE IF NOT EXISTS agents.event_session (
    event_id           text        PRIMARY KEY,
    machine            text        NOT NULL,
    agent              text        NOT NULL,
    session_id         text        NOT NULL,
    parent_session_id  text,
    run_origin         text        NOT NULL
                       CHECK (run_origin IN ('interactive', 'automated', 'subagent', 'unknown')),
    turn_id            text,
    first_seen         timestamptz NOT NULL DEFAULT now()
);

-- Non-token activity (steer, interrupt, commit, pr, tool_error, turn, invoke, ...). The kinds
-- arrive in later steps; nothing writes this yet. attrs carries non-text evidence only (counts,
-- flags, ids), never prompt or output text. Same (event_id, ts) key rule as usage_event.
-- No compression or retention policy yet: decide once there is real volume to size it against.
CREATE TABLE IF NOT EXISTS agents.activity_event (
    event_id    text        NOT NULL,
    ts          timestamptz NOT NULL,
    machine     text        NOT NULL,
    agent       text        NOT NULL,
    kind        text        NOT NULL,
    session_id  text,
    turn_id     text,
    attrs       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (event_id, ts)
);

SELECT create_hypertable('agents.activity_event', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS activity_event_machine_agent_session_idx
    ON agents.activity_event (machine, agent, session_id);

-- One row per (machine, collector): when it last scanned cleanly, when it last failed, and
-- what the latest scan said. newest_ts is over ok scans only, so a failing scan cannot claim
-- data it never delivered. Idempotent: CREATE OR REPLACE.
CREATE OR REPLACE VIEW agents.collector_coverage AS
SELECT machine, collector,
       max(finished_at) FILTER (WHERE status = 'ok')    AS last_ok_at,
       max(finished_at) FILTER (WHERE status = 'error') AS last_error_at,
       (array_agg(status ORDER BY finished_at DESC))[1] AS last_status,
       count(*) FILTER (WHERE status = 'ok' AND finished_at > now() - interval '7 days')
                                                        AS ok_scans_7d,
       max(newest_ts) FILTER (WHERE status = 'ok')      AS newest_ts,
       (array_agg(aut_version ORDER BY finished_at DESC))[1] AS aut_version
FROM agents.collector_scan
GROUP BY machine, collector;

-- ---------------------------------------------------------------------------------------------
-- Active time (phase 2). Active time is the UNION of intervals from three sources: steer
-- islands (derived from activity_event below), ActivityWatch not-AFK window time and manual
-- entries (both rows of time_interval). Intervals are merged across all machines and agents
-- before summing, so two agents steered in parallel never count as two hours. Agent runtime is
-- never human time: only the human's own steers, window time and manual entries feed this.
-- Days are local days in the sink time zone (AUT_TZ). No view here exposes machine-level detail or titles.
-- ---------------------------------------------------------------------------------------------

-- One row per interval aut delivers: source 'aw' (ActivityWatch window time intersected with
-- not-AFK, clipped at scan range edges) or 'manual' (`aut time add`). title is PRIVATE: redacted
-- on the machine before push, and never readable by agents_ro (the public view drops it). Kept
-- for good by default; the refresh job (refresh.sql) says how to turn a limit on. Remember that
-- backups also hold titles. Same (event_id, ts) key
-- rule as usage_event. No compression policy on purpose: a later title UPDATE must stay a plain
-- row update, and compressed chunks would turn it into a decompress-and-rewrite.
-- Producers must drop pieces with end <= start before insert: aut delivers every table in one
-- transaction, so a single row failing CHECK (end_ts > ts) rolls back the whole delivery and
-- fails again on every retry of that range.
CREATE TABLE IF NOT EXISTS agents.time_interval (
    event_id     text        NOT NULL,
    ts           timestamptz NOT NULL,   -- interval start (hypertable partition col)
    end_ts       timestamptz NOT NULL,
    machine      text        NOT NULL,
    source       text        NOT NULL CHECK (source IN ('aw', 'manual')),
    app          text,
    title        text,
    category     text,
    work_relevant boolean    NOT NULL,
    attrs        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (event_id, ts),
    CHECK (end_ts > ts)
);

SELECT create_hypertable('agents.time_interval', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS time_interval_ts_end_idx ON agents.time_interval (ts, end_ts);
-- The nightly title UPDATE reads only rows that still hold a title (about one day's worth), not
-- the whole history with titles already nulled.
CREATE INDEX IF NOT EXISTS time_interval_title_ts_idx ON agents.time_interval (ts) WHERE title IS NOT NULL;

-- Everything agents_ro may read about time_interval: every column except title and event_id.
-- aut builds event_id from a hash of the title, so with machine, app, ts and end_ts beside it a
-- reader could confirm a guessed title, even after the title is nulled.
-- One-time migration: the first version exposed event_id, and CREATE OR REPLACE cannot drop a
-- column. Nothing depends on the view; the grants file (run after this file) re-grants it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'agents' AND table_name = 'time_interval_public'
                 AND column_name = 'event_id') THEN
        EXECUTE 'DROP VIEW agents.time_interval_public';
    END IF;
END $$;
CREATE OR REPLACE VIEW agents.time_interval_public AS
SELECT ts, end_ts, machine, source, app, category, work_relevant, attrs
FROM agents.time_interval;

-- Parameters of the steer rules (phase 2b). The LATEST row (by computed_at) is in force for
-- steer_lead / steer_interval and everything built on them. The 'default' row holds the
-- hand-picked phase 2 numbers; the refresh job (refresh.sql) adds a 'calibrated' row (agents.time_calibrate
-- below) only when the data is big enough and says something new. Rows are never updated or
-- deleted, so the table is the history of the parameters. No titles here: agents_ro may read it.
CREATE TABLE IF NOT EXISTS agents.time_params (
    computed_at   timestamptz PRIMARY KEY,
    gap_minutes   numeric     NOT NULL,   -- a new island starts after a longer idle gap
    wpm           numeric     NOT NULL,   -- typing speed behind the lead-in
    lead_min_s    int         NOT NULL,   -- lead-in clamp, seconds
    lead_max_s    int         NOT NULL,
    n_gaps        int         NOT NULL DEFAULT 0,   -- sample sizes behind a calibrated row
    n_leads       int         NOT NULL DEFAULT 0,
    gap_agreement numeric     CHECK (gap_agreement BETWEEN 0 AND 1),
    window_days   int         NOT NULL DEFAULT 0,   -- days with ActivityWatch rows in the window
    source        text        NOT NULL CHECK (source IN ('default', 'calibrated')),
    attrs         jsonb       NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO agents.time_params (computed_at, gap_minutes, wpm, lead_min_s, lead_max_s, source)
VALUES ('1970-01-01 00:00+00', 15, 40, 30, 600, 'default')
ON CONFLICT (computed_at) DO NOTHING;

-- Each human steer with its typing lead-in start: ts - lead, lead = words at the latest wpm
-- clamped to lead_min_s .. lead_max_s, or 1 minute when the row has no word count. A non-numeric
-- words value falls back to 1 min rather than failing every query that reads this view.
CREATE OR REPLACE VIEW agents.steer_lead AS
SELECT e.ts,
       e.ts - make_interval(secs => CASE
           WHEN e.attrs->>'words' ~ '^[0-9]+(\.[0-9]+)?$'
           THEN least(greatest((e.attrs->>'words')::numeric * 60 / p.wpm, p.lead_min_s), p.lead_max_s)::float8
           ELSE 60 END) AS start_ts
FROM agents.activity_event e
CROSS JOIN (SELECT wpm, lead_min_s, lead_max_s FROM agents.time_params ORDER BY computed_at DESC LIMIT 1) p
WHERE e.kind = 'steer';

-- Steer islands. Steers are ordered by ts across ALL agents and machines; a new island starts
-- when a steer's lead-in starts more than gap_minutes (latest time_params row) after the previous
-- steer. Island = [min(lead-in start), max(ts)].
CREATE OR REPLACE VIEW agents.steer_interval AS
WITH g AS (
    SELECT ts, start_ts,
           count(*) FILTER (WHERE prev_ts IS NULL OR start_ts > prev_ts + gap)
               OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING) AS island
    FROM (SELECT l.ts, l.start_ts, lag(l.ts) OVER (ORDER BY l.ts) AS prev_ts,
                 p.gap_minutes * interval '1 minute' AS gap
          FROM agents.steer_lead l
          CROSS JOIN (SELECT gap_minutes FROM agents.time_params ORDER BY computed_at DESC LIMIT 1) p) x
)
SELECT min(start_ts) AS start_ts, max(ts) AS end_ts, count(*) AS steers
FROM g
GROUP BY island;

-- Apps learned as work (phase 2b): ActivityWatch apps in focus at the moment of at least 3
-- steers on at least 2 local days (AUT_TZ). An app is never learned when ANY of its aw rows is a
-- browser (rule browser_*, the title rules decide there) or comes from the user's own mapping
-- (rule config_app: that mapping always wins, also over the app's rows from before it). The app in
-- focus is the newest row on the steer's own machine, else the newest row on any machine (machine
-- is used here only, never exposed).
-- shortcut: looks at the 20 newest aw rows starting at or before each steer, so with many
-- machines interleaving short rows a long focus row can be missed; switch to a per-machine lookup
-- when more than about 3 machines run ActivityWatch at once.
CREATE OR REPLACE VIEW agents.app_learned AS
SELECT f.app, count(*) AS n_steers,
       count(DISTINCT (s.ts AT TIME ZONE '@AUT_TZ@')::date) AS n_days,
       min(s.ts) AS first_seen
FROM agents.activity_event s
CROSS JOIN LATERAL (
    SELECT c.app FROM (
        SELECT t.app, t.machine, t.ts, t.end_ts
        FROM agents.time_interval t
        WHERE t.source = 'aw' AND t.ts <= s.ts
        ORDER BY t.ts DESC LIMIT 20) c
    WHERE c.end_ts > s.ts
    ORDER BY c.machine = s.machine DESC NULLS LAST, c.ts DESC LIMIT 1) f
WHERE s.kind = 'steer' AND f.app IS NOT NULL
GROUP BY f.app
HAVING count(*) >= 3
   AND count(DISTINCT (s.ts AT TIME ZONE '@AUT_TZ@')::date) >= 2
   AND f.app NOT IN (SELECT x.app FROM agents.time_interval x
                     WHERE x.source = 'aw' AND x.app IS NOT NULL
                       AND (x.attrs->>'rule' LIKE 'browser\_%' OR x.attrs->>'rule' = 'config_app'));

-- time_interval as the active-time views see it: a row the collector could not place (rule
-- unknown or macos_category) counts as work when its app is learned. Every other row keeps the
-- collector's decision. No title, no title-derived event_id (same columns as time_interval_public).
CREATE OR REPLACE VIEW agents.time_interval_effective AS
SELECT t.ts, t.end_ts, t.machine, t.source, t.app, t.category,
       t.work_relevant OR (t.attrs->>'rule' IN ('unknown', 'macos_category')
                           AND t.app IN (SELECT app FROM agents.app_learned)) AS work_relevant,
       t.attrs
FROM agents.time_interval t;

-- Merged active intervals, one row per merged interval per variant. 'all' = steer islands + all
-- time_interval rows; 'relevant' = steer islands + work_relevant rows of time_interval_effective
-- (learned apps included). Overlapping or touching intervals merge (gaps and islands on start
-- with a running max of end). sources lists which of steer / aw / manual contributed. Re-merges
-- the whole history on every read (slow over months of data, see active_daily below): drill-down only.
CREATE OR REPLACE VIEW agents.active_interval AS
WITH src AS (
    SELECT v.variant, s.start_ts, s.end_ts, 'steer'::text AS source
    FROM agents.steer_interval s CROSS JOIN (VALUES ('all'), ('relevant')) AS v(variant)
    UNION ALL
    SELECT 'all', ts, end_ts, source FROM agents.time_interval
    UNION ALL
    SELECT 'relevant', ts, end_ts, source FROM agents.time_interval_effective WHERE work_relevant
), m AS (
    SELECT variant, start_ts, end_ts, source,
           max(end_ts) OVER (PARTITION BY variant ORDER BY start_ts, end_ts
                             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM src
), g AS (
    SELECT variant, start_ts, end_ts, source,
           count(*) FILTER (WHERE prev_end IS NULL OR start_ts > prev_end)
               OVER (PARTITION BY variant ORDER BY start_ts, end_ts ROWS UNBOUNDED PRECEDING) AS grp
    FROM m
)
SELECT variant, min(start_ts) AS start_ts, max(end_ts) AS end_ts,
       array_agg(DISTINCT source ORDER BY source) AS sources
FROM g
GROUP BY variant, grp;

-- Minutes per local day (sink time zone, AUT_TZ), computed live. minutes_all / minutes_relevant are the merged
-- totals; the per-source columns are summed BEFORE merging (so they can add up to more than
-- minutes_all) and exist for diagnosis only. An interval that crosses local midnight is split at
-- the boundary, DST days included (the day bounds are local midnights converted back to
-- timestamptz). Exact but slow over a long history (every read re-merges all of it); Grafana
-- reads the materialized agents.active_daily below.
CREATE OR REPLACE VIEW agents.active_daily_live AS
WITH pieces AS (
    SELECT variant AS bucket, start_ts, end_ts FROM agents.active_interval
    UNION ALL
    SELECT 'steer', start_ts, end_ts FROM agents.steer_interval
    UNION ALL
    SELECT source, ts, end_ts FROM agents.time_interval
), split AS (
    SELECT p.bucket, d::date AS day,
           greatest(p.start_ts, d AT TIME ZONE '@AUT_TZ@') AS s,
           least(p.end_ts, (d + interval '1 day') AT TIME ZONE '@AUT_TZ@') AS e
    FROM pieces p
    CROSS JOIN LATERAL generate_series(
        date_trunc('day', p.start_ts AT TIME ZONE '@AUT_TZ@'),
        date_trunc('day', p.end_ts AT TIME ZONE '@AUT_TZ@'),
        interval '1 day') AS d
)
SELECT day,
       round(coalesce(sum(extract(epoch FROM e - s)) FILTER (WHERE bucket = 'all'), 0) / 60, 1)      AS minutes_all,
       round(coalesce(sum(extract(epoch FROM e - s)) FILTER (WHERE bucket = 'relevant'), 0) / 60, 1) AS minutes_relevant,
       round(coalesce(sum(extract(epoch FROM e - s)) FILTER (WHERE bucket = 'steer'), 0) / 60, 1)    AS minutes_steer,
       round(coalesce(sum(extract(epoch FROM e - s)) FILTER (WHERE bucket = 'aw'), 0) / 60, 1)       AS minutes_aw,
       round(coalesce(sum(extract(epoch FROM e - s)) FILTER (WHERE bucket = 'manual'), 0) / 60, 1)   AS minutes_manual
FROM split
WHERE e > s
GROUP BY day;

-- agents.active_daily: active_daily_live materialized, so a 12-month Grafana panel reads ~365
-- rows instead of re-merging every interval. With a year of ActivityWatch data the live views
-- take seconds per read while a read of active_daily takes well under a millisecond. So
-- active_interval and active_daily_live are drill-down only (agents_ro may read them, but no
-- dashboard panel should sit on them).
-- Refreshed CONCURRENTLY (readers never block) by the refresh job (refresh.sql); the unique index on
-- day is what CONCURRENTLY needs.
-- shortcut: refreshed on refresh.sql's schedule, so today and late-pushed days lag until the next run;
-- add an hourly refresh entry, or read active_daily_live for recent days, when a live panel matters.
CREATE MATERIALIZED VIEW IF NOT EXISTS agents.active_daily AS
SELECT * FROM agents.active_daily_live
WITH DATA;
CREATE UNIQUE INDEX IF NOT EXISTS active_daily_day_idx ON agents.active_daily (day);

-- ---------------------------------------------------------------------------------------------
-- Calibration (phase 2b): the steer rules learn from ActivityWatch. Run by
-- the refresh job BEFORE the active_daily refresh. Window: aw rows and steers in the 90
-- days before p_end; window_days counts the local days that have aw rows.
--
-- Gap limit. A pair is two consecutive steers (all agents and machines); its gap runs from the
-- earlier steer to the later steer's lead-in start, which is what steer_interval compares with
-- gap_minutes. The lead-in uses the wpm and lead bounds measured in this same run (the latest
-- row's when there are no leads), so the gap limit is chosen against the lead-ins stored with it. Pairs with gap <= 0 (lead-in overlaps,
-- joined whatever the limit) or over 120 minutes are skipped, and so is a pair outside its days'
-- aw span (it must start after that day's first aw row and end before its own day's last aw row
-- end), so a day the collector only covered in part does not read as away. A gap is present when
-- aw rows (any app, any category, overlaps counted once) cover at least half of it. The limit is
-- the candidate 5..60 minutes with the best agreement between (gap <= limit) and present; ties go
-- to the smaller limit.
--
-- Typing speed. For steers with words >= 5 whose ts falls inside an aw row, the lead is how long
-- that row's app had been in focus on that machine right before ts (rows of the same app with
-- gaps under 30 s chain into one run), capped at 15 minutes. Focus time is an upper bound on
-- typing time (reading agent output in the same app counts too), so a lead slower than 10 wpm
-- (the wpm floor) or at the 15-minute cap is dropped as mostly reading; n_leads counts the leads
-- kept. wpm = median(words / lead minutes) of those, clamped to 10..120 and rounded; lead_min_s /
-- lead_max_s = 5th / 95th percentile of the kept leads, clamped to 15..900 s. When a steer sits
-- inside rows on two machines, the steer's own machine wins, else the run that started last.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION agents.time_calibration(p_end timestamptz DEFAULT now())
RETURNS TABLE (gap_minutes numeric, wpm numeric, lead_min_s int, lead_max_s int, n_gaps int,
               n_leads int, gap_agreement numeric, window_days int, agreement_at_latest numeric)
LANGUAGE sql STABLE AS $$
WITH w AS (
    SELECT p_end - interval '90 days' AS w_start
), latest AS (
    SELECT * FROM agents.time_params ORDER BY computed_at DESC LIMIT 1
), aw AS (
    SELECT t.machine, t.ts, t.end_ts, t.app
    FROM agents.time_interval t, w
    WHERE t.source = 'aw' AND t.ts >= w.w_start AND t.ts < p_end
), days AS (
    SELECT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, min(ts) AS first_ts, max(end_ts) AS last_end
    FROM aw GROUP BY 1
), steers AS (
    SELECT e.event_id, e.ts, e.machine,
           CASE WHEN e.attrs->>'words' ~ '^[0-9]+(\.[0-9]+)?$' THEN (e.attrs->>'words')::numeric END AS words
    FROM agents.activity_event e, w
    WHERE e.kind = 'steer' AND e.ts >= w.w_start AND e.ts < p_end
), runs AS (
    SELECT machine, ts, end_ts, min(ts) OVER (PARTITION BY machine, run) AS run_start
    FROM (SELECT machine, ts, end_ts,
                 count(*) FILTER (WHERE prev_app IS DISTINCT FROM app OR ts - prev_end >= interval '30 seconds')
                     OVER (PARTITION BY machine ORDER BY ts, end_ts) AS run
          FROM (SELECT machine, ts, end_ts, app,
                       lag(app) OVER m AS prev_app, lag(end_ts) OVER m AS prev_end
                FROM aw WINDOW m AS (PARTITION BY machine ORDER BY ts, end_ts)) a) r
), ev AS (
    -- aw rows (k = 0) and word-counted steers (k = 1, once per machine) on one timeline, so each
    -- steer finds the newest row starting at or before it by sorting, not by a range join.
    SELECT machine, ts, 0 AS k, end_ts, run_start, NULL::text AS sid, NULL::text AS smachine,
           NULL::numeric AS words FROM runs
    UNION ALL
    SELECT m.machine, s.ts, 1, NULL, NULL, s.event_id, s.machine, s.words
    FROM steers s CROSS JOIN (SELECT DISTINCT machine FROM aw) m
    WHERE s.words >= 5
), grp AS (
    SELECT *, count(*) FILTER (WHERE k = 0) OVER (PARTITION BY machine ORDER BY ts, k) AS g FROM ev
), hit AS (
    SELECT k, sid, ts, words, machine, smachine,
           max(end_ts) OVER (PARTITION BY machine, g) AS row_end,
           max(run_start) OVER (PARTITION BY machine, g) AS run_start
    FROM grp
), leads AS (
    SELECT DISTINCT ON (sid, ts) words, least(extract(epoch FROM ts - run_start), 900) AS lead_s
    FROM hit
    WHERE k = 1 AND row_end > ts AND run_start < ts
    ORDER BY sid, ts, machine = smachine DESC NULLS LAST, run_start DESC
), lstats AS (
    SELECT count(*)::int AS n_leads,
           round(least(greatest(percentile_cont(0.5) WITHIN GROUP (ORDER BY words * 60 / lead_s), 10), 120)::numeric) AS wpm,
           round(least(greatest(percentile_cont(0.05) WITHIN GROUP (ORDER BY lead_s), 15), 900))::int AS lead_min_s,
           round(least(greatest(percentile_cont(0.95) WITHIN GROUP (ORDER BY lead_s), 15), 900))::int AS lead_max_s
    FROM leads
    WHERE lead_s < 900 AND lead_s <= words * 6
), lp AS (
    SELECT coalesce(ls.wpm, lt.wpm) AS wpm, coalesce(ls.lead_min_s, lt.lead_min_s) AS lo,
           coalesce(ls.lead_max_s, lt.lead_max_s) AS hi
    FROM lstats ls, latest lt
), pairs AS (
    -- lead-in start: the steer_lead formula with this run's lead parameters
    SELECT l.prev_ts, l.start_ts
    FROM (SELECT s.ts, lag(s.ts) OVER (ORDER BY s.ts) AS prev_ts,
                 s.ts - make_interval(secs => CASE WHEN s.words IS NOT NULL
                     THEN least(greatest(s.words * 60 / lp.wpm, lp.lo), lp.hi)::float8 ELSE 60 END) AS start_ts
          FROM steers s, lp) l
    JOIN days d1 ON d1.day = (l.prev_ts AT TIME ZONE '@AUT_TZ@')::date AND l.prev_ts >= d1.first_ts
    JOIN days d2 ON d2.day = (l.start_ts AT TIME ZONE '@AUT_TZ@')::date AND l.start_ts <= d2.last_end
    WHERE l.start_ts > l.prev_ts AND l.start_ts <= l.prev_ts + interval '120 minutes'
), long_rows AS MATERIALIZED (
    SELECT ts, end_ts FROM aw WHERE end_ts - ts > interval '2 hours'
), cov AS (
    -- Union length of the aw rows inside each gap: each clipped row adds only what lies past the
    -- running max end of the rows before it (so a row found twice adds nothing the second time).
    -- The lower bound on t.ts (longest window row, at most 2 hours) keeps each index lookup short;
    -- the few rows longer than 2 hours come from long_rows instead.
    SELECT prev_ts, start_ts, sum(piece) AS covered
    FROM (SELECT p.prev_ts, p.start_ts,
                 greatest(least(t.end_ts, p.start_ts) - greatest(t.ts, p.prev_ts,
                     max(least(t.end_ts, p.start_ts)) OVER (
                         PARTITION BY p.prev_ts, p.start_ts ORDER BY t.ts, t.end_ts
                         ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)), interval '0') AS piece
          FROM pairs p
          CROSS JOIN LATERAL (
              SELECT t.ts, t.end_ts FROM agents.time_interval t
              WHERE t.source = 'aw' AND t.ts < p.start_ts AND t.end_ts > p.prev_ts
                AND t.ts >= (SELECT w_start FROM w)
                AND t.ts >= p.prev_ts - (SELECT least(max(end_ts - ts), interval '2 hours') FROM aw)
              UNION ALL
              SELECT r.ts, r.end_ts FROM long_rows r WHERE r.ts < p.start_ts AND r.end_ts > p.prev_ts) t) x
    GROUP BY prev_ts, start_ts
), gaps AS (
    SELECT p.start_ts - p.prev_ts AS g,
           coalesce(c.covered, interval '0') * 2 >= p.start_ts - p.prev_ts AS present
    FROM pairs p LEFT JOIN cov c USING (prev_ts, start_ts)
), best AS (
    SELECT l, avg(((g <= make_interval(mins => l)) = present)::int) AS agreement
    FROM gaps CROSS JOIN generate_series(5, 60) AS l
    GROUP BY l
    ORDER BY agreement DESC, l
    LIMIT 1
)
SELECT b.l::numeric, ls.wpm, ls.lead_min_s, ls.lead_max_s,
       (SELECT count(*) FROM gaps)::int, ls.n_leads, round(b.agreement, 4),
       (SELECT count(*) FROM days)::int,
       (SELECT round(avg(((g.g <= lt.gap_minutes * interval '1 minute') = g.present)::int), 4) FROM gaps g, latest lt)
FROM lstats ls LEFT JOIN best b ON true
$$;

-- Writes a 'calibrated' row only when n_gaps >= 200, n_leads >= 100 and window_days >= 14 (two
-- weeks of ActivityWatch before a row restates all history) and some parameter changes
-- materially; otherwise nothing (the last row stays in force). Hysteresis, so the window sliding
-- by a day or a flat agreement curve does not restate history every night: the gap limit moves
-- only when its agreement beats the latest limit's (agreement_at_latest, same gaps) by 0.02; wpm
-- only when it moves by 10%; the two lead bounds together when either moves by 10%. Unmoved
-- parameters keep the latest row's values. Returns rows written (0 or 1). computed_at is
-- clock_timestamp(), so two runs in one transaction never share a key.
CREATE OR REPLACE FUNCTION agents.time_calibrate(p_end timestamptz DEFAULT now())
RETURNS int
LANGUAGE sql VOLATILE AS $$
WITH c AS (
    SELECT * FROM agents.time_calibration(p_end)
), latest AS (
    SELECT * FROM agents.time_params ORDER BY computed_at DESC LIMIT 1
), v AS (
    SELECT CASE WHEN gap_move THEN c.gap_minutes ELSE l.gap_minutes END AS gap_minutes,
           CASE WHEN gap_move THEN c.gap_agreement ELSE c.agreement_at_latest END AS gap_agreement,
           CASE WHEN abs(c.wpm - l.wpm) >= 0.1 * l.wpm THEN c.wpm ELSE l.wpm END AS wpm,
           CASE WHEN lead_move THEN c.lead_min_s ELSE l.lead_min_s END AS lead_min_s,
           CASE WHEN lead_move THEN c.lead_max_s ELSE l.lead_max_s END AS lead_max_s,
           c.n_gaps, c.n_leads, c.window_days, c.agreement_at_latest
    FROM c, latest l,
         LATERAL (SELECT c.gap_agreement >= c.agreement_at_latest + 0.02 AS gap_move,
                         abs(c.lead_min_s - l.lead_min_s) >= 0.1 * l.lead_min_s
                         OR abs(c.lead_max_s - l.lead_max_s) >= 0.1 * l.lead_max_s AS lead_move) m
    WHERE c.n_gaps >= 200 AND c.n_leads >= 100 AND c.window_days >= 14
), ins AS (
    INSERT INTO agents.time_params (computed_at, gap_minutes, wpm, lead_min_s, lead_max_s, n_gaps,
                                    n_leads, gap_agreement, window_days, source, attrs)
    SELECT clock_timestamp(), v.gap_minutes, v.wpm, v.lead_min_s, v.lead_max_s, v.n_gaps,
           v.n_leads, v.gap_agreement, v.window_days, 'calibrated',
           jsonb_build_object('window_end', p_end, 'agreement_at_previous', v.agreement_at_latest)
    FROM v, latest
    WHERE (v.gap_minutes, v.wpm, v.lead_min_s, v.lead_max_s)
          IS DISTINCT FROM (latest.gap_minutes, latest.wpm, latest.lead_min_s, latest.lead_max_s)
    RETURNING 1
)
SELECT count(*)::int FROM ins
$$;

-- ---------------------------------------------------------------------------------------------
-- Episodes and outcomes (phase 3). An episode is one piece of work across sessions, days and
-- machines: the interactive sessions that built it, the subagents and scripted runs they
-- started, the commits and PRs it made, and what became of them. Nothing here holds a URL,
-- path, title, branch name, ticket id or commit subject: the cross-session keys (repo_key,
-- branch_key, pr_keys) are keyed hashes aut makes with its per-install salt, SHAs are ids.

-- Git outcome per commit, written by `aut outcomes` on a collector machine. Insert-only: a commit moves
-- through states by new rows and readers take the latest decided_at per sha. survival is the
-- share (0..1) of the commit's added lines still attributed to it 14 days after the merge.
-- sha may be the commit row's abbreviation or the full SHA: the linker matches its first 7 hex
-- digits. A commit that never gets a final state keeps its episode 'undecided', and that holds
-- its month out of episode_monthly, so every commit should end in a final state.
CREATE TABLE IF NOT EXISTS agents.commit_outcome (
    sha         text        NOT NULL,
    repo_key    text        NOT NULL,
    state       text        NOT NULL
                CHECK (state IN ('merged', 'reverted', 'reworked', 'open', 'abandoned', 'superseded', 'undecided')),
    survival    numeric     CHECK (survival BETWEEN 0 AND 1),
    decided_at  timestamptz NOT NULL,
    PRIMARY KEY (sha, repo_key, state, decided_at)
);

-- The manual /outcome (`aut outcome shipped|ruled-out|open`) on one session. Insert-only; the
-- latest row of an episode's seed sessions wins over git. repo_key (the current directory's
-- repo) limits it to that session's unit in that repo; NULL, or a repo the session has no unit
-- for, means every unit of the session.
CREATE TABLE IF NOT EXISTS agents.manual_outcome (
    session_id  text        NOT NULL,
    outcome     text        NOT NULL CHECK (outcome IN ('shipped', 'ruled_out', 'open')),
    ts          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, ts)
);
ALTER TABLE agents.manual_outcome ADD COLUMN IF NOT EXISTS repo_key text;

-- Private hand corrections from `aut episodes review`, keys only. Applied by the linker after
-- every automatic edge, in ts order: merge joins the episodes of a_session and b_session; split
-- takes a_session's units out of their episode (one new episode per episode they were in);
-- reject drops a_session's episode (not an episode); confirm marks it as checked. A seed
-- session holds one unit per repo, possibly in different episodes, so the page names the
-- reviewed units: attrs.repo_keys (a_session) and attrs.b_repo_keys (b_session), copied from
-- episode_member.repo_keys. Without them a correction acts on every unit of the session.
CREATE TABLE IF NOT EXISTS agents.episode_correction (
    id          uuid        PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    action      text        NOT NULL CHECK (action IN ('merge', 'split', 'confirm', 'reject')),
    a_session   text        NOT NULL,
    b_session   text,
    attrs       jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- Linker output, rebuilt in full every night by agents.episode_link() (derived data: the
-- inputs are activity_event, event_session and the three tables above).
CREATE TABLE IF NOT EXISTS agents.episode (
    episode_id       uuid        PRIMARY KEY,
    first_ts         timestamptz NOT NULL,
    last_ts          timestamptz NOT NULL,
    n_sessions       integer     NOT NULL,
    n_seed_sessions  integer     NOT NULL,
    n_steers         integer     NOT NULL,
    n_commits        integer     NOT NULL,
    n_prs            integer     NOT NULL,
    outcome          text        NOT NULL
                     CHECK (outcome IN ('shipped', 'reworked', 'open', 'ruled_out', 'no_code', 'undecided')),
    outcome_source   text        NOT NULL CHECK (outcome_source IN ('git', 'manual')),
    confirmed        boolean     NOT NULL
);
CREATE INDEX IF NOT EXISTS episode_first_ts_idx ON agents.episode (first_ts);

CREATE TABLE IF NOT EXISTS agents.episode_member (
    episode_id  uuid  NOT NULL REFERENCES agents.episode (episode_id) ON DELETE CASCADE,
    session_id  text  NOT NULL,
    role        text  NOT NULL CHECK (role IN ('seed', 'child')),
    PRIMARY KEY (episode_id, session_id)
);
-- The repos of the session's units in this episode (a child: its root's), for corrections.
ALTER TABLE agents.episode_member ADD COLUMN IF NOT EXISTS repo_keys text[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS episode_member_session_idx ON agents.episode_member (session_id);

-- The linker. Unit = one seed session x one repo_key (a seed is an interactive session: not a
-- subagent, not a scripted run, not marked role 'child' by aut). A seed session whose rows carry
-- no repo_key gets one '' unit; within a unit, session continuity is the glue. Edges, in trust
-- order: 1 a shared commit key (a link row's commit_keys: a commit its turn printed, or a seed
-- link row's sha_refs key that some turn printed; both are HMACs of a SHA's first 7 hex, so no
-- raw SHA joins), 2 a shared pr key (link pr_keys, or a pr row's host + repo_key + number),
-- 3 a shared branch_key whose next use comes within 14 days of the last. Ticket keys are no
-- edge: they may only confirm a pair another edge already joins, which changes nothing.
-- Union-find refuses an edge that would join two episodes whose spans lie more than 60 days
-- apart, so one long-lived key cannot grow a giant episode. A quoted SHA or a pr key is a soft
-- key: a unit whose soft keys reach more than 2 other units (git log output, a review, a
-- PR-watching session) is a hub and joins only the nearest of them in time, so it never fuses
-- the episodes it looked at. A commit row's SHA counts only as 7 to 40 hex digits and serves
-- the episode's commits and outcome only, never an edge. Children (subagents, scripted runs)
-- never seed or bridge: they attach to their root parent's unit, and of their rows only the
-- commits they printed count (owned by that unit). Corrections apply last. Episode id = md5 of
-- the earliest unit (session_id | repo_key), so it is stable while that unit is. Outcome: the
-- latest manual outcome wins; else reworked if any commit was reverted or reworked, shipped if
-- any merged (survival >= 0.5 where known), open if any is open, ruled_out if every commit was
-- abandoned (superseded pre-amend SHAs aside), undecided while commits wait for a decision, and
-- no_code for episodes without commits (research, review). Runs as postgres from the
-- refresh job; returns one row of counts.
CREATE OR REPLACE FUNCTION agents.episode_link()
RETURNS TABLE (units int, edges int, refused int, episodes int, members int)
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    n          int;
    i          int;
    ra         int;
    rb         int;
    tgt        int;
    next_label int;
    par        int[];
    labs       int[];
    f          timestamptz[];
    l          timestamptz[];
    ed         record;
    cr         record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('agents.derived_refresh'));
    -- Scratch tables are dropped at the end; a failed run rolls their creation back with it.
    refused := 0;

    -- sha: a commit row's SHA when it is one (live rows hold junk like '1' that would join
    -- unrelated sessions).
    CREATE TEMP TABLE _el_row AS
    SELECT session_id, ts, kind, nullif(attrs->>'repo_key', '') AS repo_key, attrs,
           CASE WHEN kind = 'commit' AND lower(attrs->>'sha') ~ '^[0-9a-f]{7,40}$'
                THEN lower(attrs->>'sha') END AS sha
    FROM agents.activity_event
    WHERE kind IN ('link', 'commit', 'pr', 'steer') AND session_id IS NOT NULL;

    -- Seed or child, and the root parent a child attaches to (up to 5 levels of nesting).
    CREATE TEMP TABLE _el_sess AS
    WITH RECURSIVE es AS (
        SELECT session_id, bool_or(run_origin IN ('subagent', 'automated')) AS scripted,
               max(parent_session_id) AS parent
        FROM agents.event_session GROUP BY session_id
    ), ids AS (
        SELECT DISTINCT session_id FROM _el_row
    ), marked AS (
        SELECT DISTINCT session_id FROM _el_row WHERE kind = 'link' AND attrs->>'role' = 'child'
    ), up AS (
        SELECT session_id, session_id AS root, 0 AS depth FROM ids
        UNION ALL
        SELECT up.session_id, es.parent, up.depth + 1
        FROM up JOIN es ON es.session_id = up.root
        WHERE es.parent IS NOT NULL AND up.depth < 5
    ), top AS (
        SELECT DISTINCT ON (session_id) session_id, root FROM up ORDER BY session_id, depth DESC
    )
    SELECT d.session_id, t.root,
           coalesce(es.scripted, false) OR es.parent IS NOT NULL OR m.session_id IS NOT NULL AS is_child
    FROM ids d JOIN top t USING (session_id)
    LEFT JOIN es USING (session_id) LEFT JOIN marked m USING (session_id);

    CREATE TEMP TABLE _el_unit AS
    WITH su AS (
        SELECT r.session_id, coalesce(r.repo_key, '') AS repo_key
        FROM _el_row r JOIN _el_sess s USING (session_id)
        WHERE NOT s.is_child AND r.kind <> 'steer'
        GROUP BY 1, 2
    )
    SELECT (row_number() OVER (ORDER BY session_id, repo_key))::int AS uid, session_id, repo_key
    FROM su
    WHERE repo_key <> ''
       OR NOT EXISTS (SELECT 1 FROM su s2 WHERE s2.session_id = su.session_id AND s2.repo_key <> '');
    CREATE INDEX ON _el_unit (session_id, repo_key);

    -- Where each seed session was, repo by repo, over time.
    CREATE TEMP TABLE _el_pos AS
    SELECT r.session_id, r.ts, u.uid
    FROM _el_row r JOIN _el_unit u ON u.session_id = r.session_id AND u.repo_key = r.repo_key
    WHERE r.kind <> 'steer';
    CREATE INDEX ON _el_pos (session_id);

    -- Every row to a unit of its owner (itself, or a child's root): the unit of the row's repo,
    -- else (no repo, or a repo the owner has no unit for) the owner's unit nearest in time, so a
    -- turn whose repo went unresolved stays with the work around it, else the owner's '' unit.
    -- A child whose root has no unit is in no episode. shortcut: a scripted run with no parent
    -- session (none of them carry one today) is dropped with its commits, so the interactive
    -- episode it ran inside can read no_code; attach it by repo_key + time window to the
    -- overlapping seed unit if such commits ever matter.
    CREATE TEMP TABLE _el_asg AS
    WITH o AS (
        SELECT r.session_id, r.ts, r.kind, r.repo_key, r.attrs, r.sha,
               CASE WHEN s.is_child THEN 'child' ELSE 'seed' END AS role,
               CASE WHEN s.is_child THEN s.root ELSE r.session_id END AS owner
        FROM _el_row r JOIN _el_sess s USING (session_id)
        WHERE NOT (s.is_child AND r.kind = 'steer')
    )
    SELECT o.*, coalesce(
        (SELECT u.uid FROM _el_unit u WHERE u.session_id = o.owner AND u.repo_key = o.repo_key),
        (SELECT p.uid FROM _el_pos p WHERE p.session_id = o.owner
          ORDER BY abs(extract(epoch FROM p.ts - o.ts)), p.uid LIMIT 1),
        (SELECT u.uid FROM _el_unit u WHERE u.session_id = o.owner AND u.repo_key = '')) AS uid
    FROM o;
    DELETE FROM _el_asg WHERE uid IS NULL;

    CREATE TEMP TABLE _el_span AS
    SELECT uid, min(ts) AS t0, max(ts) AS t1 FROM _el_asg GROUP BY uid;

    -- Edge keys. Children add only their printed commits (commit_keys); their quoted SHAs, PRs
    -- and branches never bridge. shortcut: a commit key is the HMAC of a SHA's first 7 hex
    -- digits (aut stores 7 to 9), so two different commits that share 7 digits would join
    -- (about a 5% chance of one such pair over 5k commits); key longer prefixes in aut if a
    -- wrong join is ever found. Only 64-hex keys count: link rows written before aut keyed
    -- them hold raw hex sha_refs and no commit_keys, and join on nothing. So history only joins
    -- by SHA once aut has sent its link rows (aut backfill --activity-only); a session whose
    -- transcript was deleted before that keeps its commits and outcome but is never SHA-linked.
    -- soft: a quoted SHA or a pr key (bridges only through the hub rule below); hard: a
    -- printed commit or a branch.
    CREATE TEMP TABLE _el_key AS
    WITH printed AS (
        SELECT a.uid, a.ts, x AS k
        FROM _el_asg a, jsonb_array_elements_text(CASE WHEN jsonb_typeof(a.attrs->'commit_keys') = 'array'
                                                       THEN a.attrs->'commit_keys' ELSE '[]' END) x
        WHERE a.kind = 'link' AND x ~ '^[0-9a-f]{64}$'
    )
    SELECT 1 AS tier, false AS soft, uid, ts, k FROM printed
    UNION ALL
    SELECT 1, true, a.uid, a.ts, x
    FROM _el_asg a, jsonb_array_elements_text(CASE WHEN jsonb_typeof(a.attrs->'sha_refs') = 'array'
                                                   THEN a.attrs->'sha_refs' ELSE '[]' END) x
    WHERE a.kind = 'link' AND a.role = 'seed' AND x ~ '^[0-9a-f]{64}$'
      AND x IN (SELECT k FROM printed)
    UNION ALL
    SELECT 2, true, a.uid, a.ts, x
    FROM _el_asg a, jsonb_array_elements_text(CASE WHEN jsonb_typeof(a.attrs->'pr_keys') = 'array'
                                                   THEN a.attrs->'pr_keys' ELSE '[]' END) x
    WHERE a.kind = 'link' AND a.role = 'seed'
    UNION ALL
    SELECT 2, true, uid, ts, concat_ws(':', 'pr-row', attrs->>'host', attrs->>'repo_key', attrs->>'number')
    FROM _el_asg WHERE kind = 'pr' AND role = 'seed'
    UNION ALL
    SELECT 3, false, uid, ts, attrs->>'branch_key'
    FROM _el_asg WHERE kind = 'link' AND role = 'seed' AND attrs->>'branch_key' IS NOT NULL;
    CREATE INDEX ON _el_key (tier, k);

    -- Hard keys: each unit to the previous unit (by first use) holding the same key; for a
    -- branch only when this unit's first use comes within 14 days of the latest use before it.
    -- Soft keys: a unit to each unit holding what it quotes (the printer of a SHA, any other
    -- holder of a pr key). A unit with more than 2 such partners is a hub and keeps only the
    -- pair with its nearest partner in time; a pr pair stands only if neither side is a hub
    -- that chose another partner (a SHA's printer owns its commit even when it is a hub).
    CREATE TEMP TABLE _el_edge AS
    WITH ku AS (
        SELECT tier, k, uid, min(ts) AS t0, max(ts) AS t1 FROM _el_key WHERE NOT soft GROUP BY 1, 2, 3
    ), ch AS (
        SELECT tier, uid, t0, lag(uid) OVER w AS prev,
               max(t1) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_t1
        FROM ku WINDOW w AS (PARTITION BY tier, k ORDER BY t0, uid)
    ), sp AS (
        SELECT s.tier, s.uid AS u, h.uid AS v, greatest(min(s.ts), min(h.ts)) AS ts,
               min(abs(extract(epoch FROM s.ts - h.ts))) AS dist
        FROM _el_key s JOIN _el_key h
          ON h.tier = s.tier AND h.k = s.k AND h.uid <> s.uid AND (h.soft = (s.tier = 2))
        WHERE s.soft GROUP BY 1, 2, 3
    ), deg AS (
        SELECT u, count(DISTINCT v) AS n, (array_agg(v ORDER BY dist, v))[1] AS near
        FROM sp GROUP BY u
    )
    SELECT tier, t0 AS ts, prev AS a, uid AS b FROM ch
    WHERE prev IS NOT NULL AND (tier < 3 OR t0 <= prev_t1 + interval '14 days')
    UNION ALL
    SELECT sp.tier, sp.ts, sp.v, sp.u
    FROM sp JOIN deg du ON du.u = sp.u LEFT JOIN deg dv ON dv.u = sp.v
    WHERE (du.n <= 2 OR du.near = sp.v)
      AND (sp.tier = 1 OR (sp.u < sp.v AND (dv.n <= 2 OR dv.near = sp.u)));
    SELECT count(*)::int INTO edges FROM _el_edge;

    -- Union-find over arrays indexed by uid (1..n), with path halving.
    SELECT coalesce(array_agg(uid ORDER BY uid), '{}'), coalesce(array_agg(t0 ORDER BY uid), '{}'),
           coalesce(array_agg(t1 ORDER BY uid), '{}')
      INTO par, f, l FROM _el_span;
    n := coalesce(array_length(par, 1), 0);
    units := n;
    FOR ed IN SELECT a, b FROM _el_edge ORDER BY tier, ts, a, b LOOP
        ra := ed.a;
        WHILE par[ra] <> ra LOOP par[ra] := par[par[ra]]; ra := par[ra]; END LOOP;
        rb := ed.b;
        WHILE par[rb] <> rb LOOP par[rb] := par[par[rb]]; rb := par[rb]; END LOOP;
        CONTINUE WHEN ra = rb;
        IF greatest(f[ra], f[rb]) - least(l[ra], l[rb]) > interval '60 days' THEN
            refused := refused + 1;
            CONTINUE;
        END IF;
        IF rb < ra THEN tgt := ra; ra := rb; rb := tgt; END IF;
        par[rb] := ra;
        f[ra] := least(f[ra], f[rb]);
        l[ra] := greatest(l[ra], l[rb]);
    END LOOP;
    labs := par;
    FOR i IN 1..n LOOP
        ra := i;
        WHILE par[ra] <> ra LOOP ra := par[ra]; END LOOP;
        labs[i] := ra;
    END LOOP;
    CREATE TEMP TABLE _el_lab AS SELECT g AS uid, labs[g] AS lab FROM generate_series(1, n) g;

    -- The units each correction names: side a = a_session's units in attrs.repo_keys (all of
    -- them without it), side b = b_session's in attrs.b_repo_keys.
    CREATE TEMP TABLE _el_cu AS
    SELECT c.id, u.uid,
           CASE WHEN u.session_id = c.a_session
                 AND (NOT c.attrs ? 'repo_keys' OR c.attrs->'repo_keys' ? u.repo_key)
                THEN 'a' ELSE 'b' END AS side
    FROM agents.episode_correction c JOIN _el_unit u
      ON (u.session_id = c.a_session AND (NOT c.attrs ? 'repo_keys' OR c.attrs->'repo_keys' ? u.repo_key))
      OR (u.session_id = c.b_session AND (NOT c.attrs ? 'b_repo_keys' OR c.attrs->'b_repo_keys' ? u.repo_key));

    -- Corrections last, in time order. Labels above n are episodes made by a split.
    next_label := n;
    FOR cr IN SELECT id, action FROM agents.episode_correction
              WHERE action <> 'confirm' ORDER BY ts, id LOOP
        IF cr.action = 'merge' THEN
            CONTINUE WHEN NOT EXISTS (SELECT 1 FROM _el_cu WHERE id = cr.id AND side = 'a')
                       OR NOT EXISTS (SELECT 1 FROM _el_cu WHERE id = cr.id AND side = 'b');
            SELECT min(x.lab) INTO tgt FROM _el_lab x JOIN _el_cu c USING (uid) WHERE c.id = cr.id;
            UPDATE _el_lab SET lab = tgt
             WHERE lab IN (SELECT x.lab FROM _el_lab x JOIN _el_cu c USING (uid) WHERE c.id = cr.id);
        ELSIF cr.action = 'split' THEN
            -- one new episode per episode the named units were in
            UPDATE _el_lab x SET lab = next_label + m.r
              FROM (SELECT y.uid, dense_rank() OVER (ORDER BY y.lab) AS r
                      FROM _el_lab y JOIN _el_cu c USING (uid)
                     WHERE c.id = cr.id AND c.side = 'a' AND y.lab IS NOT NULL) m
             WHERE x.uid = m.uid;
            next_label := next_label + n;  -- n bounds the ranks; labels need not be dense
        ELSE  -- reject
            UPDATE _el_lab SET lab = NULL
             WHERE lab IN (SELECT x.lab FROM _el_lab x JOIN _el_cu c USING (uid)
                            WHERE c.id = cr.id AND c.side = 'a');
        END IF;
    END LOOP;

    CREATE TEMP TABLE _el_ep AS
    SELECT x.uid, md5(first_value(u.session_id || '|' || u.repo_key)
                      OVER (PARTITION BY x.lab ORDER BY s.t0, u.session_id, u.repo_key))::uuid AS episode_id
    FROM _el_lab x JOIN _el_unit u USING (uid) JOIN _el_span s USING (uid)
    WHERE x.lab IS NOT NULL;

    TRUNCATE agents.episode, agents.episode_member;

    INSERT INTO agents.episode (episode_id, first_ts, last_ts, n_sessions, n_seed_sessions, n_steers,
                                n_commits, n_prs, outcome, outcome_source, confirmed)
    WITH base AS (
        SELECT e.episode_id, min(s.t0) AS first_ts, max(s.t1) AS last_ts
        FROM _el_ep e JOIN _el_span s USING (uid) GROUP BY 1
    ), mem AS (
        SELECT e.episode_id, count(DISTINCT a.session_id) AS n_sessions,
               count(DISTINCT a.session_id) FILTER (WHERE a.role = 'seed') AS n_seed,
               count(*) FILTER (WHERE a.kind = 'steer') AS n_steers
        FROM _el_ep e JOIN _el_asg a USING (uid) GROUP BY 1
    ), prs AS (
        SELECT e.episode_id,
               greatest(count(DISTINCT k.k) FILTER (WHERE k.k NOT LIKE 'pr-row:%'),
                        count(DISTINCT k.k) FILTER (WHERE k.k LIKE 'pr-row:%')) AS n_prs
        FROM _el_ep e JOIN _el_key k USING (uid) WHERE k.tier = 2 GROUP BY 1
    ), co AS (  -- the commit rows' 7-digit prefix, so any sha form `aut outcomes` writes joins
        SELECT DISTINCT ON (left(lower(sha), 7)) left(lower(sha), 7) AS k, state, survival
        FROM agents.commit_outcome ORDER BY left(lower(sha), 7), decided_at DESC,
            CASE state WHEN 'reverted' THEN 0 WHEN 'reworked' THEN 1 WHEN 'merged' THEN 2
                       WHEN 'open' THEN 3 WHEN 'abandoned' THEN 4 WHEN 'superseded' THEN 5 ELSE 6 END,
            repo_key, sha, survival NULLS LAST
    ), cs AS (
        SELECT DISTINCT e.episode_id, left(a.sha, 7) AS k
        FROM _el_ep e JOIN _el_asg a USING (uid) WHERE a.sha IS NOT NULL
    ), git AS (
        SELECT cs.episode_id,
               count(*) FILTER (WHERE coalesce(co.state, '') <> 'superseded') AS n_commits,
               CASE WHEN bool_or(co.state IN ('reverted', 'reworked')) THEN 'reworked'
                    WHEN bool_or(co.state = 'merged' AND coalesce(co.survival, 1) >= 0.5) THEN 'shipped'
                    WHEN bool_or(co.state = 'open') THEN 'open'
                    -- coalesce: a commit with no outcome row yet keeps the episode undecided
                    WHEN bool_and(coalesce(co.state, '') IN ('abandoned', 'superseded'))
                         AND bool_or(co.state = 'abandoned') THEN 'ruled_out'
                    ELSE 'undecided' END AS outcome
        FROM cs LEFT JOIN co USING (k) GROUP BY 1
    ), man AS (
        SELECT DISTINCT ON (e.episode_id) e.episode_id, m.outcome
        FROM agents.manual_outcome m
        JOIN _el_unit u ON u.session_id = m.session_id
         AND (m.repo_key IS NULL OR u.repo_key = m.repo_key
              OR NOT EXISTS (SELECT 1 FROM _el_unit v
                              WHERE v.session_id = m.session_id AND v.repo_key = m.repo_key))
        JOIN _el_ep e USING (uid)
        ORDER BY e.episode_id, m.ts DESC, m.session_id, m.repo_key NULLS LAST, m.outcome
    ), conf AS (
        SELECT DISTINCT e.episode_id
        FROM _el_ep e JOIN _el_cu cu USING (uid) JOIN agents.episode_correction c ON c.id = cu.id
        WHERE c.action = 'confirm' AND cu.side = 'a'
    )
    SELECT b.episode_id, b.first_ts, b.last_ts, m.n_sessions, m.n_seed, m.n_steers,
           coalesce(g.n_commits, 0), coalesce(p.n_prs, 0),
           coalesce(man.outcome, g.outcome, 'no_code'),
           CASE WHEN man.outcome IS NOT NULL THEN 'manual' ELSE 'git' END,
           conf.episode_id IS NOT NULL
    FROM base b JOIN mem m USING (episode_id)
    LEFT JOIN prs p USING (episode_id) LEFT JOIN git g USING (episode_id)
    LEFT JOIN man USING (episode_id) LEFT JOIN conf USING (episode_id);
    GET DIAGNOSTICS episodes = ROW_COUNT;

    INSERT INTO agents.episode_member (episode_id, session_id, role, repo_keys)
    SELECT e.episode_id, a.session_id, a.role, array_agg(DISTINCT u.repo_key ORDER BY u.repo_key)
    FROM _el_ep e JOIN _el_asg a USING (uid) JOIN _el_unit u USING (uid)
    GROUP BY 1, 2, 3;
    GET DIAGNOSTICS members = ROW_COUNT;

    DROP TABLE _el_row, _el_sess, _el_unit, _el_pos, _el_asg, _el_span, _el_key, _el_edge,
               _el_lab, _el_cu, _el_ep;
    RETURN NEXT;
END
$$;

-- Episodes per completed month (sink time zone AUT_TZ, by the month an episode started), for
-- public summaries. A month shows only 14 days after it ends, once every episode
-- that started in it has been quiet for 14 days and none is still undecided, so the counts do
-- not move after they are first shown (later hand corrections still may, on purpose). A
-- commit that never gets a final outcome holds its month back. Counts only: no session or
-- episode id.
CREATE OR REPLACE VIEW agents.episode_monthly AS
SELECT month,
       count(*)::int                                             AS episodes,
       count(*) FILTER (WHERE outcome = 'shipped')::int          AS shipped,
       count(*) FILTER (WHERE outcome = 'reworked')::int         AS reworked,
       count(*) FILTER (WHERE outcome = 'open')::int             AS open,
       count(*) FILTER (WHERE outcome = 'ruled_out')::int        AS ruled_out,
       count(*) FILTER (WHERE outcome = 'no_code')::int          AS no_code,
       count(*) FILTER (WHERE outcome = 'undecided')::int        AS undecided,
       count(*) FILTER (WHERE first_day <> last_day)::int        AS multi_day,
       count(*) FILTER (WHERE n_seed_sessions >= 2)::int         AS revisits
FROM (SELECT date_trunc('month', first_ts AT TIME ZONE '@AUT_TZ@')::date AS month,
             (first_ts AT TIME ZONE '@AUT_TZ@')::date AS first_day,
             (last_ts AT TIME ZONE '@AUT_TZ@')::date AS last_day,
             last_ts, outcome, n_seed_sessions
      FROM agents.episode) e
WHERE (month + interval '1 month' + interval '14 days') AT TIME ZONE '@AUT_TZ@' <= now()
GROUP BY month
HAVING max(last_ts) + interval '14 days' <= now()
   AND count(*) FILTER (WHERE outcome = 'undecided') = 0;

-- Phase 4: labels from a local model on a collector machine. No new table for them: aut writes one
-- activity_event row per decision (kind 'label') and one per bake-off result (kind 'eval'),
-- attrs ids, enums and numbers only, never a title or prompt text. A new model, prompt or
-- task_ver adds rows and never overwrites, so these views pick the current one. Neither view
-- has a machine column: labels are per unit, whichever machine wrote them.

-- Current label per (task, unit, unit_id): newest task_ver, then the newest labelling run
-- (attrs labelled_at), event_id only to keep the pick stable. below_tau rows stay
-- in, so consumers treat them as unknown (excluded from shares in numerator and denominator).
CREATE OR REPLACE VIEW agents.label_latest AS
SELECT DISTINCT ON (attrs->>'task', attrs->>'unit', attrs->>'unit_id')
       attrs->>'unit'                     AS unit,
       attrs->>'unit_id'                  AS unit_id,
       attrs->>'task'                     AS task,
       attrs->>'label'                    AS label,
       (attrs->>'p')::double precision    AS p,
       coalesce((attrs->>'below_tau')::boolean, true) AS below_tau,
       attrs->>'model_id'                 AS model_id,
       (attrs->>'task_ver')::int          AS task_ver,
       ts
FROM agents.activity_event
WHERE kind = 'label'
  AND coalesce(attrs->>'task', '') NOT LIKE 'working.%'
ORDER BY attrs->>'task', attrs->>'unit', attrs->>'unit_id',
         (attrs->>'task_ver')::int DESC, attrs->>'labelled_at' DESC NULLS LAST, event_id DESC;

-- Newest eval per (task, model_id), one row per label of its per_label object. Numbers only.
-- aut's eval row: attrs {task, task_ver, model_id, model_rev, macro_f1, kappa, ece_raw,
-- ece_cal, per_label: {<label>: {n, precision, recall, f1, tau, coverage, share_error_pp}}}.
CREATE OR REPLACE VIEW agents.eval_latest AS
SELECT e.task, e.task_ver, e.model_id, e.model_rev, e.ts AS evaluated_at,
       l.key                                       AS label,
       (l.value->>'n')::int                        AS n,
       (l.value->>'precision')::double precision   AS "precision",
       (l.value->>'recall')::double precision      AS recall,
       (l.value->>'f1')::double precision          AS f1,
       (l.value->>'tau')::double precision         AS tau,
       (l.value->>'coverage')::double precision    AS coverage,
       (l.value->>'share_error_pp')::double precision AS share_error_pp,
       e.macro_f1, e.kappa, e.ece_raw, e.ece_cal
FROM (SELECT DISTINCT ON (attrs->>'task', attrs->>'model_id')
             attrs->>'task'                          AS task,
             (attrs->>'task_ver')::int               AS task_ver,
             attrs->>'model_id'                      AS model_id,
             attrs->>'model_rev'                     AS model_rev,
             (attrs->>'macro_f1')::double precision  AS macro_f1,
             (attrs->>'kappa')::double precision     AS kappa,
             (attrs->>'ece_raw')::double precision   AS ece_raw,
             (attrs->>'ece_cal')::double precision   AS ece_cal,
             attrs->'per_label'                      AS per_label,
             ts
      FROM agents.activity_event
      WHERE kind = 'eval'
      ORDER BY attrs->>'task', attrs->>'model_id', ts DESC, event_id DESC) e
CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e.per_label) = 'object'
                                   THEN e.per_label ELSE '{}'::jsonb END) l;

-- Private nightly notes: one short draft per day, written by aut on a collector machine from that
-- day's labels and episode facts. Text, so private like window titles: agents_ro has no access, and
-- the writer inserts and reads only the day (enough for ON CONFLICT (day) DO NOTHING), so a
-- leaked writer DSN cannot read the notes. Kept for good by default: no retention job.
CREATE TABLE IF NOT EXISTS agents.daily_note (
    day         date        PRIMARY KEY,
    text        text        NOT NULL,
    model_id    text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Commits per local day from a full git scan (all clones, the user's own identities only, counts
-- only: no repo, name or SHA). Insert-only, a day's latest scan wins (agents.git_commits_daily).
-- Partial scans carry recorded lower bounds; a missing day remains unknown, not a zero.
CREATE TABLE IF NOT EXISTS agents.git_commit_day (
    day         date        NOT NULL,
    commits     integer     NOT NULL CHECK (commits >= 0),
    scanned_at  timestamptz NOT NULL DEFAULT now(),
    complete    boolean     NOT NULL DEFAULT true,
    PRIMARY KEY (day, scanned_at)
);
-- Existing rows came only from successful complete scans.
ALTER TABLE agents.git_commit_day
    ADD COLUMN IF NOT EXISTS complete boolean NOT NULL DEFAULT true;
