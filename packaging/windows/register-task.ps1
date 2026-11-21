# Register a Windows Scheduled Task that runs aut scan every 15 minutes.
# Run in PowerShell after installing aut and configuring a sink.
param(
    [ValidateSet("install", "uninstall")][string]$Mode = "install",
    [string]$AutExecutable,
    [switch]$Managed
)

$ErrorActionPreference = "Stop"
$name = "agent-usage-tracker"
$marker = "Managed by aut setup"
$description = "Collect coding-agent token usage"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$existing = Get-ScheduledTask -TaskPath "\" | Where-Object { $_.TaskName -eq $name }

if ($existing) {
    $sameUser = $existing.Principal.UserId -in @($identity.Name, $identity.User.Value)
    if ($existing.Description -ne $marker -or -not $sameUser) {
        throw "Existing scheduled task '$name' was kept."
    }
    if ($Mode -eq "uninstall") {
        if (-not $Managed) { throw "Use aut setup --uninstall to remove a managed task." }
        Unregister-ScheduledTask -InputObject $existing -Confirm:$false
        Write-Output "removed"
    } else {
        Write-Output "already_installed"
    }
    exit
}
if ($Mode -eq "uninstall") {
    Write-Output "absent"
    exit
}
if (-not $AutExecutable) {
    $AutExecutable = (Get-Command aut -ErrorAction SilentlyContinue).Source
}
if (-not $AutExecutable) { throw "aut not found on PATH." }
if ($Managed) { $description = $marker }

$action = New-ScheduledTaskAction -Execute $AutExecutable -Argument "scan"
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 1)
# Task Scheduler repeats indefinitely when Duration is absent.
$trigger.Repetition.Duration = $null
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -TaskPath "\" -Action $action -Trigger $trigger `
    -Settings $settings -Description $description -Principal $principal | Out-Null
Write-Output "installed"
