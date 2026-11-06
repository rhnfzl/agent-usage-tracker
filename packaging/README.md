# Running `aut` on a timer

Install once per machine, then wire the per-OS timer. `aut scan` is idempotent and
offline-safe, so a missed run just catches up next time.

## macOS (launchd)

```bash
uv tool install .                       # or from git
mkdir -p ~/.config/agent-usage-tracker && cp config.example.toml ~/.config/agent-usage-tracker/config.toml
$EDITOR ~/.config/agent-usage-tracker/config.toml     # set machine + webhook_url + webhook_secret
chmod 600 ~/.config/agent-usage-tracker/config.toml

cp packaging/launchd/dev.aut.scan.plist ~/Library/LaunchAgents/
sed -i '' "s#/Users/CHANGEME/.local/bin/aut#$(which aut)#" ~/Library/LaunchAgents/dev.aut.scan.plist
launchctl load ~/Library/LaunchAgents/dev.aut.scan.plist
```

launchd jobs do not inherit your shell environment, so put the webhook secret **directly**
in the local config file (not as `env:`).

The scan job runs at login and on wake as well as on its interval, so a laptop that comes back
after a week catches up at once. An older copy without `StartCalendarInterval` and `RunAtLoad`
waits out its interval: copy the plist again and reload it.

### Daily backup (optional)

`dev.aut.backup.plist` runs `aut backup` daily at 02:30 and at login. Set up the `[backup]` table
and the repository first; [docs/new-laptop.md](../docs/new-laptop.md) has every step, and the
restore onto a new laptop.

## Linux (systemd user timer)

```bash
uv tool install .
mkdir -p ~/.config/agent-usage-tracker && cp config.example.toml ~/.config/agent-usage-tracker/config.toml
chmod 600 ~/.config/agent-usage-tracker/config.toml   # it can hold the webhook secret / DSN
$EDITOR ~/.config/agent-usage-tracker/config.toml

mkdir -p ~/.config/systemd/user
cp packaging/systemd/agent-usage-tracker.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-usage-tracker.timer
loginctl enable-linger "$USER"          # so the timer runs when you are not logged in
```

Put the secret in the config file, or use `env:AUT_WEBHOOK_SECRET` with a systemd
`Environment=` / drop-in if you prefer.

## Windows (Task Scheduler)

```powershell
uv tool install .
mkdir $HOME\.config\agent-usage-tracker -Force
copy config.example.toml $HOME\.config\agent-usage-tracker\config.toml
notepad $HOME\.config\agent-usage-tracker\config.toml   # set machine + webhook_url + webhook_secret

# register the 15-minute task
powershell -ExecutionPolicy Bypass -File packaging\windows\register-task.ps1
```

Claude Code, Codex, and Cursor all work on Windows: their logs live under `%USERPROFILE%`
(`~/.claude`, `~/.codex`) and `%APPDATA%\Cursor\...`, which the collector locates
automatically. The config lives at `C:\Users\<you>\.config\agent-usage-tracker\config.toml`.

## Retrying as soon as the sink comes back (optional)

A scan that fires while the sink is unreachable — laptop off the VPN, tunnel down — is safe,
but it then waits out the whole interval before trying again. On the stock 15-minute timer
that is fine. If you run a long interval, or push through a VPN/tunnel, you can have the
missed scan land the moment the network returns instead.

`aut scan` already exits non-zero on a delivery failure, so both platforms key off that.

**Linux** needs nothing extra: `agent-usage-tracker.service` ships with `Restart=on-failure`
and `RestartSec=120`.

**macOS** has no equivalent, so a marker file stands in for one:

```bash
cp packaging/launchd/aut-scan.sh ~/.config/agent-usage-tracker/
chmod +x ~/.config/agent-usage-tracker/aut-scan.sh

# point the existing scan job at the wrapper, so a failed push arms the marker
/usr/libexec/PlistBuddy \
  -c "Delete :ProgramArguments" \
  -c "Add :ProgramArguments array" \
  -c "Add :ProgramArguments:0 string /bin/bash" \
  -c "Add :ProgramArguments:1 string $HOME/.config/agent-usage-tracker/aut-scan.sh" \
  ~/Library/LaunchAgents/dev.aut.scan.plist

cp packaging/launchd/dev.aut.retry.plist ~/Library/LaunchAgents/
sed -i '' "s#/Users/CHANGEME#$HOME#g" ~/Library/LaunchAgents/dev.aut.retry.plist
launchctl unload ~/Library/LaunchAgents/dev.aut.scan.plist
launchctl load  ~/Library/LaunchAgents/dev.aut.scan.plist
launchctl load  ~/Library/LaunchAgents/dev.aut.retry.plist
```

The retry job uses `KeepAlive`/`PathState`, so launchd runs it *only* while the marker exists
and stops the moment a scan succeeds. A healthy machine never wakes it.

### Per-machine settings: `scan.env`

`aut-scan.sh` is meant to be byte-identical everywhere; anything machine-specific goes in
`~/.config/agent-usage-tracker/scan.env`, which it sources if present. Timer jobs do not
inherit your shell environment, which is why this file exists rather than an `export` in your
profile. **Keep secrets out of it** — those belong in `config.toml`, which is already `chmod 600`.

```bash
cat > ~/.config/agent-usage-tracker/scan.env <<'EOF'
AUT_BIN=/opt/homebrew/bin/aut          # default: ~/.local/bin/aut
EOF
```

### Sinks behind SSH

If the sink is not directly reachable — a homelab Postgres on Tailscale, say — the wrapper can
open the tunnel first. Set both variables; leaving `AUT_TUNNEL_HOST` unset means no tunnel:

```bash
cat >> ~/.config/agent-usage-tracker/scan.env <<'EOF'
AUT_TUNNEL_HOST=user@tailscale-host
AUT_TUNNEL_SPEC=15432:192.168.1.3:5432    # the ssh -L argument
EOF
```

Then point `transport.pg_dsn` at `localhost:15432`. The wrapper reuses a tunnel that is already
up and only tears down one it opened itself, so it is safe to run alongside an interactive
session. An unreachable host arms the retry marker rather than counting as a scan failure —
which is exactly the case this pairs with, since a laptop off the VPN cannot reach the host at all.

### Commit outcomes once a day

After a delivered scan the wrapper tries `aut outcomes` once per calendar day (marker
`outcomes-day` in the config dir, written before the attempt, so a failing run waits for the next
day and shows up as an error heartbeat), as its own step with its own heartbeat row (collector
`outcomes`). It reads the clones under `outcome_repo_roots` (default `["~/projects"]`); set
`outcome_fetch = true` in `config.toml` to `git fetch` them (30 s each, failures skipped) and ask
`gh` about merged pull requests first. Turn it on for the machine that runs this: without a
fetch, merged commits wait while the local default branch is older than merge + 14 days, and a
commit whose GitHub PR was squash-merged ends `abandoned`. The first run computes line survival for every merged
commit older than 14 days, which can take a while; later runs only score new commits.

## Claude Code `/outcome` command (optional)

`packaging/claude/commands/outcome.md` runs `aut outcome <shipped|ruled-out|open> --session
<this session's id>`, which marks the session the command was typed in. Install it for your user:

```bash
mkdir -p ~/.claude/commands
cp packaging/claude/commands/outcome.md ~/.claude/commands/
```

Then type `/outcome shipped` (or `ruled-out`, `open`) in a session. It needs `aut` on `PATH`
and `transport.pg_dsn`.

## First run

Do a one-time backfill of history (optional; on a heavy machine this can be a lot of
rows), then let the timer take over:

```bash
aut scan --dry-run     # sanity-check the numbers first
aut backfill           # push all history (or skip and just let `scan` accumulate forward)
```
