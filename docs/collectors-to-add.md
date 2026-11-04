# Collectors ready to add (not built - no local data to build against)

The tracker builds every collector against real on-disk data (never a guessed schema). These are
scoped and ready to build the moment the tool is installed on a machine that uses them.

## opencode (Go variant) - local SQLite

**Source** (confirmed via CodexBar research, `docs/research/codexbar-learnings.md`): the Go build of
opencode keeps usage in a local SQLite DB at:

```
~/.local/share/opencode/opencode.db      # Linux / macOS (XDG data dir)
```

(The TypeScript/cloud opencode uses a web dashboard RPC with no local usage file - not parseable.)

**Why not built yet:** opencode is not installed on any tracked machine, so there is no real DB to
inspect the table/column names or verify the token fields. Building blind against the documented
shape risks being wrong on the actual schema.

**To build it** (once opencode is installed and has been used):
1. Inspect the schema: `sqlite3 ~/.local/share/opencode/opencode.db '.tables'` then `.schema <table>`
   for the message/usage table. Look for per-message token columns (input/output/cache) + a model
   column + a timestamp + a stable row id.
2. Copy `collectors/cursor.py` as the template - it is the existing read-only SQLite collector
   (`sqlite3.connect(file:...?mode=ro)`, `PRAGMA query_only`, full-scan + `event_id` dedup at the
   sink). opencode.db is a friendlier relational shape than Cursor's blob KV, so the mapping is
   more direct.
3. Map each usage row -> `UsageEvent(agent="opencode", model=..., input/output/cache_*_tokens=...,
   ts=..., provider=...)`, `event_id = make_event_id("opencode", <stable row id>)`.
4. Register in `collectors/__init__.py`, add `"opencode"` to `ALL_AGENTS`, add a fixture-backed test.
5. If its logs hold prompts, add a reader to `collectors/session_digest.py` and call it from
   `local_info`, so `aut episodes review` and `aut inspect` show its sessions' local text.

Effort: ~1 hour once the real DB exists to read the schema from.

## Notes on other agents CodexBar covers (deliberately not planned)

- **Windsurf** - `state.vscdb` usage blob is undocumented; needs reverse-engineering a key. Low value.
- **JetBrains AI** - `AIAssistantQuotaManager2.xml` is quota-only (no token log), so it would feed a
  quota gauge, not token-usage events. Different data model; revisit only if quota tracking is added.
- **Amp / Kiro** - exposed via a CLI-invocation path (`amp usage`, `kiro-cli ... /usage`), i.e.
  scraping a command's output, not parsing a log file. Out of scope because it's live CLI execution,
  a different collector shape from "tail a file", not because of stored credentials.
- **Gemini CLI / Copilot / everything else** - API-key / OAuth / browser-cookie based. Capturing
  them would turn this from a passive local parser into a live-polling credential client, which is
  out of scope. (The one exception already made is `cursor_api`, opt-in and off by default.)
