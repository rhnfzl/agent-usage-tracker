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
# Export sourced settings to the installed Python runtime that owns locking and child cleanup.
set -a
if [ -f "$CONF/scan.env" ]; then
  . "$CONF/scan.env" || exit 2
fi
set +a
exec "${AUT_BIN:-$HOME/.local/bin/aut}" scan-job
