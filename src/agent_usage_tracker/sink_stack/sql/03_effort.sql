-- Phase 5: private effort, outcome, cost and audit views over the agents database.
--
-- Applied after 01_schema.sql and 02_price.sql (the cost views read
-- agents.model_price), before 04_grants.sql. Idempotent: CREATE OR REPLACE, IF NOT EXISTS.
--
-- Everything here is private: read by agents_ro (Grafana) only. Hours are private
-- numbers. No view carries a title, prompt text, branch name or machine: rows are counts, shares,
-- ids, enums and numbers. Days are local days in the sink time zone (AUT_TZ), weeks ISO weeks (Monday), months
-- calendar months. Views that read Phase 4 labels return zero rows (or NULL shares) until aut
-- writes a label row.
--
-- Slow views (they re-merge active time or read every usage row) have a *_live twin and a
-- materialized copy refreshed CONCURRENTLY by refresh.sql, after the
-- episode rebuild. Grafana reads the materialized copies.
--
-- Activity rows that are counted are read once per event_id (DISTINCT ON (event_id), earliest
-- ts): a Claude Code re-log stores a steer or turn again under another ts, and the table key is
-- (event_id, ts). event_id hashes the kind, so filtering by kind before the dedup is safe.

-- Run origin per session (event_session holds one row per usage event). scripted = any subagent
-- or automated row (the linker's rule), interactive = interactive rows and none scripted.
CREATE OR REPLACE VIEW agents.session_origin AS
SELECT session_id,
       bool_or(run_origin IN ('subagent', 'automated'))  AS scripted,
       bool_or(run_origin = 'automated')                 AS automated,
       bool_or(run_origin = 'interactive')
           AND NOT bool_or(run_origin IN ('subagent', 'automated')) AS interactive
FROM agents.event_session
GROUP BY session_id;

-- Latest git state per commit, keyed by the first 7 hex digits (the linker's rule).
CREATE OR REPLACE VIEW agents.commit_state AS
SELECT DISTINCT ON (left(lower(sha), 7)) left(lower(sha), 7) AS k, state, survival
FROM agents.commit_outcome
ORDER BY left(lower(sha), 7), decided_at DESC;

-- The episodes effort counts: agents.episode minus (a) episodes with a reject correction on one
-- of their units (the linker drops those on its next run; this covers a reject made since) and
-- (b) episodes whose confident work_category labels (unit episode_day, not below_tau) are all
-- non-building: learning, collaboration or other (admin, a quick question, personal). An
-- episode with no confident label counts, so before labels exist every episode counts.
CREATE OR REPLACE VIEW agents.episode_counted AS
WITH lab AS (
    SELECT split_part(unit_id, ':', 1) AS episode_id,
           bool_or(label NOT IN ('learning', 'collaboration', 'other')) AS building
    FROM agents.label_latest
    WHERE task = 'work_category' AND unit = 'episode_day' AND NOT below_tau
    GROUP BY 1
), rej AS (
    SELECT DISTINCT m.episode_id
    FROM agents.episode_correction c
    JOIN agents.episode_member m ON m.session_id = c.a_session
    WHERE c.action = 'reject'
      AND (NOT c.attrs ? 'repo_keys'
           OR EXISTS (SELECT 1 FROM unnest(m.repo_keys) k WHERE c.attrs->'repo_keys' ? k))
)
SELECT e.episode_id, e.first_ts, e.last_ts, e.n_sessions, e.n_seed_sessions, e.n_steers,
       e.n_commits, e.n_prs, e.outcome
FROM agents.episode e
LEFT JOIN lab l ON l.episode_id = e.episode_id::text
WHERE coalesce(l.building, true)
  AND e.episode_id NOT IN (SELECT episode_id FROM rej);

-- Local days a counted episode had a member row on (the linker's row kinds, inside the
-- episode's span, so a session with units in two episodes adds each its own days).
CREATE OR REPLACE VIEW agents.episode_activity_day AS
SELECT DISTINCT c.episode_id, (a.ts AT TIME ZONE '@AUT_TZ@')::date AS day
FROM agents.episode_counted c
JOIN agents.episode_member m USING (episode_id)
JOIN agents.activity_event a ON a.session_id = m.session_id
WHERE a.kind IN ('steer', 'link', 'commit', 'pr')
  AND a.ts BETWEEN c.first_ts AND c.last_ts;

-- Interactive sessions steered in each 15-minute window (windows start on the quarter hour).
CREATE OR REPLACE VIEW agents.steer_window AS
SELECT date_bin('15 minutes', a.ts, '2000-01-01 00:00+00') AS window_start,
       count(DISTINCT a.session_id)::int AS sessions
FROM (SELECT DISTINCT ON (event_id) ts, session_id FROM agents.activity_event
      WHERE kind = 'steer' ORDER BY event_id, ts) a
JOIN agents.session_origin o USING (session_id)
WHERE o.interactive
GROUP BY 1;

-- A stretch of one interactive session being steered: steers at most 15 minutes apart, at least
-- two of them, at least a minute from first to last. A one-steer or seconds-long session is no
-- stretch, so a burst of them never counts as sessions at once.
CREATE OR REPLACE VIEW agents.steer_span AS
SELECT session_id, min(ts) AS start_ts, max(ts) AS end_ts
FROM (SELECT session_id, ts,
             count(*) FILTER (WHERE prev IS NULL OR ts - prev > interval '15 minutes')
                 OVER (PARTITION BY session_id ORDER BY ts) AS stretch
      FROM (SELECT a.session_id, a.ts, lag(a.ts) OVER (PARTITION BY a.session_id ORDER BY a.ts) AS prev
            FROM (SELECT DISTINCT ON (event_id) ts, session_id FROM agents.activity_event
                  WHERE kind = 'steer' ORDER BY event_id, ts) a
            JOIN agents.session_origin o USING (session_id)
            WHERE o.interactive) g) n
GROUP BY session_id, stretch
HAVING count(*) >= 2 AND max(ts) - min(ts) >= interval '1 minute';

-- Stretches running at one moment, per local day: a line sweep, so two sessions steered one after
-- the other inside one 15-minute window are not at once. A day with any interactive steer is at
-- least 1. shortcut: read at the stretches' starts and ends, so a peak still running across
-- midnight shows on the day it was reached; read per moment if a day's peak ever matters that much.
CREATE OR REPLACE VIEW agents.steer_peak_day AS
SELECT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, at_once
FROM (SELECT ts, sum(delta) OVER (ORDER BY ts, delta DESC ROWS UNBOUNDED PRECEDING)::int AS at_once
      FROM (SELECT start_ts AS ts, 1 AS delta FROM agents.steer_span
            UNION ALL
            SELECT end_ts, -1 FROM agents.steer_span) e) sweep
UNION ALL
SELECT (window_start AT TIME ZONE '@AUT_TZ@')::date, 1 FROM agents.steer_window;

-- Effort per day, ISO week and month (grain 'day' | 'week' | 'month', period = its first day).
--   steers               human steers outside scripted sessions; steers_by_agent splits them
--   interactive_sessions sessions with a row in the period whose run_origin is interactive
--   sessions_all         every session with a row in the period, scripted ones included
--   projects             distinct repo_key on link and commit rows
--   commits, prs         distinct commits (7-hex prefix, superseded pre-amend SHAs left out)
--                        and PRs (host, repo_key, number)
--   merged_commits       of those commits, the ones whose latest git state is merged
--   episodes_active      counted episodes with a member row in the period; episodes_started
--                        by their first row
--   focus_blocks         merged 'relevant' active intervals of 60 minutes or more, more than
--                        half covered by steer islands and editor/dev/terminal window time,
--                        counted in the period they start
--   parallel_peak        most interactive sessions being steered at one moment (steer_peak_day)
--   active_minutes_*     agents.active_daily summed (private: never published)
-- Reads agents.active_daily (materialized), so refresh that first.
CREATE OR REPLACE VIEW agents.effort_period_live AS
WITH g(grain) AS (VALUES ('day'), ('week'), ('month')),
act AS (
    SELECT (a.ts AT TIME ZONE '@AUT_TZ@')::date AS day, a.kind, a.agent, a.session_id,
           CASE WHEN a.kind IN ('link', 'commit') THEN nullif(a.attrs->>'repo_key', '') END AS repo_key,
           CASE WHEN a.kind = 'commit' AND lower(a.attrs->>'sha') ~ '^[0-9a-f]{7,40}$'
                THEN left(lower(a.attrs->>'sha'), 7) END AS sha7,
           CASE WHEN a.kind = 'pr'
                THEN concat_ws(':', a.attrs->>'host', a.attrs->>'repo_key', a.attrs->>'number') END AS pr,
           coalesce(o.scripted, false) AS scripted, coalesce(o.interactive, false) AS interactive
    FROM (SELECT DISTINCT ON (event_id) ts, kind, agent, session_id, attrs FROM agents.activity_event
          WHERE session_id IS NOT NULL ORDER BY event_id, ts) a
    LEFT JOIN agents.session_origin o USING (session_id)
), base AS (
    SELECT g.grain, date_trunc(g.grain, x.day::timestamp)::date AS period,
           count(*) FILTER (WHERE x.kind = 'steer' AND NOT x.scripted)::int AS steers,
           count(DISTINCT x.session_id) FILTER (WHERE x.interactive)::int AS interactive_sessions,
           count(DISTINCT x.session_id)::int AS sessions_all,
           count(DISTINCT x.repo_key)::int AS projects,
           count(DISTINCT x.sha7) FILTER (WHERE cs.state IS DISTINCT FROM 'superseded')::int AS commits,
           count(DISTINCT x.pr)::int AS prs,
           count(DISTINCT x.sha7) FILTER (WHERE cs.state = 'merged')::int AS merged_commits
    FROM act x CROSS JOIN g
    LEFT JOIN agents.commit_state cs ON cs.k = x.sha7
    GROUP BY 1, 2
), by_agent AS (
    SELECT grain, period, jsonb_object_agg(agent, n) AS steers_by_agent
    FROM (SELECT g.grain, date_trunc(g.grain, x.day::timestamp)::date AS period, x.agent, count(*) AS n
          FROM act x CROSS JOIN g
          WHERE x.kind = 'steer' AND NOT x.scripted
          GROUP BY 1, 2, 3) s
    GROUP BY 1, 2
), ep AS (
    SELECT g.grain, date_trunc(g.grain, d.day::timestamp)::date AS period,
           count(DISTINCT d.episode_id)::int AS episodes_active
    FROM agents.episode_activity_day d CROSS JOIN g
    GROUP BY 1, 2
), started AS (
    SELECT g.grain, date_trunc(g.grain, (c.first_ts AT TIME ZONE '@AUT_TZ@')::date::timestamp)::date AS period,
           count(*)::int AS episodes_started
    FROM agents.episode_counted c CROSS JOIN g
    GROUP BY 1, 2
), fsrc AS (
    SELECT start_ts, end_ts FROM agents.steer_interval
    UNION ALL
    SELECT ts, end_ts FROM agents.time_interval_effective WHERE category IN ('editor', 'dev', 'terminal')
), fprev AS (
    SELECT start_ts, end_ts,
           max(end_ts) OVER (ORDER BY start_ts, end_ts ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM fsrc
), focus AS MATERIALIZED (
    -- focus time merged, so the overlaps summed below never count one minute twice
    SELECT min(start_ts) AS s, max(end_ts) AS e
    FROM (SELECT start_ts, end_ts,
                 count(*) FILTER (WHERE prev_end IS NULL OR start_ts > prev_end)
                     OVER (ORDER BY start_ts, end_ts ROWS UNBOUNDED PRECEDING) AS grp
          FROM fprev) f
    GROUP BY grp
), blocks AS (
    SELECT (r.start_ts AT TIME ZONE '@AUT_TZ@')::date AS day
    FROM agents.active_interval r
    WHERE r.variant = 'relevant' AND r.end_ts - r.start_ts >= interval '60 minutes'
      AND 2 * (SELECT coalesce(sum(extract(epoch FROM least(f.e, r.end_ts) - greatest(f.s, r.start_ts))), 0)
               FROM focus f WHERE f.s < r.end_ts AND f.e > r.start_ts)
          > extract(epoch FROM r.end_ts - r.start_ts)
), fb AS (
    SELECT g.grain, date_trunc(g.grain, b.day::timestamp)::date AS period, count(*)::int AS focus_blocks
    FROM blocks b CROSS JOIN g
    GROUP BY 1, 2
), par AS (
    SELECT g.grain, date_trunc(g.grain, d.day::timestamp)::date AS period, max(d.at_once)::int AS parallel_peak
    FROM agents.steer_peak_day d CROSS JOIN g
    GROUP BY 1, 2
), mins AS (
    SELECT g.grain, date_trunc(g.grain, d.day::timestamp)::date AS period,
           sum(d.minutes_all) AS active_minutes_all, sum(d.minutes_relevant) AS active_minutes_relevant
    FROM agents.active_daily d CROSS JOIN g
    GROUP BY 1, 2
), spine AS (
    SELECT grain, period FROM base UNION SELECT grain, period FROM ep
    UNION SELECT grain, period FROM started UNION SELECT grain, period FROM fb
    UNION SELECT grain, period FROM mins
)
SELECT s.grain, s.period,
       coalesce(b.steers, 0) AS steers, coalesce(a.steers_by_agent, '{}'::jsonb) AS steers_by_agent,
       coalesce(b.interactive_sessions, 0) AS interactive_sessions,
       coalesce(b.sessions_all, 0) AS sessions_all, coalesce(b.projects, 0) AS projects,
       coalesce(b.commits, 0) AS commits, coalesce(b.prs, 0) AS prs,
       coalesce(b.merged_commits, 0) AS merged_commits,
       coalesce(e.episodes_active, 0) AS episodes_active,
       coalesce(st.episodes_started, 0) AS episodes_started,
       coalesce(f.focus_blocks, 0) AS focus_blocks, coalesce(p.parallel_peak, 0) AS parallel_peak,
       coalesce(m.active_minutes_all, 0) AS active_minutes_all,
       coalesce(m.active_minutes_relevant, 0) AS active_minutes_relevant
FROM spine s
LEFT JOIN base b USING (grain, period)
LEFT JOIN by_agent a USING (grain, period)
LEFT JOIN ep e USING (grain, period)
LEFT JOIN started st USING (grain, period)
LEFT JOIN fb f USING (grain, period)
LEFT JOIN par p USING (grain, period)
LEFT JOIN mins m USING (grain, period);

CREATE MATERIALIZED VIEW IF NOT EXISTS agents.effort_period AS
SELECT * FROM agents.effort_period_live
WITH DATA;
CREATE UNIQUE INDEX IF NOT EXISTS effort_period_key ON agents.effort_period (grain, period);

CREATE OR REPLACE VIEW agents.effort_daily AS
SELECT period AS day, steers, steers_by_agent, interactive_sessions, sessions_all, projects,
       commits, prs, merged_commits, episodes_active, episodes_started, focus_blocks,
       parallel_peak, active_minutes_all, active_minutes_relevant
FROM agents.effort_period WHERE grain = 'day';

CREATE OR REPLACE VIEW agents.effort_weekly AS
SELECT period AS week, steers, steers_by_agent, interactive_sessions, sessions_all, projects,
       commits, prs, merged_commits, episodes_active, episodes_started, focus_blocks,
       parallel_peak, active_minutes_all, active_minutes_relevant
FROM agents.effort_period WHERE grain = 'week';

CREATE OR REPLACE VIEW agents.effort_monthly AS
SELECT period AS month, steers, steers_by_agent, interactive_sessions, sessions_all, projects,
       commits, prs, merged_commits, episodes_active, episodes_started, focus_blocks,
       parallel_peak, active_minutes_all, active_minutes_relevant
FROM agents.effort_period WHERE grain = 'month';

-- ---------------------------------------------------------------------------------------------
-- Modules: counts and shares only.
-- ---------------------------------------------------------------------------------------------

-- Label shares per task and local month, by unit count. share is among confident
-- units; below_tau units are unknown: one row with label NULL, and unknown_share on every row of
-- the month. A month with only unknown units shows just that row.
-- shortcut: every unit weighs the same; label rows carry no duration, so work_category shares
-- are of labelled units, not of active minutes. Weight by minutes once aut writes them.
CREATE OR REPLACE VIEW agents.label_share_monthly AS
SELECT task, month, label, units,
       CASE WHEN label IS NOT NULL
            THEN round(units::numeric / nullif(sum(units) FILTER (WHERE label IS NOT NULL) OVER w, 0), 4)
       END AS share,
       round(coalesce(sum(units) FILTER (WHERE label IS NULL) OVER w, 0)::numeric / sum(units) OVER w, 4)
           AS unknown_share
FROM (SELECT task, date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month,
             CASE WHEN NOT below_tau THEN label END AS label, count(*)::int AS units
      FROM agents.label_latest
      GROUP BY 1, 2, 3) x
WINDOW w AS (PARTITION BY task, month);

-- unit_share, not share: a unit is an episode day or a window block, each counted once (the
-- shortcut above). One-time migration: CREATE OR REPLACE cannot rename a column; nothing
-- depends on the view and 04_grants.sql (run after this file) re-grants it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'agents' AND table_name = 'work_mix_monthly'
                 AND column_name = 'share') THEN
        EXECUTE 'DROP VIEW agents.work_mix_monthly';
    END IF;
END $$;
CREATE OR REPLACE VIEW agents.work_mix_monthly AS
SELECT month, label AS category, units, share AS unit_share, unknown_share
FROM agents.label_share_monthly WHERE task = 'work_category';

CREATE OR REPLACE VIEW agents.steer_intent_monthly AS
SELECT month, label AS intent, units, share, unknown_share
FROM agents.label_share_monthly WHERE task = 'steer_intent';

-- Outcomes of counted episodes by the month they started. final = the month is complete the way
-- agents.episode_monthly publishes it (14 days after the month, every episode quiet for 14 days,
-- none undecided); before that the counts can still move. The two sets differ on purpose:
-- episode_monthly (phase 3) counts every linked episode, this view only the counted ones (no
-- reject, building work). Phase 6 publishes the counted set, so it reads this view's final
-- months, not episode_monthly. The self-check pins the difference.
CREATE OR REPLACE VIEW agents.outcomes_monthly AS
SELECT month,
       count(*)::int                                       AS episodes,
       count(*) FILTER (WHERE outcome = 'shipped')::int    AS shipped,
       count(*) FILTER (WHERE outcome = 'reworked')::int   AS reworked,
       count(*) FILTER (WHERE outcome = 'open')::int       AS open,
       count(*) FILTER (WHERE outcome = 'ruled_out')::int  AS ruled_out,
       count(*) FILTER (WHERE outcome = 'no_code')::int    AS no_code,
       count(*) FILTER (WHERE outcome = 'undecided')::int  AS undecided,
       round(count(*) FILTER (WHERE outcome = 'shipped')::numeric / count(*), 4) AS shipped_share,
       round(count(*) FILTER (WHERE outcome = 'reworked')::numeric
             / nullif(count(*) FILTER (WHERE outcome IN ('shipped', 'reworked')), 0), 4) AS rework_share,
       (month + interval '1 month' + interval '14 days') AT TIME ZONE '@AUT_TZ@' <= now()
           AND max(last_ts) + interval '14 days' <= now()
           AND count(*) FILTER (WHERE outcome = 'undecided') = 0 AS final
FROM (SELECT date_trunc('month', first_ts AT TIME ZONE '@AUT_TZ@')::date AS month,
             last_ts, outcome
      FROM agents.episode_counted) e
GROUP BY month;

-- Counted episodes active per ISO week, and returns: an active day after two or more whole days
-- with no activity on that episode.
CREATE OR REPLACE VIEW agents.episodes_revisits_weekly AS
SELECT date_trunc('week', day::timestamp)::date               AS week,
       count(DISTINCT episode_id)::int                         AS episodes_active,
       count(*) FILTER (WHERE gap IS NULL)::int                AS episodes_started,
       count(*) FILTER (WHERE gap > 2)::int                    AS returns,
       count(DISTINCT episode_id) FILTER (WHERE gap > 2)::int  AS episodes_returning
FROM (SELECT episode_id, day, day - lag(day) OVER (PARTITION BY episode_id ORDER BY day) AS gap
      FROM agents.episode_activity_day) d
GROUP BY 1;

-- Parallel steering per ISO week. parallel_peak: most sessions being steered at one moment
-- (steer_peak_day). The window columns: 15-minute windows with an interactive steer, of which those
-- with two or more sessions; they count sessions that merely share a window, so they stay private.
CREATE OR REPLACE VIEW agents.parallel_steering_weekly AS
SELECT date_trunc('week', w.window_start AT TIME ZONE '@AUT_TZ@')::date AS week,
       max(p.peak)::int                                      AS parallel_peak,
       count(*)::int                                         AS steered_windows,
       count(*) FILTER (WHERE w.sessions >= 2)::int          AS parallel_windows,
       round(count(*) FILTER (WHERE w.sessions >= 2)::numeric / count(*), 4) AS parallel_share
FROM agents.steer_window w
JOIN (SELECT date_trunc('week', day::timestamp)::date AS week, max(at_once) AS peak
      FROM agents.steer_peak_day GROUP BY 1) p
  ON p.week = date_trunc('week', w.window_start AT TIME ZONE '@AUT_TZ@')::date
GROUP BY 1;

-- Commits by the month their first commit row landed, by latest git state. aut decides reverted
-- (a revert within 14 days of the merge) and reworked (under half the lines survive 14 days).
-- Superseded pre-amend SHAs are left out; a commit without an outcome row is undecided.
CREATE OR REPLACE VIEW agents.shipping_rework_monthly AS
SELECT c.month,
       count(*)::int                                                AS commits,
       count(*) FILTER (WHERE s.state = 'merged')::int              AS merged,
       count(*) FILTER (WHERE s.state = 'reverted')::int            AS reverted,
       count(*) FILTER (WHERE s.state = 'reworked')::int            AS reworked,
       count(*) FILTER (WHERE s.state = 'open')::int                AS open,
       count(*) FILTER (WHERE s.state = 'abandoned')::int           AS abandoned,
       count(*) FILTER (WHERE coalesce(s.state, 'undecided') = 'undecided')::int AS undecided,
       round(count(*) FILTER (WHERE s.state IN ('reverted', 'reworked'))::numeric
             / nullif(count(*) FILTER (WHERE s.state IN ('merged', 'reverted', 'reworked')), 0), 4)
           AS rework_share
FROM (SELECT DISTINCT ON (left(lower(attrs->>'sha'), 7)) left(lower(attrs->>'sha'), 7) AS k,
             date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month
      FROM agents.activity_event
      WHERE kind = 'commit' AND lower(attrs->>'sha') ~ '^[0-9a-f]{7,40}$'
      ORDER BY 1, ts) c
LEFT JOIN agents.commit_state s USING (k)
WHERE s.state IS DISTINCT FROM 'superseded'
GROUP BY c.month;

-- Skills built and reused: invoke rows by month. skills_new = skills invoked for the first time
-- ever; later_invocations = every invoke after a skill's first; skills_reused = distinct skills
-- invoked this month that first appeared in an earlier month. 'mcp' (every MCP tool collapsed)
-- is no skill and is left out.
CREATE OR REPLACE VIEW agents.built_reused_monthly AS
SELECT month,
       count(*) FILTER (WHERE nth = 1)::int                          AS skills_new,
       count(*)::int                                                 AS invocations,
       count(*) FILTER (WHERE nth > 1)::int                          AS later_invocations,
       count(DISTINCT skill) FILTER (WHERE first_month < month)::int AS skills_reused
FROM (SELECT attrs->>'skill' AS skill,
             date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month,
             row_number() OVER (PARTITION BY attrs->>'skill' ORDER BY ts, event_id) AS nth,
             min(date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date)
                 OVER (PARTITION BY attrs->>'skill') AS first_month
      FROM (SELECT DISTINCT ON (event_id) event_id, ts, attrs FROM agents.activity_event
            WHERE kind = 'invoke' ORDER BY event_id, ts) a
      WHERE coalesce(attrs->>'skill', '') NOT IN ('', 'mcp')) i
GROUP BY month;

-- Learning followed by doing: confident 'learning' episode-day labels by month, and how many were
-- followed within 30 days by a confident building-work episode-day on one of the same repos
-- (building work = episode_counted's rule: any category but learning, collaboration, other).
-- shortcut: aw_block labels carry no repo, so learning time outside an episode is not counted.
CREATE OR REPLACE VIEW agents.learning_to_doing_monthly AS
WITH ed AS (
    SELECT DISTINCT l.unit_id, l.label, l.ts, k AS repo_key,
           l.label NOT IN ('learning', 'collaboration', 'other') AS building
    FROM agents.label_latest l
    JOIN agents.episode_member m ON m.episode_id::text = split_part(l.unit_id, ':', 1)
    CROSS JOIN LATERAL unnest(m.repo_keys) k
    WHERE l.task = 'work_category' AND l.unit = 'episode_day' AND NOT l.below_tau
      AND k <> ''
), learn AS (
    SELECT le.unit_id, min(le.ts) AS ts,
           bool_or(EXISTS (SELECT 1 FROM ed b
                           WHERE b.building AND b.repo_key = le.repo_key
                             AND b.ts > le.ts AND b.ts <= le.ts + interval '30 days')) AS followed
    FROM ed le WHERE le.label = 'learning'
    GROUP BY le.unit_id
)
SELECT date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month,
       count(*)::int                         AS learning_days,
       count(*) FILTER (WHERE followed)::int AS followed_by_building
FROM learn
GROUP BY 1;

-- Follow-through: commits (outside scripted sessions, superseded SHAs left out), and those on a
-- repo where a counted episode shipped and the repo then went quiet for more than 30 days. Each
-- commit is read against the latest shipped episode before it: quiet means the repo's first
-- activity after that episode (link, commit or PR outside scripted sessions) came over 30 days
-- later. A repo that keeps moving after it ships never counts; a later shipped episode starts over.
CREATE OR REPLACE VIEW agents.follow_through_commit AS
WITH act AS MATERIALIZED (
    SELECT a.attrs->>'repo_key' AS repo_key, a.ts
    FROM agents.activity_event a
    LEFT JOIN agents.session_origin o USING (session_id)
    WHERE a.kind IN ('link', 'commit', 'pr') AND coalesce(a.attrs->>'repo_key', '') <> ''
      AND NOT coalesce(o.scripted, false)
), shipped AS (
    SELECT s.repo_key, s.last_ts,
           coalesce((SELECT min(x.ts) FROM act x WHERE x.repo_key = s.repo_key AND x.ts > s.last_ts)
                    > s.last_ts + interval '30 days', false) AS quiet
    FROM (SELECT DISTINCT k AS repo_key, e.last_ts
          FROM agents.episode_counted e
          JOIN agents.episode_member m USING (episode_id)
          CROSS JOIN LATERAL unnest(m.repo_keys) k
          WHERE e.outcome = 'shipped' AND k <> '') s
), c AS (
    SELECT DISTINCT ON (left(lower(a.attrs->>'sha'), 7)) a.ts, nullif(a.attrs->>'repo_key', '') AS repo_key
    FROM agents.activity_event a
    LEFT JOIN agents.session_origin o USING (session_id)
    LEFT JOIN agents.commit_state cs ON cs.k = left(lower(a.attrs->>'sha'), 7)
    WHERE a.kind = 'commit' AND lower(a.attrs->>'sha') ~ '^[0-9a-f]{7,40}$'
      AND NOT coalesce(o.scripted, false) AND cs.state IS DISTINCT FROM 'superseded'
    ORDER BY left(lower(a.attrs->>'sha'), 7), a.ts
)
SELECT c.ts, c.repo_key, coalesce(l.quiet, false) AS follow_through
FROM c
LEFT JOIN LATERAL (SELECT s.quiet FROM shipped s
                   WHERE s.repo_key = c.repo_key AND s.last_ts < c.ts
                   ORDER BY s.last_ts DESC LIMIT 1) l ON true;

CREATE OR REPLACE VIEW agents.follow_through_monthly AS
SELECT date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month,
       count(*)::int                                                AS commits,
       count(*) FILTER (WHERE follow_through)::int                  AS follow_through_commits,
       count(DISTINCT repo_key) FILTER (WHERE follow_through)::int  AS follow_through_repos
FROM agents.follow_through_commit
GROUP BY 1;

-- ---------------------------------------------------------------------------------------------
-- Tokens, cost and efficiency (private). Cost is the 02_price.sql rule: API-equivalent,
-- NOTIONAL, reasoning tokens never added (they are part of output). An unpriced model costs
-- NULL, never a guess; unpriced_turns says how much of a figure is missing.
-- ---------------------------------------------------------------------------------------------

CREATE OR REPLACE VIEW agents.usage_cost AS
SELECT u.event_id, u.ts, u.agent, coalesce(u.model, '') AS model,
       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
       u.reasoning_tokens, u.total_tokens,
       u.input_tokens * p.input_cost + u.output_tokens * p.output_cost
       + u.cache_read_tokens * coalesce(p.cache_read_cost, p.input_cost)
       + u.cache_write_tokens * coalesce(p.cache_write_cost, p.input_cost) AS est_cost_usd
FROM agents.usage_event_dedup u
LEFT JOIN agents.model_price p ON p.model_key = lower(u.model)
WHERE u.kind = 'agent';

CREATE OR REPLACE VIEW agents.tokens_cost_by_model_daily_live AS
SELECT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, agent, model,
       count(*)::int                    AS turns,
       sum(input_tokens)::bigint        AS input_tokens,
       sum(output_tokens)::bigint       AS output_tokens,
       sum(cache_read_tokens)::bigint   AS cache_read_tokens,
       sum(cache_write_tokens)::bigint  AS cache_write_tokens,
       sum(reasoning_tokens)::bigint    AS reasoning_tokens,
       sum(total_tokens)::bigint        AS total_tokens,
       sum(est_cost_usd)                AS est_cost_usd,
       count(*) FILTER (WHERE est_cost_usd IS NULL)::int AS unpriced_turns
FROM agents.usage_cost
GROUP BY 1, 2, 3;

CREATE MATERIALIZED VIEW IF NOT EXISTS agents.tokens_cost_by_model_daily AS
SELECT * FROM agents.tokens_cost_by_model_daily_live
WITH DATA;
CREATE UNIQUE INDEX IF NOT EXISTS tokens_cost_by_model_daily_key
    ON agents.tokens_cost_by_model_daily (day, agent, model);

-- Tokens and cost per counted episode: the usage rows of its member sessions inside its span,
-- each row given to exactly one episode. A session that worked in two repos has a unit in each,
-- and their spans can overlap; such a row goes to the episode whose repo rows in that session
-- (link, commit, pr) lie nearest in time, the linker's _el_pos rule. The pick runs over every
-- episode, so a row of a rejected or non-building episode is never moved onto a counted one.
-- Every counted episode has a row: turns 0 and NULL tokens when no usage row fell in its span.
CREATE OR REPLACE VIEW agents.tokens_cost_by_episode_live AS
WITH cand AS (
    SELECT e.episode_id, m.session_id, m.repo_keys, u.event_id, u.ts, u.total_tokens, u.est_cost_usd,
           count(*) OVER (PARTITION BY u.event_id) AS n
    FROM agents.episode e
    JOIN agents.episode_member m USING (episode_id)
    JOIN agents.event_session s ON s.session_id = m.session_id
    JOIN agents.usage_cost u ON u.event_id = s.event_id AND u.ts BETWEEN e.first_ts AND e.last_ts
), pos AS MATERIALIZED (
    SELECT session_id, ts, attrs->>'repo_key' AS repo_key
    FROM agents.activity_event
    WHERE kind IN ('link', 'commit', 'pr') AND coalesce(attrs->>'repo_key', '') <> ''
      AND session_id IN (SELECT session_id FROM cand WHERE n > 1)
), one AS (
    SELECT DISTINCT ON (c.event_id) c.episode_id, c.event_id, c.total_tokens, c.est_cost_usd
    FROM cand c
    ORDER BY c.event_id,
             CASE WHEN c.n > 1 THEN (SELECT min(abs(extract(epoch FROM p.ts - c.ts))) FROM pos p
                                     WHERE p.session_id = c.session_id AND p.repo_key = ANY (c.repo_keys))
             END NULLS LAST,
             c.episode_id
)
SELECT e.episode_id, date_trunc('month', e.first_ts AT TIME ZONE '@AUT_TZ@')::date AS month,
       e.outcome,
       count(o.event_id)::int        AS turns,
       sum(o.total_tokens)::bigint   AS total_tokens,
       sum(o.est_cost_usd)           AS est_cost_usd,
       count(o.event_id) FILTER (WHERE o.est_cost_usd IS NULL)::int AS unpriced_turns
FROM agents.episode_counted e
LEFT JOIN one o USING (episode_id)
GROUP BY 1, 2, 3;

CREATE MATERIALIZED VIEW IF NOT EXISTS agents.tokens_cost_by_episode AS
SELECT * FROM agents.tokens_cost_by_episode_live
WITH DATA;
CREATE UNIQUE INDEX IF NOT EXISTS tokens_cost_by_episode_key ON agents.tokens_cost_by_episode (episode_id);

-- Which sources reported per month: local days with data, by source. agents = usage rows
-- (tokens_cost_by_model_daily, refresh it first), activitywatch = window time, outcomes = git
-- outcome decisions, labels = label rows by their unit's day, manual = hand-entered active time. A
-- month a source never reported has no row for it.
CREATE OR REPLACE VIEW agents.coverage_monthly AS
SELECT date_trunc('month', day::timestamp)::date AS month, source, count(DISTINCT day)::int AS days
FROM (SELECT 'agents' AS source, day FROM agents.tokens_cost_by_model_daily
      UNION ALL
      SELECT 'activitywatch', day FROM agents.active_daily WHERE minutes_aw > 0
      UNION ALL
      SELECT 'manual', day FROM agents.active_daily WHERE minutes_manual > 0
      UNION ALL
      SELECT 'outcomes', (decided_at AT TIME ZONE '@AUT_TZ@')::date FROM agents.commit_outcome
      UNION ALL
      SELECT 'labels', (ts AT TIME ZONE '@AUT_TZ@')::date FROM agents.activity_event
      WHERE kind = 'label') s
GROUP BY 1, 2;

-- Efficiency per month (episodes by their start month): median tokens and cost of a shipped
-- episode, and all counted episodes' tokens and cost per shipped episode. Every counted episode
-- is in tokens_cost_by_episode, so shipped and episodes match outcomes_monthly.
CREATE OR REPLACE VIEW agents.efficiency_by_episode AS
SELECT month,
       count(*) FILTER (WHERE outcome = 'shipped')::int AS shipped,
       count(*)::int                                    AS episodes,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY total_tokens)
           FILTER (WHERE outcome = 'shipped')           AS median_tokens_shipped,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY est_cost_usd)
           FILTER (WHERE outcome = 'shipped')           AS median_cost_usd_shipped,
       round(sum(total_tokens) / nullif(count(*) FILTER (WHERE outcome = 'shipped'), 0))
                                                        AS tokens_per_shipped,
       round(sum(est_cost_usd) / nullif(count(*) FILTER (WHERE outcome = 'shipped'), 0), 2)
                                                        AS cost_usd_per_shipped
FROM agents.tokens_cost_by_episode
GROUP BY month;

-- ---------------------------------------------------------------------------------------------
-- Counting audit, per local day.
--   raw_usage_rows / dedup_usage_rows  usage_event rows vs distinct event_id (by first ts)
--   dup_groups                         event_ids stored more than once
--   dup_unexplained                    of those, groups the documented re-log rule does not
--                                      explain: Claude Code re-logs one message with another ts
--                                      on one machine; any other agent, or two machines or agents
--                                      under one event_id, is unexplained (a monitoring check
--                                      should alert on it)
--   activity_ts_conflicts              activity event_ids stored under two or more ts (the views
--                                      read them once; a monitoring check should alert on them)
--   steer_human                        per agent {steers, human}: steers vs human usage rows
--                                      (both one per event_id)
--   scripted_share                     automated sessions among sessions with a row that day
--   stale_collectors                   collectors (scanned in the 30 days before) with no ok
--                                      scan in the 80 hours before the day's end
--   links_without_session              link rows with no event_session row for their session
--   overlap_share                      share of per-source active minutes (steer + aw + manual)
--                                      that overlap another source, from agents.active_daily
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW agents.audit_daily_live AS
WITH u AS (
    SELECT event_id, (min(ts) AT TIME ZONE '@AUT_TZ@')::date AS day, count(*) AS n,
           count(DISTINCT machine) AS machines, count(DISTINCT agent) AS agents, min(agent) AS agent,
           bool_or(kind = 'human') AS human
    FROM agents.usage_event
    GROUP BY event_id
), ud AS (
    SELECT day, sum(n)::bigint AS raw_usage_rows, count(*)::bigint AS dedup_usage_rows,
           count(*) FILTER (WHERE n > 1)::int AS dup_groups,
           count(*) FILTER (WHERE n > 1 AND NOT (agent = 'claude_code' AND agents = 1 AND machines = 1))::int
               AS dup_unexplained
    FROM u GROUP BY day
), sh AS (
    SELECT day, jsonb_object_agg(agent, jsonb_build_object('steers', steers, 'human', human)) AS steer_human
    FROM (SELECT coalesce(s.day, h.day) AS day, coalesce(s.agent, h.agent) AS agent,
                 coalesce(s.n, 0) AS steers, coalesce(h.n, 0) AS human
          FROM (SELECT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, agent, count(*) AS n
                FROM (SELECT DISTINCT ON (event_id) ts, agent FROM agents.activity_event
                      WHERE kind = 'steer' ORDER BY event_id, ts) d
                GROUP BY 1, 2) s
          FULL JOIN (SELECT day, agent, count(*) AS n FROM u WHERE human GROUP BY 1, 2) h
            ON h.day = s.day AND h.agent = s.agent) x
    GROUP BY day
), ac AS (
    SELECT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, count(*)::int AS activity_ts_conflicts
    FROM (SELECT min(ts) AS ts FROM agents.activity_event GROUP BY event_id HAVING count(*) > 1) x
    GROUP BY 1
), ss AS (
    SELECT x.day,
           round(count(*) FILTER (WHERE o.automated)::numeric / count(*), 4) AS scripted_share
    FROM (SELECT DISTINCT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, session_id
          FROM agents.activity_event WHERE session_id IS NOT NULL) x
    LEFT JOIN agents.session_origin o USING (session_id)
    GROUP BY 1
), lk AS (
    SELECT (a.ts AT TIME ZONE '@AUT_TZ@')::date AS day,
           count(*) FILTER (WHERE o.session_id IS NULL)::int AS links_without_session
    FROM agents.activity_event a
    LEFT JOIN agents.session_origin o USING (session_id)
    WHERE a.kind = 'link'
    GROUP BY 1
), spine AS (
    SELECT day FROM ud UNION SELECT day FROM sh UNION SELECT day FROM ac UNION SELECT day FROM ss
    UNION SELECT day FROM lk UNION SELECT day FROM agents.active_daily
    UNION SELECT (finished_at AT TIME ZONE '@AUT_TZ@')::date FROM agents.collector_scan
), cs AS (
    -- the latest scan and the latest ok scan of each collector before the day's end (index
    -- lookups on the primary key, newest first)
    SELECT d.day, count(*) FILTER (WHERE lo.finished_at IS NULL
                                      OR lo.finished_at < d.day_end - interval '80 hours')::int AS stale_collectors
    FROM (SELECT day, least(((day + 1)::timestamp) AT TIME ZONE '@AUT_TZ@', now()) AS day_end
          FROM spine) d
    CROSS JOIN (SELECT DISTINCT machine, collector FROM agents.collector_scan) p
    CROSS JOIN LATERAL (SELECT s.finished_at FROM agents.collector_scan s
                        WHERE s.machine = p.machine AND s.collector = p.collector
                          AND s.finished_at < d.day_end
                        ORDER BY s.finished_at DESC LIMIT 1) la
    LEFT JOIN LATERAL (SELECT s.finished_at FROM agents.collector_scan s
                       WHERE s.machine = p.machine AND s.collector = p.collector
                         AND s.finished_at < d.day_end AND s.status = 'ok'
                       ORDER BY s.finished_at DESC LIMIT 1) lo ON true
    WHERE la.finished_at > d.day_end - interval '30 days'
    GROUP BY d.day
)
SELECT s.day,
       coalesce(ud.raw_usage_rows, 0) AS raw_usage_rows,
       coalesce(ud.dedup_usage_rows, 0) AS dedup_usage_rows,
       coalesce(ud.dup_groups, 0) AS dup_groups,
       coalesce(ud.dup_unexplained, 0) AS dup_unexplained,
       coalesce(ac.activity_ts_conflicts, 0) AS activity_ts_conflicts,
       coalesce(sh.steer_human, '{}'::jsonb) AS steer_human,
       ss.scripted_share,
       coalesce(cs.stale_collectors, 0) AS stale_collectors,
       coalesce(lk.links_without_session, 0) AS links_without_session,
       round(1 - ad.minutes_all
                 / nullif(ad.minutes_steer + ad.minutes_aw + ad.minutes_manual, 0), 4) AS overlap_share
FROM spine s
LEFT JOIN ud USING (day) LEFT JOIN sh USING (day) LEFT JOIN ac USING (day)
LEFT JOIN ss USING (day) LEFT JOIN lk USING (day) LEFT JOIN cs USING (day)
LEFT JOIN agents.active_daily ad USING (day);

CREATE MATERIALIZED VIEW IF NOT EXISTS agents.audit_daily AS
SELECT * FROM agents.audit_daily_live
WITH DATA;
CREATE UNIQUE INDEX IF NOT EXISTS audit_daily_day_idx ON agents.audit_daily (day);

-- ---------------------------------------------------------------------------------------------
-- Interventions: an interrupt or a rejection followed by a steer in the same session. The turn
-- before is the case's own turn (its turn_id), the turn after is the next steer's; for each, the
-- turn rows' tool_errors, retry and runaway flags, and whether a commit row shares the turn.
-- Ids only. NULL numbers mean no turn row carried that turn_id.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW agents.intervention_cases AS
WITH ev AS (
    SELECT event_id, ts, machine, agent, session_id, turn_id, kind,
           count(*) FILTER (WHERE kind = 'steer') OVER (
               PARTITION BY machine, agent, session_id ORDER BY ts, kind = 'steer', event_id
               ROWS UNBOUNDED PRECEDING) AS n_steer
    FROM (SELECT DISTINCT ON (event_id) event_id, ts, machine, agent, session_id, turn_id, kind
          FROM agents.activity_event
          WHERE kind IN ('interrupt', 'rejection', 'steer') AND session_id IS NOT NULL
          ORDER BY event_id, ts) a
), turns AS (
    SELECT machine, agent, session_id, turn_id,
           sum(CASE WHEN attrs->>'tool_errors' ~ '^[0-9]+$' THEN (attrs->>'tool_errors')::int END)
               FILTER (WHERE kind = 'turn')                        AS tool_errors,
           bool_or(attrs->'retry_flag' = 'true') FILTER (WHERE kind = 'turn')   AS retry,
           bool_or(attrs->'runaway_flag' = 'true') FILTER (WHERE kind = 'turn') AS runaway,
           bool_or(kind = 'commit')                                AS committed
    FROM (SELECT DISTINCT ON (event_id) machine, agent, session_id, turn_id, kind, attrs
          FROM agents.activity_event
          WHERE kind IN ('turn', 'commit') AND session_id IS NOT NULL AND turn_id IS NOT NULL
          ORDER BY event_id, ts) a
    GROUP BY 1, 2, 3, 4
)
SELECT c.event_id, c.kind, c.ts, c.agent, c.session_id,
       s.event_id AS steer_event_id, c.turn_id AS before_turn_id, s.turn_id AS after_turn_id,
       b.tool_errors AS before_tool_errors, b.retry AS before_retry, b.runaway AS before_runaway,
       coalesce(b.committed, false) AS before_commit,
       a.tool_errors AS after_tool_errors, a.retry AS after_retry, a.runaway AS after_runaway,
       coalesce(a.committed, false) AS after_commit
FROM ev c
JOIN ev s ON s.kind = 'steer' AND s.machine = c.machine AND s.agent = c.agent
         AND s.session_id = c.session_id AND s.n_steer = c.n_steer + 1
LEFT JOIN turns b ON b.machine = c.machine AND b.agent = c.agent AND b.session_id = c.session_id
                 AND b.turn_id = c.turn_id
LEFT JOIN turns a ON a.machine = s.machine AND a.agent = s.agent AND a.session_id = s.session_id
                 AND a.turn_id = s.turn_id
WHERE c.kind IN ('interrupt', 'rejection');

CREATE OR REPLACE VIEW agents.intervention_monthly AS
SELECT date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month,
       count(*)::int                                        AS cases,
       count(*) FILTER (WHERE kind = 'interrupt')::int      AS interrupts,
       count(*) FILTER (WHERE kind = 'rejection')::int      AS rejections,
       round(avg(before_tool_errors), 2)                    AS before_tool_errors_avg,
       round(avg(after_tool_errors), 2)                     AS after_tool_errors_avg,
       count(*) FILTER (WHERE before_retry)::int            AS before_retries,
       count(*) FILTER (WHERE after_retry)::int             AS after_retries,
       count(*) FILTER (WHERE before_runaway)::int          AS before_runaways,
       count(*) FILTER (WHERE after_runaway)::int           AS after_runaways,
       count(*) FILTER (WHERE before_commit)::int           AS before_commits,
       count(*) FILTER (WHERE after_commit)::int            AS after_commits
FROM agents.intervention_cases
GROUP BY 1;

-- Every eval record aut wrote (one row per backend per bake-off), numbers and ids only; per-label
-- detail of the newest one is agents.eval_latest.
CREATE OR REPLACE VIEW agents.eval_history AS
SELECT ts                                       AS evaluated_at,
       attrs->>'task'                           AS task,
       (attrs->>'task_ver')::int                AS task_ver,
       attrs->>'backend'                        AS backend,
       attrs->>'model_id'                       AS model_id,
       attrs->>'model_rev'                      AS model_rev,
       (attrs->>'size_mb')::double precision    AS size_mb,
       (attrs->>'temperature')::double precision AS temperature,
       (attrs->>'n')::int                       AS n,
       (attrs->>'n_cal')::int                   AS n_cal,
       (attrs->>'n_held')::int                  AS n_held,
       (attrs->>'macro_f1')::double precision   AS macro_f1,
       (attrs->>'kappa')::double precision      AS kappa,
       (attrs->>'ece_raw')::double precision    AS ece_raw,
       (attrs->>'ece_cal')::double precision    AS ece_cal,
       (attrs->>'coverage')::double precision   AS coverage,
       (attrs->>'s_per_decision')::double precision AS s_per_decision
FROM agents.activity_event
WHERE kind = 'eval';

-- ---------------------------------------------------------------------------------------------
-- Phase 6: what the publisher reads (weeks are ISO weeks by their Monday, days local days).
-- The database keeps every label; only the publisher's one table merges them for the public file.
-- ---------------------------------------------------------------------------------------------

-- Commits per local day from the full git scan, the latest scan of each day. Not the same
-- count as the commit rows above, which hold only what agent logs printed. Never add the two.
CREATE OR REPLACE VIEW agents.git_commits_daily AS
WITH latest_complete AS (
    SELECT DISTINCT ON (day) day, scanned_at
    FROM agents.git_commit_day
    WHERE complete
    ORDER BY day, scanned_at DESC
), candidates AS (
    SELECT g.day, g.commits, g.complete, g.scanned_at
    FROM agents.git_commit_day g
    LEFT JOIN latest_complete c USING (day)
    WHERE c.day IS NULL OR g.scanned_at >= c.scanned_at
)
-- Partial reads cannot lower a count or downgrade unchanged complete coverage.
-- A newer complete scan can still apply the collector's explicit audited reset.
SELECT DISTINCT ON (day) day, commits, complete
FROM candidates
ORDER BY day, commits DESC, complete DESC, scanned_at DESC;

-- Counted steers per local day, read live: effort_period's steer rule without waiting for its refresh.
-- claude_code_steers: those of Claude Code, the only agent whose steer text the labeler reads, so the
-- publisher waits for labels only in weeks that have some.
CREATE OR REPLACE VIEW agents.steers_counted_daily AS
SELECT (a.ts AT TIME ZONE '@AUT_TZ@')::date AS day, count(*)::int AS steers,
       count(*) FILTER (WHERE a.agent = 'claude_code')::int AS claude_code_steers
FROM (SELECT DISTINCT ON (event_id) ts, session_id, agent FROM agents.activity_event
      WHERE kind = 'steer' AND session_id IS NOT NULL ORDER BY event_id, ts) a
LEFT JOIN agents.session_origin o USING (session_id)
WHERE NOT coalesce(o.scripted, false)
GROUP BY 1;

-- Steer estimates per local day (aut history-steers: attrs.count, one row per local day and agent,
-- stamped noon local). Kept apart from counted steers: no other view reads this kind.
CREATE OR REPLACE VIEW agents.steer_estimate_daily AS
SELECT (ts AT TIME ZONE '@AUT_TZ@')::date AS day, sum((attrs->>'count')::int)::int AS steers_estimated
FROM (SELECT DISTINCT ON (event_id) ts, attrs FROM agents.activity_event
      WHERE kind = 'steer_estimate' AND attrs->>'count' ~ '^[0-9]+$' ORDER BY event_id, ts) a
GROUP BY 1;

-- The public session: interactive origin and at least one steer. Each count holds at its own grain
-- only: a session that spans days or weeks shows in each, so never add them into a total.
CREATE OR REPLACE VIEW agents.session_steer_day AS
SELECT DISTINCT (a.ts AT TIME ZONE '@AUT_TZ@')::date AS day, a.session_id
FROM (SELECT DISTINCT ON (event_id) ts, session_id FROM agents.activity_event
      WHERE kind = 'steer' ORDER BY event_id, ts) a
JOIN agents.session_origin o USING (session_id)
WHERE o.interactive;

CREATE OR REPLACE VIEW agents.sessions_steered_daily AS
SELECT day, count(*)::int AS sessions
FROM agents.session_steer_day
GROUP BY 1;

CREATE OR REPLACE VIEW agents.sessions_steered_weekly AS
SELECT date_trunc('week', day::timestamp)::date AS week, count(DISTINCT session_id)::int AS sessions
FROM agents.session_steer_day
GROUP BY 1;

-- Twins of label_share_monthly, work_mix_monthly and steer_intent_monthly.
CREATE OR REPLACE VIEW agents.label_share_weekly AS
SELECT task, week, label, units,
       CASE WHEN label IS NOT NULL
            THEN round(units::numeric / nullif(sum(units) FILTER (WHERE label IS NOT NULL) OVER w, 0), 4)
       END AS share,
       round(coalesce(sum(units) FILTER (WHERE label IS NULL) OVER w, 0)::numeric / sum(units) OVER w, 4)
           AS unknown_share
FROM (SELECT task, date_trunc('week', ts AT TIME ZONE '@AUT_TZ@')::date AS week,
             CASE WHEN NOT below_tau THEN label END AS label, count(*)::int AS units
      FROM agents.label_latest
      GROUP BY 1, 2, 3) x
WINDOW w AS (PARTITION BY task, week);

CREATE OR REPLACE VIEW agents.work_mix_weekly AS
SELECT week, label AS category, units, share AS unit_share, unknown_share
FROM agents.label_share_weekly WHERE task = 'work_category';

CREATE OR REPLACE VIEW agents.steer_intent_weekly AS
SELECT week, label AS intent, units, share, unknown_share
FROM agents.label_share_weekly WHERE task = 'steer_intent';

-- Counted episodes by the week they started, each once. still_open: git open or undecided, or last
-- active under 14 days ago (a policy choice, it can change here), whatever the outcome says. The
-- soft_ columns split no_code by the confident soft_outcome label; no_code is what has none.
CREATE OR REPLACE VIEW agents.outcomes_weekly AS
SELECT week,
       count(*)::int                                                AS episodes,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'shipped')::int   AS shipped,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'reworked')::int  AS reworked,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'ruled_out')::int AS ruled_out,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'no_code' AND soft IS NULL)::int AS no_code,
       count(*) FILTER (WHERE unsettled)::int                       AS still_open,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'no_code' AND soft = 'resolved')::int   AS soft_resolved,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'no_code' AND soft = 'blocked')::int    AS soft_blocked,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'no_code' AND soft = 'ruled_out')::int  AS soft_ruled_out,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'no_code' AND soft = 'still_open')::int AS soft_still_open
FROM (SELECT date_trunc('week', c.first_ts AT TIME ZONE '@AUT_TZ@')::date AS week, c.outcome, l.label AS soft,
             c.outcome IN ('open', 'undecided') OR c.last_ts + interval '14 days' > now() AS unsettled
      FROM agents.episode_counted c
      LEFT JOIN agents.label_latest l
        ON l.task = 'soft_outcome' AND l.unit = 'episode' AND NOT l.below_tau AND l.unit_id = c.episode_id::text) e
GROUP BY week;

-- Twin of shipping_rework_monthly: commits by the week their first commit row landed.
CREATE OR REPLACE VIEW agents.shipping_rework_weekly AS
SELECT c.week,
       count(*)::int                                                AS commits,
       count(*) FILTER (WHERE s.state = 'merged')::int              AS merged,
       count(*) FILTER (WHERE s.state = 'reverted')::int            AS reverted,
       count(*) FILTER (WHERE s.state = 'reworked')::int            AS reworked,
       count(*) FILTER (WHERE s.state = 'open')::int                AS open,
       count(*) FILTER (WHERE s.state = 'abandoned')::int           AS abandoned,
       count(*) FILTER (WHERE coalesce(s.state, 'undecided') = 'undecided')::int AS undecided
FROM (SELECT DISTINCT ON (left(lower(attrs->>'sha'), 7)) left(lower(attrs->>'sha'), 7) AS k,
             date_trunc('week', ts AT TIME ZONE '@AUT_TZ@')::date AS week
      FROM agents.activity_event
      WHERE kind = 'commit' AND lower(attrs->>'sha') ~ '^[0-9a-f]{7,40}$'
      ORDER BY 1, ts) c
LEFT JOIN agents.commit_state s USING (k)
WHERE s.state IS DISTINCT FROM 'superseded'
GROUP BY c.week;

-- Skill invokes with the week of their skill's first invoke. Twin of built_reused_monthly.
CREATE OR REPLACE VIEW agents.skill_invoke_week AS
SELECT attrs->>'skill' AS skill,
       date_trunc('week', ts AT TIME ZONE '@AUT_TZ@')::date AS week,
       row_number() OVER (PARTITION BY attrs->>'skill' ORDER BY ts, event_id) AS nth,
       min(date_trunc('week', ts AT TIME ZONE '@AUT_TZ@')::date) OVER (PARTITION BY attrs->>'skill') AS first_week
FROM (SELECT DISTINCT ON (event_id) event_id, ts, attrs FROM agents.activity_event
      WHERE kind = 'invoke' ORDER BY event_id, ts) a
WHERE coalesce(attrs->>'skill', '') NOT IN ('', 'mcp');

CREATE OR REPLACE VIEW agents.built_reused_weekly AS
SELECT week,
       count(*) FILTER (WHERE nth = 1)::int                         AS skills_new,
       count(*)::int                                                AS invocations,
       count(*) FILTER (WHERE nth > 1)::int                         AS later_invocations,
       count(DISTINCT skill) FILTER (WHERE first_week < week)::int  AS skills_reused
FROM agents.skill_invoke_week
GROUP BY week;

-- Real distinct counts over the 26 weeks ending with last_week, one row per week so the publisher
-- picks its window. Week rows cannot be added into these: a session or skill spans weeks.
CREATE OR REPLACE VIEW agents.distinct_26_weeks AS
WITH s AS MATERIALIZED (SELECT day, session_id FROM agents.session_steer_day),
r AS MATERIALIZED (SELECT DISTINCT skill, week FROM agents.skill_invoke_week WHERE first_week < week)
SELECT w.last_week,
       (SELECT count(DISTINCT s.session_id) FROM s
        WHERE s.day >= w.last_week - 175 AND s.day < w.last_week + 7)::int AS sessions,
       (SELECT count(DISTINCT r.skill) FROM r
        WHERE r.week >= w.last_week - 175 AND r.week <= w.last_week)::int AS skills_reused
FROM (SELECT generate_series(date_trunc('week', min(day)::timestamp),
                             now() AT TIME ZONE '@AUT_TZ@', interval '7 days')::date AS last_week
      FROM s) w;

-- Twin of follow_through_monthly. Not proved public: the publisher reads it behind its own switch.
CREATE OR REPLACE VIEW agents.follow_through_weekly AS
SELECT date_trunc('week', ts AT TIME ZONE '@AUT_TZ@')::date AS week,
       count(*)::int                                                AS commits,
       count(*) FILTER (WHERE follow_through)::int                  AS follow_through_commits,
       count(DISTINCT repo_key) FILTER (WHERE follow_through)::int  AS follow_through_repos
FROM agents.follow_through_commit
GROUP BY 1;

-- Provisional historical estimates stay separate from calibrated task names.
-- The importer sends only ids, enums and numbers, never review reasons or source text.
CREATE OR REPLACE VIEW agents.working_label_latest AS
SELECT DISTINCT ON (attrs->>'task', attrs->>'unit', attrs->>'unit_id')
       substring(attrs->>'task' from 9) AS task,
       attrs->>'unit_id' AS unit_id,
       ts,
       attrs->>'label' AS label,
       (attrs->>'population_weight')::numeric AS population_weight,
       attrs->>'label_source' AS label_source,
       attrs->>'model_id' AS model_id,
       attrs->>'baseline_id' AS baseline_id,
       coalesce(attrs->'signals', '{}'::jsonb) AS signals
FROM agents.activity_event
WHERE kind = 'label'
  AND attrs->>'label_policy' = 'working-sonnet-v1'
  AND attrs->>'provisional' = 'true'
  AND attrs->>'human_verified' = 'false'
  AND attrs->>'task' IN ('working.steer_intent', 'working.soft_outcome')
ORDER BY attrs->>'task', attrs->>'unit', attrs->>'unit_id',
         attrs->>'labelled_at' DESC NULLS LAST, event_id DESC;

CREATE OR REPLACE VIEW agents.working_label_monthly AS
SELECT task, month, label, units, estimated_units,
       estimated_units / sum(estimated_units) OVER w AS estimated_share,
       coalesce(sum(estimated_units) FILTER (WHERE label = 'unknown') OVER w, 0)
           / sum(estimated_units) OVER w AS unknown_share,
       'provisional'::text AS quality
FROM (
    SELECT task, date_trunc('month', ts AT TIME ZONE '@AUT_TZ@')::date AS month,
           label, count(*)::int AS units, sum(population_weight) AS estimated_units
    FROM agents.working_label_latest
    GROUP BY 1, 2, 3
) x
WINDOW w AS (PARTITION BY task, month);

-- Signal counts describe the reviewed subset, never a population-weighted prevalence.
CREATE OR REPLACE VIEW agents.working_signal_monthly AS
SELECT date_trunc('month', l.ts AT TIME ZONE '@AUT_TZ@')::date AS month,
       q.question, coalesce(l.signals->q.question->>'answer', 'not_reviewed') AS answer,
       count(*)::int AS units,
       CASE WHEN l.signals ? q.question THEN 'agent_review' ELSE 'none' END AS review_source,
       'provisional'::text AS quality
FROM agents.working_label_latest l
CROSS JOIN LATERAL unnest(CASE WHEN l.task = 'steer_intent'
    THEN ARRAY['explicit_halt', 'disputes_agent_work']
    ELSE ARRAY['required_next_step', 'external_blocker'] END) q(question)
GROUP BY 1, 2, 3, 5;

-- Private reference estimates describe only the frozen sampled frame, not all agent activity.
CREATE OR REPLACE VIEW agents.working_label_weekly AS
SELECT task, date_trunc('week', ts AT TIME ZONE '@AUT_TZ@')::date AS week,
       label, count(*)::int AS units, sum(population_weight) AS estimated_units
FROM agents.working_label_latest
GROUP BY 1, 2, 3;

-- Audited missing inputs cannot regain a classification through a model retry.
CREATE OR REPLACE VIEW agents.decision_input_status_latest AS
SELECT DISTINCT ON (c.attrs->>'task', c.attrs->>'task_ver', c.attrs->>'unit',
                    c.attrs->>'unit_id', c.attrs->>'input_hash')
       substring(c.attrs->>'task' from 15) AS task,
       CASE c.attrs->>'task_ver' WHEN '1' THEN 1 WHEN '2' THEN 2 WHEN '3' THEN 3 END AS task_ver,
       c.attrs->>'unit' AS unit, c.attrs->>'unit_id' AS unit_id,
       c.attrs->>'input_hash' AS input_hash, c.attrs->>'state' AS input_state
FROM agents.activity_event c
JOIN agents.activity_event t ON t.event_id = c.attrs->>'target_event_id'
  AND t.kind = 'label' AND t.attrs->>'label_source' = 'local_decision'
  AND t.attrs->>'task' = c.attrs->>'task' AND t.attrs->>'task_ver' = c.attrs->>'task_ver'
  AND t.attrs->>'unit' = c.attrs->>'unit' AND t.attrs->>'unit_id' = c.attrs->>'unit_id'
  AND t.attrs->>'input_hash' = c.attrs->>'input_hash'
WHERE c.kind = 'label_input_status' AND c.attrs->>'label_source' = 'local_input_guard'
  AND c.attrs->>'task_ver' IN ('1', '2', '3')
  AND ((c.attrs->>'input_contract' = 'episode-day-bounds-v1'
        AND c.attrs->>'task' IN ('working.local.work_category', 'working.local.writing_activity')
        AND c.attrs->>'unit' = 'episode_day')
    OR (c.attrs->>'input_contract' = 'codex-import-origin-v1'
        AND ((c.attrs->>'task' = 'working.local.work_category' AND c.attrs->>'unit' = 'episode_day')
          OR (c.attrs->>'task' = 'working.local.writing_activity' AND c.attrs->>'unit' = 'episode_day')
          OR (c.attrs->>'task' = 'working.local.steer_intent' AND c.attrs->>'unit' = 'steer')
          OR (c.attrs->>'task' = 'working.local.soft_outcome' AND c.attrs->>'unit' = 'episode'))))
  AND NULLIF(c.attrs->>'unit_id', '') IS NOT NULL
  AND c.attrs->>'input_hash' ~ '^[0-9a-f]{64}$'
  AND c.attrs->>'evidence_sha256' ~ '^[0-9a-f]{64}$'
  AND ((c.attrs->>'state' = 'unavailable' AND c.attrs->>'reason' = 'source_unavailable')
    OR (c.attrs->>'state' = 'available' AND c.attrs->>'reason' = 'audited_restore'))
ORDER BY c.attrs->>'task', c.attrs->>'task_ver', c.attrs->>'unit',
         c.attrs->>'unit_id', c.attrs->>'input_hash', c.ts DESC,
         (c.attrs->>'state' = 'unavailable') DESC, c.event_id DESC;

-- Public classifications follow each unit's latest decision, including an unknown result.
-- Reference-model labels remain in their separate private projections above.
CREATE OR REPLACE VIEW agents.decision_label_latest AS
WITH latest AS (
    SELECT DISTINCT ON (attrs->>'task', attrs->>'unit', attrs->>'unit_id')
           substring(attrs->>'task' from 15) AS task,
           attrs->>'unit' AS unit,
           attrs->>'unit_id' AS unit_id,
           ts,
           attrs->>'label' AS label,
           (attrs->>'labelled_at')::timestamptz AS labelled_at,
           (attrs->>'task_ver')::int AS task_ver,
           event_id, attrs->>'input_hash' AS input_hash
    FROM agents.activity_event
    WHERE kind = 'label'
      AND attrs->>'label_source' = 'local_decision'
      AND NULLIF(attrs->>'unit_id', '') IS NOT NULL
      AND ((attrs->>'task' = 'working.local.work_category'
            AND attrs->>'unit' IN ('episode_day', 'aw_block'))
        OR (attrs->>'task' = 'working.local.steer_intent' AND attrs->>'unit' = 'steer')
        OR (attrs->>'task' = 'working.local.writing_activity' AND attrs->>'unit' = 'episode_day')
        OR (attrs->>'task' = 'working.local.soft_outcome' AND attrs->>'unit' = 'episode'))
    ORDER BY attrs->>'task', attrs->>'unit', attrs->>'unit_id',
             (attrs->>'labelled_at')::timestamptz DESC NULLS LAST,
             (attrs->>'task_ver')::int DESC NULLS LAST, event_id DESC
)
SELECT l.task, l.unit, l.unit_id, l.ts, l.label, l.labelled_at, l.task_ver, l.event_id
FROM latest l
LEFT JOIN agents.episode e ON l.task = 'soft_outcome' AND e.episode_id::text = l.unit_id
WHERE (l.task <> 'soft_outcome' OR e.outcome = 'no_code')
  AND NOT EXISTS (
      SELECT 1 FROM agents.decision_input_status_latest s
      WHERE s.task = l.task AND s.task_ver = l.task_ver AND s.unit = l.unit
        AND s.unit_id = l.unit_id AND s.input_state = 'unavailable'
        AND (s.input_hash = l.input_hash OR l.input_hash IS NULL OR l.input_hash !~ '^[0-9a-f]{64}$')
  );

-- Private acceptance selector; a Writing-capable taxonomy is not active by default.
-- Created only when missing: an operator who switched it keeps the choice on the next apply.
DO $$
BEGIN
    IF to_regclass('agents.work_taxonomy_mode') IS NULL THEN
        CREATE VIEW agents.work_taxonomy_mode AS SELECT 2::integer AS version;
    END IF;
END $$;

-- Keep each taxonomy's latest result before selecting the accepted version.
-- A late legacy label cannot discard a Writing-capable result from this helper.
CREATE OR REPLACE VIEW agents.decision_work_latest_by_taxonomy AS
WITH latest AS (
    SELECT DISTINCT ON ((attrs->>'task_ver')::int, attrs->>'unit', attrs->>'unit_id')
           (attrs->>'task_ver')::int AS task_ver,
           attrs->>'unit' AS unit, attrs->>'unit_id' AS unit_id,
           ts, attrs->>'label' AS label,
           (attrs->>'labelled_at')::timestamptz AS labelled_at, event_id,
           attrs->>'input_hash' AS input_hash
    FROM agents.activity_event
    WHERE kind = 'label' AND attrs->>'label_source' = 'local_decision'
      AND attrs->>'task' = 'working.local.work_category'
      AND attrs->>'unit' IN ('episode_day', 'aw_block')
      AND NULLIF(attrs->>'unit_id', '') IS NOT NULL
    ORDER BY (attrs->>'task_ver')::int, attrs->>'unit', attrs->>'unit_id',
             (attrs->>'labelled_at')::timestamptz DESC NULLS LAST, event_id DESC
)
SELECT l.task_ver, l.unit, l.unit_id, l.ts, l.label, l.labelled_at, l.event_id
FROM latest l
WHERE NOT EXISTS (
    SELECT 1 FROM agents.decision_input_status_latest s
    WHERE s.task = 'work_category' AND s.task_ver = l.task_ver AND s.unit = l.unit
      AND s.unit_id = l.unit_id AND s.input_state = 'unavailable'
      AND (s.input_hash = l.input_hash OR l.input_hash IS NULL OR l.input_hash !~ '^[0-9a-f]{64}$')
);

-- Private acceptance selector; stricter outcome evidence is not active by default.
-- Created only when missing: an operator who switched it keeps the choice on the next apply.
DO $$
BEGIN
    IF to_regclass('agents.outcome_evidence_mode') IS NULL THEN
        CREATE VIEW agents.outcome_evidence_mode AS SELECT 2::integer AS version;
    END IF;
END $$;

-- Select the evidence contract before latest labels can discard accepted results.
CREATE OR REPLACE VIEW agents.decision_outcome_latest_by_contract AS
WITH latest AS (
    SELECT DISTINCT ON ((attrs->>'task_ver')::int, attrs->>'unit_id')
           (attrs->>'task_ver')::int AS task_ver, attrs->>'unit_id' AS unit_id,
           ts, attrs->>'label' AS label,
           (attrs->>'labelled_at')::timestamptz AS labelled_at, event_id,
           attrs->>'input_hash' AS input_hash
    FROM agents.activity_event
    WHERE kind = 'label' AND attrs->>'label_source' = 'local_decision'
      AND attrs->>'task' = 'working.local.soft_outcome' AND attrs->>'unit' = 'episode'
      AND NULLIF(attrs->>'unit_id', '') IS NOT NULL
    ORDER BY (attrs->>'task_ver')::int, attrs->>'unit_id',
             (attrs->>'labelled_at')::timestamptz DESC NULLS LAST, event_id DESC
)
SELECT l.task_ver, l.unit_id, l.ts, l.label, l.labelled_at, l.event_id
FROM latest l
WHERE NOT EXISTS (
    SELECT 1 FROM agents.decision_input_status_latest s
    WHERE s.task = 'soft_outcome' AND s.task_ver = l.task_ver AND s.unit = 'episode'
      AND s.unit_id = l.unit_id AND s.input_state = 'unavailable'
      AND (s.input_hash = l.input_hash OR l.input_hash IS NULL OR l.input_hash !~ '^[0-9a-f]{64}$')
);

-- Public outcome details use the same counted, settled cohort and first week as their Git parent.
-- The private latest-label helper above retains raw no-code decisions for other consumers.
CREATE OR REPLACE VIEW agents.decision_label_weekly AS
WITH accepted_work AS (
    SELECT min(version) AS version FROM agents.work_taxonomy_mode
    HAVING count(*) = 1 AND min(version) IN (2, 3)
), accepted_outcome AS (
    SELECT min(version) AS version FROM agents.outcome_evidence_mode
    HAVING count(*) = 1 AND min(version) IN (2, 3)
), labels AS (
    SELECT task, ts, label, unit_id FROM agents.decision_label_latest WHERE task NOT IN ('work_category', 'soft_outcome')
    UNION ALL
    SELECT CASE m.version WHEN 2 THEN 'work_category' WHEN 3 THEN 'work_category@3' END,
           w.ts, w.label, w.unit_id
    FROM agents.decision_work_latest_by_taxonomy w CROSS JOIN accepted_work m
    WHERE w.task_ver = m.version
    UNION ALL
    SELECT 'soft_outcome', o.ts, o.label, o.unit_id
    FROM agents.decision_outcome_latest_by_contract o CROSS JOIN accepted_outcome m
    WHERE o.task_ver = m.version
      AND (m.version = 2 OR (o.labelled_at IS NOT NULL AND EXISTS (
          SELECT 1 FROM agents.episode e
          WHERE e.episode_id::text = o.unit_id AND o.labelled_at >= e.last_ts
      )))
)
SELECT l.task,
       date_trunc('week', (CASE WHEN l.task = 'soft_outcome' THEN c.first_ts ELSE l.ts END)
                  AT TIME ZONE '@AUT_TZ@')::date AS week,
       l.label, count(*)::bigint AS units
FROM labels l
LEFT JOIN agents.episode_counted c ON l.task = 'soft_outcome' AND c.episode_id::text = l.unit_id
WHERE l.task <> 'soft_outcome'
   OR (c.outcome = 'no_code' AND c.last_ts + interval '14 days' <= now())
GROUP BY 1, 2, 3;

-- Git evidence stays separate from assessed outcomes for no-code episodes.
CREATE OR REPLACE VIEW agents.git_outcomes_weekly AS
SELECT week, count(*)::int AS episodes,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'shipped')::int AS shipped,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'reworked')::int AS reworked,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'ruled_out')::int AS ruled_out,
       count(*) FILTER (WHERE NOT unsettled AND outcome = 'no_code')::int AS no_code,
       count(*) FILTER (WHERE unsettled)::int AS still_open
FROM (SELECT date_trunc('week', first_ts AT TIME ZONE '@AUT_TZ@')::date AS week,
             outcome, outcome IN ('open', 'undecided') OR last_ts + interval '14 days' > now() AS unsettled
      FROM agents.episode_counted) e
GROUP BY 1;
