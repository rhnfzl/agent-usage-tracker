---
description: Record what became of this session's work (shipped, ruled-out or open) for aut episodes
argument-hint: shipped | ruled-out | open
allowed-tools: Bash(aut outcome:*)
---
If `$ARGUMENTS` is not exactly one of `shipped`, `ruled-out` or `open`, ask which one applies and
stop. Otherwise run `aut outcome $ARGUMENTS --session "${CLAUDE_SESSION_ID}"` with the Bash tool
from the current directory and report its one-line result. The session id names this session, so
another session open in the same project is never marked; nothing but the session id, the outcome
and a repo key is stored.
