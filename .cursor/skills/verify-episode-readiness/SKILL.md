---
name: verify-episode-readiness
description: Check whether label progress waits for a successful episode rebuild that covers the scan.
---

# Verify episode readiness

A label job must wait for a rebuild that includes the scan before it advances the saved label date.
Advancing too early can skip a day permanently.

Run `./scripts/local-ci.sh check` from the checkout root with local Docker and `psql` available.
The `sink-sql` job in GitHub Actions runs these same checks against the shipped TimescaleDB schema.

- `test_episode_watermark_matches_every_bounded_schedule` checks the real readiness query after all
  224 steps across 28 schedules. The model has one source scan, two successful rebuilds, and one
  aborted rebuild between them. It treats the rebuild's published scan coverage as the set of scans
  that had finished when that rebuild started. Aborting leaves the last published coverage in place.
- `test_label_cli_waits_for_a_rebuild_to_cover_the_scan` runs the installed CLI against TimescaleDB.
  It checks that the saved label date stays pending after an overlap and advances after the retry.

To replay the old defect, use a scratch worktree. In `Source.episodes_ready`, change the rebuild
cutoff from `max(started_at)` to `max(finished_at)` and run the checks. The query should admit
a scan that finished after rebuild start. The model test prints the failing schedule prefix.
The CLI test should see a successful exit before the retry. Restore the cutoff and run again.

This model checks readiness given committed receipts with distinct timestamps and one source.
It does not simulate database scheduling inside a read, clock skew, or delivery over the network.
The other SQL readiness tests cover multiple sources and legacy sinks.

Coverage map, verified against production code at `fa8caed6ae2871c92568ca0d662398d1c281216d`:

- `src/agent_usage_tracker/sink_stack/sql/refresh.sql` serializes rebuilds and writes the receipt
  in the same transaction as the rebuild.
- `src/agent_usage_tracker/label/tasks.py`, `Source.episodes_ready`, reads committed receipts.
- `src/agent_usage_tracker/label/run.py`, `_day_window` and `_Run.advance`, apply readiness
  to the local label date.
- `tests/pipeline/episode_readiness_model.py` defines the transitions independently of the SQL.

When a covered file changes, check the transition assumptions against the code. Replay the broken
cutoff and the intended behavior before moving the sync commit forward. Use
`/maintain-formal-verification-skill` to revisit the model after changes.
