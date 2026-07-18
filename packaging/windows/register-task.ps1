# Register a Windows Scheduled Task that runs `aut scan` every 15 minutes.
# Run once in PowerShell after `uv tool install` and creating your config at
#   C:\Users\<you>\.config\agent-usage-tracker\config.toml
# Offline runs are safe; the watermark only advances after a successful push.

$aut = (Get-Command aut -ErrorAction SilentlyContinue).Source
if (-not $aut) { throw "aut not found on PATH. Install with: uv tool install agent-usage-tracker" }

$action   = New-ScheduledTaskAction -Execute $aut -Argument "scan"
$trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date) `
             -RepetitionInterval (New-TimeSpan -Minutes 15) `
             -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "agent-usage-tracker" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Collect coding-agent token usage" -Force

Write-Host "Registered scheduled task 'agent-usage-tracker' (every 15 min)."
Write-Host "Verify a run with:  aut scan --dry-run"
