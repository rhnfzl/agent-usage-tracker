#!/bin/bash
# Timer wrapper for `aut scan`. Optional on every platform -- the bare `aut scan` in the shipped
# launchd/systemd units is still fine if you need none of this.
#
# It adds five things `aut scan` cannot do for itself:
#
#   1. Opens an SSH tunnel first, for a sink that is not directly reachable (a homelab Postgres
#      behind Tailscale, say). Reuses a tunnel that is already up and only tears down its own.
#   2. Leaves a marker file behind when the push fails, which dev.aut.retry.plist watches, so a
#      scan missed with the VPN down lands as soon as the network is back instead of waiting out
#      the whole timer interval.
#   3. Timestamps both into one log you can diagnose from afterwards.
#   4. Runs `aut outcomes` once a day after a delivered scan.
#   5. Runs `aut label run` once a day after that, where AUT_LABEL=1 (the machine with the model).
#
# Per-machine settings live in ~/.config/agent-usage-tracker/scan.env, sourced if it exists, so
# this file is identical on every machine and stays a single thing to maintain:
#
#   AUT_BIN=/path/to/aut                     # default: ~/.local/bin/aut
#   AUT_TUNNEL_HOST=user@host                # unset (the default) means no tunnel, scan direct
#   AUT_TUNNEL_SPEC=15432:10.0.0.5:5432      # ssh -L argument; required with AUT_TUNNEL_HOST
#   AUT_LABEL=1                              # run the nightly `aut label run` here (needs aut[label])
#   AUT_LABEL_MODE=local                     # saved local estimates, separate from calibrated labels
#
# The env file exists because launchd and systemd do not inherit your shell environment. Keep
# secrets out of it: they belong in config.toml, which is already chmod 600.
set -u
# launchd omits Homebrew; outcomes needs gh even when aut itself uses an absolute path.
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin"

CONF="$HOME/.config/agent-usage-tracker"
LOG="$CONF/scan.log"
MARK="$CONF/retry-pending"
[ -f "$CONF/scan.env" ] && . "$CONF/scan.env"

AUT="${AUT_BIN:-$HOME/.local/bin/aut}"
HOST="${AUT_TUNNEL_HOST:-}"
SPEC="${AUT_TUNNEL_SPEC:-}"
ts() { date "+%Y-%m-%dT%H:%M:%S"; }

opened=""
if [ -n "$HOST" ]; then
  if [ -z "$SPEC" ]; then
    # Misconfiguration, not an outage. Fail loudly and do NOT arm the marker, or the retry job
    # would spin on it forever.
    echo "$(ts) AUT_TUNNEL_HOST is set but AUT_TUNNEL_SPEC is not; refusing to scan" >>"$LOG"
    exit 2
  fi
  port="${SPEC%%:*}"
  if ! nc -z localhost "$port" 2>/dev/null; then
    if ssh -f -N -o ExitOnForwardFailure=yes -o ConnectTimeout=15 -L "$SPEC" "$HOST" 2>>"$LOG"
    then
      opened=1
      sleep 2
    else
      echo "$(ts) tunnel FAILED; armed retry-pending (retries until the host is reachable)" >>"$LOG"
      touch "$MARK"
      exit 0        # an unreachable host is not a scan failure; the retry job owns the next try
    fi
  fi
fi

echo "$(ts) scan start" >>"$LOG"
"$AUT" scan >>"$LOG" 2>&1
rc=$?
echo "$(ts) scan done (exit $rc)" >>"$LOG"

# `aut scan` exits non-zero when delivery fails: push.py raises, and neither run_scan() nor
# main() swallows it. The watermark is not advanced for the undelivered batch, so the retry
# re-sends it and the sink discards the repeat on event_id.
if [ "$rc" -eq 0 ]; then rm -f "$MARK"; else touch "$MARK"; fi

# Once a day after a delivered scan: `aut outcomes` (commit outcomes from the local clones). A
# separate step with its own heartbeat (collector `outcomes`), so it never changes the scan's exit
# code. One attempt a day, failed or not: a failure is in its heartbeat row and the log, and
# retrying it after every scan would redo the whole clone pass all day. outcome_fetch = true in
# config.toml fetches first.
DAYMARK="$CONF/outcomes-day"
if [ "$rc" -eq 0 ] && [ "$(cat "$DAYMARK" 2>/dev/null)" != "$(date +%F)" ]; then
  date +%F >"$DAYMARK"
  echo "$(ts) outcomes start" >>"$LOG"
  "$AUT" outcomes >>"$LOG" 2>&1
  orc=$?    # before "$(ts)" below, which resets $?
  echo "$(ts) outcomes done (exit $orc)" >>"$LOG"
fi

# Once a day after a delivered scan, only where AUT_LABEL=1: `aut label run` (the tasks in
# label_tasks, inside label_budget_s). After outcomes, and the same rules: a separate step with its
# own heartbeat (collector `label`), never the scan's exit code, one attempt a day.
LABELMARK="$CONF/label-day"
if [ "$rc" -eq 0 ] && [ "${AUT_LABEL:-}" = "1" ] \
    && [ "$(cat "$LABELMARK" 2>/dev/null)" != "$(date +%F)" ]; then
  date +%F >"$LABELMARK"
  echo "$(ts) label start" >>"$LOG"
  label_args=()
  if [ "${AUT_LABEL_MODE:-calibrated}" = "local" ]; then label_args+=(--local-estimates); fi
  "$AUT" label run "${label_args[@]}" >>"$LOG" 2>&1
  lrc=$?
  echo "$(ts) label done (exit $lrc)" >>"$LOG"
fi

[ -n "$opened" ] && pkill -f "$SPEC" 2>/dev/null
exit "$rc"
