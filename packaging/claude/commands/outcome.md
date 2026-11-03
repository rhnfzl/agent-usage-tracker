---
description: Record what became of this session's work (shipped, ruled-out or open) for aut episodes
argument-hint: shipped | ruled-out | open
allowed-tools: Bash(aut outcome:*)
---
If `$ARGUMENTS` is not exactly one of `shipped`, `ruled-out` or `open`, ask which one applies and
stop. Otherwise run `aut outcome $ARGUMENTS` with the Bash tool from the current directory and
report its one-line result. It marks the newest interactive session of this project, which is
this one; nothing but the session id, the outcome and a repo key is stored.
