#!/bin/bash
# Optional wrapper for the launchd timer. Runs `aut scan` and leaves a marker file behind when
# the push could not be delivered, which is what dev.aut.retry.plist watches: while the marker
# exists launchd re-runs this every ThrottleInterval, so usage missed with the VPN down lands as
# soon as the sink is reachable instead of waiting out the full StartInterval.
#
# Only worth wiring up if your scan interval is long, or the sink sits behind a VPN/tunnel that
# is often unavailable. On the stock 15-minute timer the marker buys you almost nothing.
#
# Point dev.aut.scan.plist's ProgramArguments at this script instead of `aut scan` directly.
set -u

AUT="${AUT_BIN:-$HOME/.local/bin/aut}"          # override: AUT_BIN=/path/to/aut
MARK="$HOME/.config/agent-usage-tracker/retry-pending"

"$AUT" scan
rc=$?

# `aut scan` exits non-zero when delivery fails: push.py raises, and neither run_scan() nor
# main() swallows it. The watermark is not advanced for the undelivered batch, so the retry
# re-sends it and the sink discards the repeat on event_id.
if [ "$rc" -eq 0 ]; then rm -f "$MARK"; else touch "$MARK"; fi
exit "$rc"
