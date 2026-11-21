-- Grants for agents.usage_event (companion to 01_schema.sql). Idempotent and
-- role-tolerant: every grant is guarded by an EXISTS check so a fresh database
-- (where the roles may not exist yet) does not abort under ON_ERROR_STOP.
--
-- Roles (create separately with their own passwords; this file only manages grants):
--   * agents_ro     -> SELECT. The Grafana "agents" datasource logs in as this. NOT on
--                      time_interval (window titles): only time_interval_public and the
--                      active-time views, none of which carries a title or the
--                      title-derived event_id.
--   * agents_writer -> USAGE on schema agents + SELECT/INSERT on usage_event, collector_scan,
--                      event_session and activity_event, INSERT only on time_interval.
--                      UPDATE on activity_event.attrs corrects late turn counts. No DELETE;
--                      other columns and tables stay append-only + deduped by ON CONFLICT.
--                      daily_note (phase 4): INSERT + SELECT on day only; agents_ro none.
--   * ledger_reader -> read-only role for a publisher: only the views and columns its queries
--                      name. No usage_event, no attrs, no active minutes.
--
-- The writer is aut on each collector workstation, not a server-side job, so the database's
-- network access rules must admit every workstation that runs a collector.
--
-- A fresh LOGIN role has no USAGE on non-public schemas, and table privileges are inert
-- without it, so USAGE on schema agents is granted explicitly.
--
-- Applied via psql -f, after 01_schema.sql. Safe to re-run any number of times.

DO $$
DECLARE
    have_ro     boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_ro');
    have_writer boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agents_writer');
    have_ledger boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_reader');
BEGIN
    IF have_writer THEN
        GRANT USAGE ON SCHEMA agents TO agents_writer;
        GRANT SELECT, INSERT ON agents.usage_event TO agents_writer;
        GRANT SELECT ON agents.usage_event_dedup TO agents_writer;
        -- Heartbeat now, session identity and activity in later steps. Same INSERT-only rule.
        GRANT SELECT, INSERT ON agents.collector_scan TO agents_writer;
        GRANT SELECT, INSERT ON agents.event_session TO agents_writer;
        GRANT SELECT, INSERT ON agents.activity_event TO agents_writer;
        GRANT UPDATE (attrs) ON agents.activity_event TO agents_writer;
        -- Active time: aut delivers aw + manual intervals; only the postgres-run retention job
        -- ever updates a row (it nulls old titles). No table SELECT: the writer's password sits on
        -- every workstation, so a leaked DSN must not read other machines' titles. aut's
        -- INSERT ... ON CONFLICT (event_id, ts) DO NOTHING needs SELECT on the conflict-target
        -- columns only (Postgres checks the arbiter columns), so just those two.
        -- The table REVOKE also drops column grants, then they are re-granted: idempotent, and it
        -- undoes the first version's table-wide grant.
        REVOKE SELECT ON agents.time_interval FROM agents_writer;
        GRANT INSERT ON agents.time_interval TO agents_writer;
        GRANT SELECT (event_id, ts) ON agents.time_interval TO agents_writer;
        -- Phase 3: aut writes git outcomes, the manual /outcome and private episode corrections
        -- (insert-only, ON CONFLICT DO NOTHING) and reads the linker's episodes for the review.
        GRANT SELECT, INSERT ON agents.commit_outcome TO agents_writer;
        GRANT SELECT, INSERT ON agents.manual_outcome TO agents_writer;
        GRANT SELECT, INSERT ON agents.episode_correction TO agents_writer;
        GRANT SELECT ON agents.episode TO agents_writer;
        GRANT SELECT ON agents.episode_member TO agents_writer;
        -- Phase 4: labels and evals are activity_event rows (granted above). Nightly notes are
        -- text: insert plus SELECT on the conflict column only, same reason as time_interval.
        REVOKE SELECT ON agents.daily_note FROM agents_writer;
        GRANT INSERT ON agents.daily_note TO agents_writer;
        GRANT SELECT (day) ON agents.daily_note TO agents_writer;
        -- Phase 6: commits per day from the user's git scan, counts only. aut git-days inserts
        -- new scans and reads each day's latest value, to write only the days that changed.
        GRANT SELECT, INSERT ON agents.git_commit_day TO agents_writer;
        GRANT SELECT ON agents.git_commits_daily TO agents_writer;
    END IF;
    IF have_ro THEN
        GRANT USAGE ON SCHEMA agents TO agents_ro;
        REVOKE ALL ON agents.work_taxonomy_mode, agents.decision_work_latest_by_taxonomy,
            agents.outcome_evidence_mode, agents.decision_outcome_latest_by_contract,
            agents.decision_input_status_latest FROM agents_ro;
        GRANT SELECT ON agents.usage_event TO agents_ro;
        GRANT SELECT ON agents.usage_event_dedup TO agents_ro;
        GRANT SELECT ON agents.usage_daily TO agents_ro;
        GRANT SELECT ON agents.collector_scan TO agents_ro;
        GRANT SELECT ON agents.event_session TO agents_ro;
        GRANT SELECT ON agents.activity_event TO agents_ro;
        GRANT SELECT ON agents.collector_coverage TO agents_ro;
        -- Window titles are private: agents_ro never reads the table, only views without them.
        -- The REVOKE also undoes any earlier hand grant.
        REVOKE ALL ON agents.time_interval FROM agents_ro;
        GRANT SELECT ON agents.time_interval_public TO agents_ro;
        GRANT SELECT ON agents.steer_interval TO agents_ro;
        GRANT SELECT ON agents.active_interval TO agents_ro;
        GRANT SELECT ON agents.active_daily_live TO agents_ro;
        GRANT SELECT ON agents.active_daily TO agents_ro;
        -- Phase 2b calibration: parameters and learned apps carry no title.
        GRANT SELECT ON agents.time_params TO agents_ro;
        GRANT SELECT ON agents.app_learned TO agents_ro;
        GRANT SELECT ON agents.time_interval_effective TO agents_ro;
        -- Phase 3: the episode tables stay closed; episode_monthly gives monthly counts only.
        -- Phase 5 below deliberately opens per-episode rows (tokens_cost_by_episode: episode_id,
        -- outcome) and per-case session ids (intervention_cases) to agents_ro, for a private
        -- Grafana only: with activity_event that maps sessions to episodes and their outcomes.
        REVOKE ALL ON agents.commit_outcome FROM agents_ro;
        REVOKE ALL ON agents.manual_outcome FROM agents_ro;
        REVOKE ALL ON agents.episode_correction FROM agents_ro;
        REVOKE ALL ON agents.episode FROM agents_ro;
        REVOKE ALL ON agents.episode_member FROM agents_ro;
        GRANT SELECT ON agents.episode_monthly TO agents_ro;
        -- Phase 4: label and eval views (ids, enums, numbers; agents_ro reads activity_event
        -- already, so no new exposure). Nightly notes are private text: no access at all.
        GRANT SELECT ON agents.label_latest TO agents_ro;
        GRANT SELECT ON agents.eval_latest TO agents_ro;
        REVOKE ALL ON agents.daily_note FROM agents_ro;
        -- Phase 5 (03_effort.sql): private effort, outcome, cost and audit views for
        -- a private Grafana. Counts, shares, ids and numbers; no title, text or machine. The
        -- helper views and *_live twins stay ungranted: they read the phase 3 tables agents_ro
        -- may not, and the twins are too slow for a panel.
        GRANT SELECT ON agents.effort_period, agents.effort_daily, agents.effort_weekly,
            agents.effort_monthly, agents.label_share_monthly, agents.work_mix_monthly,
            agents.steer_intent_monthly, agents.outcomes_monthly, agents.episodes_revisits_weekly,
            agents.parallel_steering_weekly, agents.shipping_rework_monthly, agents.coverage_monthly,
            agents.built_reused_monthly, agents.learning_to_doing_monthly,
            agents.follow_through_monthly, agents.tokens_cost_by_model_daily,
            agents.tokens_cost_by_episode, agents.efficiency_by_episode, agents.audit_daily,
            agents.intervention_cases, agents.intervention_monthly, agents.eval_history, agents.working_label_monthly, agents.working_signal_monthly,
            agents.git_commits_daily, agents.steers_counted_daily, agents.steer_estimate_daily,
            agents.sessions_steered_daily, agents.sessions_steered_weekly, agents.label_share_weekly,
            agents.work_mix_weekly, agents.steer_intent_weekly, agents.outcomes_weekly,
            agents.shipping_rework_weekly, agents.built_reused_weekly, agents.distinct_26_weeks,
            agents.follow_through_weekly, agents.decision_label_weekly TO agents_ro;
    END IF;
    IF have_ledger THEN
        GRANT USAGE ON SCHEMA agents TO ledger_reader;
        -- Undo earlier label-view grants; publication reads only the aggregate projections.
        REVOKE ALL ON agents.work_mix_weekly, agents.steer_intent_weekly,
            agents.outcomes_weekly, agents.working_label_latest, agents.working_label_monthly,
            agents.working_signal_monthly, agents.working_label_weekly,
            agents.decision_label_latest, agents.work_taxonomy_mode,
            agents.decision_work_latest_by_taxonomy, agents.outcome_evidence_mode,
            agents.decision_outcome_latest_by_contract, agents.decision_input_status_latest FROM ledger_reader;
        GRANT SELECT (kind, ts) ON agents.activity_event TO ledger_reader;
        GRANT SELECT (last_ok_at) ON agents.collector_coverage TO ledger_reader;
        GRANT SELECT (week, focus_blocks, parallel_peak) ON agents.effort_weekly TO ledger_reader;
        GRANT SELECT (month, source, days) ON agents.coverage_monthly TO ledger_reader;
        GRANT SELECT ON agents.git_commits_daily, agents.steers_counted_daily, agents.steer_estimate_daily,
            agents.sessions_steered_daily, agents.sessions_steered_weekly, agents.decision_label_weekly,
            agents.git_outcomes_weekly, agents.shipping_rework_weekly,
            agents.built_reused_weekly, agents.distinct_26_weeks, agents.follow_through_weekly TO ledger_reader;
    END IF;
END $$;

-- Calibration runs as postgres from the daily job only. Functions are executable by PUBLIC by
-- default; they run with the caller's rights, so this is belt and braces.
REVOKE EXECUTE ON FUNCTION agents.time_calibration(timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION agents.time_calibrate(timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION agents.episode_link() FROM PUBLIC;
