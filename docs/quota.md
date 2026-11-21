# Private Claude Code quota and operational status

AUT shows the last quota reading, when the window resets, and whether recent use is on pace to reach the limit. It uses the data that Claude Code already sends to a configured statusline command.

## Enable local capture

Claude Code can include five-hour and seven-day rate limits in the statusline JSON. Both windows can be absent. AUT keeps the percentage used and the reset time. [Claude Code statusline fields](https://code.claude.com/docs/en/statusline#available-data)

Create an opaque UUID once. Use the same UUID for sessions on the same allowance. Use a different UUID for a separate allowance. Add the following to your Claude Code statusline wrapper, with the absolute path to your local AUT executable and your generated UUID.

```sh
#!/bin/sh
aut=/absolute/path/to/aut
scope=your-generated-uuid
"$aut" quota capture --scope "$scope" >/dev/null
"$aut" statusline
```

The first command reads one payload from stdin. It saves only normalized quota fields in the private AUT observation store and updates a small cache. A repeated payload leaves the recorded time unchanged. The second command reads the quota and coverage caches. It does not scan agent logs or contact a provider. Use `aut statusline --json` for reset times, freshness, and the reason an estimate is unavailable.

A reading expires after 30 minutes or at reset, whichever comes first. Forecasts require two fresh readings from the same allowance and reset window. They project the change between those readings at a constant hourly rate. AUT keeps decimal percentage values to two places as basis points. One basis point is 0.01 percentage points.

## Local notices

```sh
aut quota check
aut quota check --notify --quiet-hours 22:00-07:00
aut quota inbox
aut quota mute --scope your-generated-uuid --window five_hour
```

The first command only reads the cache. The second writes notices to a private local inbox when a fresh reading reaches 90% or the observed pace points to reaching the limit before reset. Quiet hours use the configured ledger timezone. Mute lasts until the recorded reset. A recovery requires a fresh reading that shows the warning condition ended.

The statusline shows the latest saved scan and coverage evidence. The existing scan route continues to handle coverage and no-scan paging.

## Pending delivery notices

```sh
aut delivery-notices check
aut delivery-notices check --notify --quiet-hours 22:00-07:00
aut delivery-notices inbox
```

A scan saves an outbox count with its local coverage assessment. The check reports a backlog once. It reports recovery when a newer assessment reads an empty outbox. A missing outbox, an unreadable queue or an assessment older than 12 hours leaves the episode open. Quiet hours follow the ledger timezone. Notices stay in a private inbox on this machine.
