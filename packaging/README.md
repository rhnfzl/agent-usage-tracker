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

## First run

Do a one-time backfill of history (optional; on a heavy machine this can be a lot of
rows), then let the timer take over:

```bash
aut scan --dry-run     # sanity-check the numbers first
aut backfill           # push all history (or skip and just let `scan` accumulate forward)
```
