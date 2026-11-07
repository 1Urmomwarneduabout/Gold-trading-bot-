<#
.SYNOPSIS
    Registers (or removes) the Windows scheduled task that keeps `goldbot live run` running.

.DESCRIPTION
    Run from PowerShell in the repository folder, AFTER the bot is installed:
        powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
    Remove it again with:
        powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Uninstall

    The task runs as YOU, only while you are logged in ("Interactive"), because
    the MetaTrader5 Python package talks to the MT5 terminal on your desktop.
    A task that runs as SYSTEM or "whether the user is logged on or not" starts
    in a different session, cannot see the terminal and fails to connect.
    No administrator rights are needed for a task that runs as yourself.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "GoldBot",
    [string]$RepoDir  = (Split-Path -Parent $PSScriptRoot),
    [string]$Config   = "live.toml",
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

$RepoDir = (Resolve-Path $RepoDir).Path
$exe     = Join-Path $RepoDir ".venv\Scripts\goldbot.exe"
$cfgPath = Join-Path $RepoDir $Config

# Check everything the task needs, so it doesn't register fine and then fail silently.
$problems = @()
if (-not (Test-Path $exe)) {
    $problems += "Not found: $exe`n    Create the venv and install the bot first (in $RepoDir):`n      py -m venv .venv`n      .venv\Scripts\activate`n      pip install -e "".[mt5,plot]"""
}
if (-not (Test-Path $cfgPath)) {
    $problems += "Not found: $cfgPath`n    copy live.example.toml $Config   (then edit symbol etc.)"
}
if ($problems) {
    $problems | ForEach-Object { Write-Host "ERROR: $_" -ForegroundColor Red }
    exit 1
}

$arguments = "live run --config `"$Config`""
if ($DryRun) { $arguments += " --dry-run" }

$user      = "$env:USERDOMAIN\$env:USERNAME"
$action    = New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory $RepoDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
# Give the MT5 terminal (started from shell:startup) a minute to log in first.
$trigger.Delay = "PT1M"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "goldbot: MT5 gold trend-following bot ($RepoDir)" | Out-Null

Write-Host "Registered scheduled task '$TaskName':"
Write-Host "  runs   $exe $arguments"
Write-Host "  in     $RepoDir"
Write-Host "  when   at log on of $user (+1 min), restarts every minute if it fails"
Write-Host ""
Write-Host "Start it now:   schtasks /run /tn `"$TaskName`""
Write-Host "Check status:   schtasks /query /tn `"$TaskName`" /v /fo list"
Write-Host "Bot log:        $RepoDir\state\goldbot.log"
