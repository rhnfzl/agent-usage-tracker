# A long gap, a backup, and a new laptop

aut reads logs that live only on your machine. The sink keeps counts and keys, never text, so the
logs are the one copy of your prompts, agent replies and window titles. This page covers three
cases: the laptop is away for a while, the laptop backs itself up, and a new laptop replaces it.

## The laptop was away for a week or a month

There is nothing to do. When the laptop comes back:

- The scan runs at login and on wake (`packaging/launchd/dev.aut.scan.plist` has `RunAtLoad` and a
  `StartCalendarInterval`, which launchd runs once on wake for a slot missed while asleep). Without
  them, a `StartInterval` job waits out its whole interval first. On an older install, reload the
  plist from this repo, or run `aut scan` once by hand.
- Every collector resumes from its own bookmark. `cursor_api` walks the gap one day at a time,
  oldest first, and moves its bookmark after each day, so a long gap loses nothing. Two limits:
  the day windows assume that Cursor's API reads `endDate` as inclusive (not checked against the
  real API; an event exactly on a window edge is read twice and dropped as a repeat), and one day
  can hold at most 20,000 events (200 pages of 100). A single day with more than that still loses
  the rest, and the scan prints a warning to stderr when it hits the cap.
- Day labels (`work_category`, `daily_note`) are built from the sink's episodes, which the sink
  rebuilds once a night. `aut label run` labels a day only once a rebuild has seen that day's
  data. So the gap days are labelled the night after the catch-up, not skipped. (It reads the
  rebuild time from an `agents.collector_scan` row with collector `episode_link`. If the sink
  does not write one, it uses the newest episode's day instead.)
- Steers whose text is not on this machine are counted and printed by `aut label run`
  (`N steers skipped: their text is not on this machine`).

Do not run a plain `aut backfill` to catch up. It resets the bookmark and re-reads everything (see
the dedup step below). `aut scan` already is the catch-up.

## Back up the laptop, before you need it

`aut backup` sends an encrypted, deduplicated [restic](https://restic.net) snapshot to a
repository you choose (an SFTP server, a restic REST server, S3, ...). It covers:

- `~/.claude/projects` and `~/.claude/history.jsonl`: Claude Code sessions and typed history
- `~/.codex/sessions`, `~/.codex/history.jsonl`, `~/.codex/session_index.jsonl` and
  `~/.codex/archived_sessions`: Codex sessions and history
- `~/.cursor/chats`, `~/.cursor/projects` and the desktop Cursor User directory: CLI, SDK and IDE
  stores. An explicit `cursor_db_path` backs up that database and its WAL/SHM files only.
- `~/.pi/agent/sessions`: Pi sessions. Files matching `cursor_sdk_manifests` also count, including
  manifests outside the standard Cursor folders.
- `~/Library/Application Support/activitywatch`: the window history
- `~/.config/agent-usage-tracker`: `config.toml`, `label.json`, `scan.env`
- `~/.local/share/aut`: the label state and the `eval/` gold files, but not the model caches under
  `label/models` (they download again)
- `~/.local/state/agent-usage-tracker`: `repo_key.salt` and the bookmarks

The first run sends everything (several GB). Later runs send only what changed.

1. Install restic: `brew install restic`.
2. Make a long random password and store it in the macOS keychain. The prompt keeps it out of your
   shell history:

   ```bash
   security add-generic-password -a "$USER" -s aut-backup -w
   ```

   Keep a second copy in your password manager. Without it the backup cannot be read.
3. Add the `[backup]` table to `config.toml` (see `config.example.toml`). It goes above
   `[transport]`:

   ```toml
   [backup]
   repository = "sftp:backup-host:/srv/restic/aut"     # your restic repository string
   password_command = "security find-generic-password -s aut-backup -w"
   ```

   Write the repository string itself, not `env:VAR`. launchd starts the backup job with only the
   PATH its plist sets, so the variable is not there and every nightly run would fail.

   `paths = [...]` replaces the list above. `excludes = [...]` adds to the model-cache exclude.
4. Create the repository once:

   ```bash
   RESTIC_REPOSITORY="<repository>" \
   RESTIC_PASSWORD_COMMAND="security find-generic-password -s aut-backup -w" restic init
   ```

5. Check it, then run the first backup: `aut backup --check`, then `aut backup`.
6. Install the daily job: `packaging/launchd/dev.aut.backup.plist` (steps in the file). It runs at
   02:30, at login, and on wake when 02:30 was missed. It skips, with exit 0 and a log line, when
   another run is still going, when the laptop is on battery below 20%, or when the repository
   cannot be reached. A wrong password or a missing repository is an error.

After each good backup, `aut backup` also runs the retention on this machine's snapshots:
`restic forget --tag aut --host <machine> --keep-daily 14 --keep-weekly 8 --keep-monthly 12`, with
`--prune` at most once a week (the time of the last prune is kept in `backup.json`). Nothing else
needs to run a retention on this repository.
Set `retention = false` in `[backup]` to keep every snapshot and disable both forget and prune.
Use this for a new installation until retention deletion is explicitly approved.

`aut status` shows the age of the last good backup.

The backup password must have a recovery copy outside the Mac. Keep it separate from the SFTP
account and repository, readable only by the recovery owner. Test a restore before relying on
the first snapshot. A live SQLite store can change during backup; inspect restored databases
with SQLite's integrity check when verifying a backup.

## Move to a new laptop

1. Install aut (see the README) and restic, and put the backup password back in the keychain
   (step 2 above, with the same password).
2. Restore into a scratch folder. restic keeps absolute paths, so your old home's path appears
   under it:

   ```bash
   export RESTIC_REPOSITORY="<repository>"
   export RESTIC_PASSWORD_COMMAND="security find-generic-password -s aut-backup -w"
   restic snapshots --tag aut
   restic restore latest --tag aut --host <machine> --target ~/aut-restore
   ```

   `<machine>` is the `machine` value from the old `config.toml`, which aut passes to restic as
   the host.
3. Copy each folder from `~/aut-restore/<old home path>/` to the same place in your new home.
   Quit ActivityWatch before you copy its folder.
4. Config and salt: `~/.config/agent-usage-tracker/` and `~/.local/state/agent-usage-tracker/`
   came back with the restore. Run `chmod 600` on `config.toml` and `repo_key.salt`. Keep the
   same `machine =` value. You can also set `repo_key_salt` in `config.toml` to the contents of
   the old `repo_key.salt`. Without the old salt, every repo key from the new laptop would differ
   from the old ones, and old and new work on a repo would never join into one episode. So
   `aut scan` (also with `--dry-run`), `aut label` and `aut outcomes` refuse to run when the salt
   file is missing and the sink already holds this machine's repo keys. Two cases it cannot see,
   so restore the salt before any aut command anyway:
   - With no `pg_dsn` (a webhook-only sink) aut cannot ask the sink. It prints a warning and
     makes a new salt.
   - The check looks for keys under this machine's name. If you give the new laptop a new
     `machine =` name, it finds none and makes a new salt. Keep the old name, or copy the old
     salt first.
5. ActivityWatch reads the buckets named after this computer's hostname. Give the new laptop the
   old hostname before you start ActivityWatch, or its old history is not read.
6. Run `aut scan` once by hand. It reads every file again from the start, because the files are
   new to this machine. That is slow, and the sink drops the repeats.
7. Run the sink's one-off dedup script once (`scripts/sql/agents-dedup-once.sql` in the sink's
   repository). A full re-read can add a few extra raw usage rows: a message whose two log lines
   fell into different read chunks comes back with a partial total. The `usage_event_dedup` view
   already ignores them. The script also removes them from the raw table, which some dashboards
   read.
8. Install the two launchd jobs: the scan (`dev.aut.scan.plist`, or the wrapper and retry job in
   `packaging/README.md`) and the backup (`dev.aut.backup.plist`). Both run at login, and the scan
   also runs on wake.
9. If `[git_days]` is enabled, check that `ssh -o BatchMode=yes <host> true` works for each
   remote (keys and `~/.ssh/config` aliases), and run `aut git-days --dry-run`: its year totals
   should match the old laptop's. Clone every repo the old laptop had under `roots` first: a run
   that finds under half the repositories of the best recent run writes nothing, and a day whose
   count would go down is kept as stored (the summary counts them; `--allow-lower` rewrites).
   Restore `git-days-sources.json` beside the watermark with its original repository salt.
   It retains verified commit identities and source coverage across offline machines.
   Use `aut git-days --local-only` while remotes are unavailable. Missing coverage stays unknown.
   A lower-count reset requires every configured source freshly complete.
10. On the machine that labels (`AUT_LABEL=1`), download the label models again. `label.json` and
   the gold files came back with the restore.
