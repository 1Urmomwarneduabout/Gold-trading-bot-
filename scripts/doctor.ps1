<#
.SYNOPSIS
    Checks every goldbot scheduled task, config and running copy, and turns off
    tasks that would start a second copy of a config another task already runs.

.DESCRIPTION
    Run from an ADMINISTRATOR PowerShell (right-click PowerShell > Run as administrator):
        powershell -ExecutionPolicy Bypass -File C:\gbot\scripts\doctor.ps1

    It reads everything first and prints a report, then lists the changes it
    would make and asks before making any. The only changes it ever makes:
      * DISABLES (never deletes) a scheduled task when every config it runs is
        also run by a task it keeps. Undo: Enable-ScheduledTask -TaskName <name>
      * ends the extra running copies of those configs, and
      * starts each kept task that is not running, then checks the bot came up.
        If it did not, it turns the disabled tasks back on and restarts them.
    When two tasks run the same config it keeps the one that runs while you are
    logged in ("Interactive"), the way scripts\install_task.ps1 sets it up: the
    MetaTrader5 package needs the MT5 terminal on your desktop.

    Config clashes (two bots sharing a magic number or a state folder) are only
    reported: fixing those safely needs care while a position is open.
    A copy of everything printed is saved to doctor-report.txt in the repo folder.
#>
[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$RepoDir = (Resolve-Path $RepoDir).Path
$script:warnings = 0

function Section([string]$title) { Write-Host ""; Write-Host "== $title ==" -ForegroundColor Cyan }
function Ok([string]$msg)   { Write-Host "  OK    $msg" -ForegroundColor Green }
function Info([string]$msg) { Write-Host "        $msg" }
function Warn([string]$msg) { Write-Host "  WARN  $msg" -ForegroundColor Yellow; $script:warnings++ }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this from an Administrator PowerShell (right-click PowerShell > Run as administrator)." -ForegroundColor Red
    exit 1
}
try { Start-Transcript -Path (Join-Path $RepoDir "doctor-report.txt") -Force | Out-Null } catch { }
$mySession = (Get-Process -Id $PID).SessionId

function Resolve-Under([string]$base, [string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $null }
    $path = [Environment]::ExpandEnvironmentVariables($path.Trim([char[]]@('"', "'", ' ')))
    try {
        if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $base $path }
        return [IO.Path]::GetFullPath($path)
    } catch { return $null }
}

# Config files named on a command line or in a launcher script ("live run" with no --config means live.toml).
function Get-ConfigNames([string]$text) {
    $names = @()
    foreach ($m in [regex]::Matches($text, '--config[\s=]+(?:"([^"]+)"|''([^'']+)''|(\S+))')) {
        $names += ($m.Groups[1].Value + $m.Groups[2].Value + $m.Groups[3].Value)
    }
    if ($names.Count -eq 0 -and $text -match 'live\s+run') { $names += "live.toml" }
    return $names
}

# A relative config is looked up in the task's folder, then in the repo folder.
function Resolve-Config([string]$name, [string]$workDir) {
    $first = Resolve-Under $workDir $name
    if ($first -and (Test-Path $first)) { return $first }
    $second = Resolve-Under $RepoDir $name
    if ($second -and (Test-Path $second)) { return $second }
    if ($first) { return $first }
    return $second
}

# The [live] keys this check needs, with the bot's defaults (goldbot/live.py LiveConfig).
function Read-LiveConfig([string]$file) {
    $c = @{ symbol = "XAUUSD"; magic = "7861001"; state_dir = "state"; dry_run = "false"; allow_live = "false"; exists = $false }
    if (-not (Test-Path $file)) { return $c }
    $c.exists = $true
    $section = ""
    foreach ($line in Get-Content $file) {
        $l = $line.Trim()
        if ($l -match '^\[\s*([^\]]+?)\s*\]') { $section = $Matches[1]; continue }
        if ($section -ne "live") { continue }
        if ($l -match '^(symbol|magic|state_dir|dry_run|allow_live)\s*=\s*(?:"([^"]*)"|''([^'']*)''|([^#\s]+))') {
            $c[$Matches[1]] = ($Matches[2] + $Matches[3] + $Matches[4])
        }
    }
    return $c
}

function Get-Descendants([int]$parentId, $all) {
    $kids = @($all | Where-Object { $_.ParentProcessId -eq $parentId })
    $out = @()
    foreach ($k in $kids) { $out += $k; $out += Get-Descendants $k.ProcessId $all }
    return $out
}

# ---------------------------------------------------------------- tasks
Section "Scheduled tasks that run goldbot"
$tasks = @()
foreach ($t in Get-ScheduledTask) {
    $text = ""; $configs = @(); $actionsText = @(); $workDir = $null
    foreach ($a in $t.Actions) {
        $exe = [string]$a.Execute; $arg = [string]$a.Arguments; $wd = [string]$a.WorkingDirectory
        if ([string]::IsNullOrWhiteSpace($wd)) { $wd = Join-Path $env:windir "System32" }
        else { $wd = [Environment]::ExpandEnvironmentVariables($wd.Trim('"')) }
        $actionText = "$exe $arg"
        # A task that runs a .bat/.cmd/.ps1: read that script too.
        foreach ($m in [regex]::Matches($actionText, '"([^"]+\.(?:bat|cmd|ps1))"|(\S+\.(?:bat|cmd|ps1))')) {
            $p = Resolve-Under $wd ($m.Groups[1].Value + $m.Groups[2].Value)
            if ($p -and (Test-Path $p)) { $actionText += "`n" + (Get-Content $p -Raw) }
        }
        if ($actionText -match 'goldbot') {
            $text += "`n" + $actionText
            $actionsText += "$exe $arg  (in $wd)"
            if (-not $workDir) { $workDir = $wd }
            foreach ($n in Get-ConfigNames $actionText) { $configs += (Resolve-Config $n $wd) }
        }
    }
    if (-not $text) { continue }
    $logon = [string]$t.Principal.LogonType
    $tasks += [pscustomobject]@{
        Name        = $t.TaskName
        Path        = $t.TaskPath
        State       = [string]$t.State
        Enabled     = ([string]$t.State -ne "Disabled")
        User        = $t.Principal.UserId
        LogonType   = $logon
        Interactive = ($logon -eq "Interactive")
        Configs     = @($configs | Where-Object { $_ } | Sort-Object -Unique)
        Actions     = $actionsText
    }
}
if ($tasks.Count -eq 0) { Warn "No scheduled task runs goldbot. Register one with scripts\install_task.ps1." }
foreach ($t in $tasks) {
    $where = if ($t.Interactive) { "while you are logged in (your desktop)" } else { "in the background (session 0, LogonType $($t.LogonType))" }
    Write-Host ""
    Write-Host "  $($t.Path)$($t.Name)   state $($t.State)   runs $where as $($t.User)"
    foreach ($a in $t.Actions) { Info "action: $a" }
    if ($t.Configs.Count) { Info "configs: $($t.Configs -join ', ')" } else { Info "configs: none (does not run 'goldbot live run')" }
    if ($t.Configs.Count -and ($t.Actions -match [regex]::Escape("(in $env:windir\System32)")) -and -not ($t.Actions -match [regex]::Escape($RepoDir))) {
        Warn "Task '$($t.Name)' has no 'Start in' folder, so the bot may keep its state and log in $env:windir\System32\state instead of $RepoDir\state."
    }
}

# ---------------------------------------------------------------- processes
Section "Running bots"
$allProcs = @(Get-CimInstance Win32_Process)
$launchers = @($allProcs | Where-Object { $_.Name -eq "goldbot.exe" -and $_.CommandLine -match 'live\s+run' } | ForEach-Object {
    $names = @(Get-ConfigNames ([string]$_.CommandLine))
    [pscustomobject]@{ Id = [int]$_.ProcessId; Session = [int]$_.SessionId; Started = $_.CreationDate; Config = (Resolve-Config $names[0] $RepoDir) }
})
if ($launchers.Count -eq 0) { Warn "No goldbot is running right now." }
foreach ($l in $launchers) {
    $where = if ($l.Session -eq $mySession) { "your desktop" } elseif ($l.Session -eq 0) { "background session 0" } else { "session $($l.Session)" }
    Info "pid $($l.Id)  $where  started $($l.Started)  config $($l.Config)"
}
foreach ($g in ($launchers | Group-Object Config | Where-Object { $_.Count -gt 1 })) {
    Warn "$($g.Count) copies of $($g.Name) are running at once (pids $(($g.Group | ForEach-Object { $_.Id }) -join ', '))."
}

# ---------------------------------------------------------------- configs, state, logs
Section "Configs"
$allConfigs = @(@($tasks | ForEach-Object { $_.Configs }) + @($launchers | ForEach-Object { $_.Config }) | Where-Object { $_ } | Sort-Object -Unique)
$cfgInfo = @{}
foreach ($f in $allConfigs) {
    $c = Read-LiveConfig $f
    $c.state_path = Resolve-Under $RepoDir $c.state_dir
    $cfgInfo[$f] = $c
    if (-not $c.exists) { Warn "$f does not exist, so any task that runs it fails at once."; continue }
    $mode = if ($c.dry_run -eq "true") { "DRY RUN (logs orders, sends none)" } else { "sends real orders" }
    Info "$f"
    Info "    symbol $($c.symbol)  magic $($c.magic)  state $($c.state_path)  $mode  allow_live $($c.allow_live)"
}
$existing = @($allConfigs | Where-Object { $cfgInfo[$_].exists })
foreach ($g in ($existing | Group-Object { "$($cfgInfo[$_].symbol)|$($cfgInfo[$_].magic)" } | Where-Object { $_.Count -gt 1 })) {
    Warn "These configs trade the same symbol with the same magic number, so each bot will see the other's position as its own and halt: $($g.Group -join ', '). Give each a different magic."
}
foreach ($g in ($existing | Group-Object { $cfgInfo[$_].state_path } | Where-Object { $_.Count -gt 1 })) {
    Warn "These configs share the state folder $($g.Name) and overwrite each other's state.json and log: $($g.Group -join ', '). Give each its own state_dir."
}
if ($existing.Count -gt 1 -and -not ($existing | Group-Object { "$($cfgInfo[$_].symbol)|$($cfgInfo[$_].magic)" } | Where-Object { $_.Count -gt 1 })) {
    Ok "Each config has its own magic number."
}

Section "Bot state and logs"
foreach ($dir in ($existing | ForEach-Object { $cfgInfo[$_].state_path } | Sort-Object -Unique)) {
    Info "$dir"
    $stateFile = Join-Path $dir "state.json"
    if (Test-Path $stateFile) {
        try {
            $s = Get-Content $stateFile -Raw | ConvertFrom-Json
            if ($s.halted) { Warn "HALTED: $($s.halt_reason). Review, then run: goldbot live reset-halt --config <config>" }
            else { Ok "not halted; last bar acted on $($s.last_bar)" }
            if ($s.position) {
                $side = if ($s.position.side -gt 0) { "long" } else { "short" }
                Info "    position: $side $($s.position.volume) lots from $($s.position.entry_price), stop $($s.position.sl)"
            } else { Info "    position: none" }
        } catch { Warn "state.json could not be read: $_" }
    } else { Info "    no state.json yet" }
    $log = Join-Path $dir "goldbot.log"
    if (Test-Path $log) {
        $age = (Get-Date) - (Get-Item $log).LastWriteTime
        if ($age.TotalHours -gt 26) { Warn "goldbot.log last written $([int]$age.TotalHours) hours ago; the bot writes at least once a day." }
        else { Ok "goldbot.log written $([int]$age.TotalMinutes) minutes ago" }
        $problems = @(Get-Content $log -Tail 500 | Where-Object { $_ -match ' (ERROR|CRITICAL|WARNING) |Traceback|halt' })
        if ($problems.Count) {
            Warn "recent problems in goldbot.log (last $([math]::Min(5, $problems.Count)) of $($problems.Count)):"
            $problems | Select-Object -Last 5 | ForEach-Object { Info "    $_" }
        }
    } else { Info "    no goldbot.log yet" }
}

# ---------------------------------------------------------------- plan
Section "What needs doing"
$enabled = @($tasks | Where-Object { $_.Enabled -and $_.Configs.Count -gt 0 })
$configsRun = @($enabled | ForEach-Object { $_.Configs } | Sort-Object -Unique)
# A task that is the only one running some config has to stay.
$required = @{}
foreach ($cfg in $configsRun) {
    $runners = @($enabled | Where-Object { $_.Configs -contains $cfg })
    if ($runners.Count -eq 1) { $required[$runners[0].Name] = $true }
}
$keep = @{}
$keepFor = @{}
foreach ($cfg in $configsRun) {
    $runners = @($enabled | Where-Object { $_.Configs -contains $cfg })
    $pick = @($runners | Where-Object { $required[$_.Name] -or $keep[$_.Name] } | Select-Object -First 1)
    if (-not $pick) {
        $pick = @($runners | Sort-Object @{ Expression = { [int]$_.Interactive * 2 + [int]($_.State -eq "Running") }; Descending = $true }, Name | Select-Object -First 1)
    }
    $keep[$pick[0].Name] = $true
    $keepFor[$cfg] = $pick[0]
}
$toDisable = @($enabled | Where-Object { -not $keep[$_.Name] })
foreach ($cfg in $configsRun) {
    $keepers = @($enabled | Where-Object { $keep[$_.Name] -and ($_.Configs -contains $cfg) })
    if ($keepers.Count -gt 1) {
        Warn "$cfg is run by $($keepers.Count) tasks that each also run something no other task does ($(($keepers | ForEach-Object { $_.Name }) -join ', ')). Fix by hand: edit one so it no longer runs $cfg."
    }
}
foreach ($t in @($tasks | Where-Object { $_.Enabled -and $_.Configs.Count -eq 0 })) {
    Warn "Task '$($t.Name)' mentions goldbot but does not run 'goldbot live run'. Left alone; check what it does."
}

# Running copies that the kept task does not own: those in the other kind of session.
$toKill = @()
foreach ($cfg in $configsRun) {
    $keeper = $keepFor[$cfg]
    $keepSession = if ($keeper.Interactive) { $mySession } else { 0 }
    $toKill += @($launchers | Where-Object { $_.Config -eq $cfg -and $_.Session -ne $keepSession })
}
$toStart = @($enabled | Where-Object { $keep[$_.Name] -and $_.State -ne "Running" })

if ($toDisable.Count -eq 0 -and $toKill.Count -eq 0 -and $toStart.Count -eq 0) {
    Ok "No duplicate tasks or extra copies to remove."
    if ($script:warnings) { Write-Host "`n$($script:warnings) warning(s) above need a look; nothing was changed." -ForegroundColor Yellow }
    else { Write-Host "`nAll good. Nothing to change." -ForegroundColor Green }
    try { Stop-Transcript | Out-Null } catch { }
    exit 0
}
foreach ($cfg in $configsRun) { Info "keep task '$($keepFor[$cfg].Name)' for $cfg" }
foreach ($t in $toDisable) { Write-Host "  DISABLE task '$($t.Name)' (it duplicates $($t.Configs -join ', '))" -ForegroundColor Yellow }
foreach ($l in $toKill)    { Write-Host "  END     extra running copy pid $($l.Id) of $($l.Config)" -ForegroundColor Yellow }
foreach ($t in $toStart)   { Write-Host "  START   task '$($t.Name)'" -ForegroundColor Yellow }

$now = Get-Date
if (($now.Hour -eq 17 -and $now.Minute -ge 45) -or ($now.Hour -eq 18 -and $now.Minute -le 20)) {
    Warn "It is close to 18:00, when the bot makes its daily decision. Safer to run this again in half an hour."
}
if (-not $Yes) {
    $answer = Read-Host "`nMake these changes? (y/n)"
    if ($answer -notmatch '^(y|yes)$') { Write-Host "Nothing changed."; try { Stop-Transcript | Out-Null } catch { }; exit 0 }
}

# ---------------------------------------------------------------- apply
Section "Applying"
$changedAt = Get-Date
foreach ($t in $toDisable) {
    Stop-ScheduledTask -TaskName $t.Name -TaskPath $t.Path -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $t.Name -TaskPath $t.Path | Out-Null
    Ok "disabled '$($t.Name)'  (undo: Enable-ScheduledTask -TaskName '$($t.Name)')"
}
$allProcs = @(Get-CimInstance Win32_Process)
foreach ($l in $toKill) {
    $tree = @(Get-Descendants $l.Id $allProcs) + @($allProcs | Where-Object { $_.ProcessId -eq $l.Id })
    foreach ($p in $tree) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Ok "ended pid $($l.Id) and its python processes"
}
foreach ($t in $toStart) {
    Start-ScheduledTask -TaskName $t.Name -TaskPath $t.Path
    Ok "started '$($t.Name)'"
}

# ---------------------------------------------------------------- verify
Write-Host "`nWaiting 60 seconds for the bots to connect to MT5..."
Start-Sleep -Seconds 60
Section "Check"
$failed = $false
$now = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "goldbot.exe" -and $_.CommandLine -match 'live\s+run' })
foreach ($cfg in $configsRun) {
    $copies = @($now | Where-Object { (Resolve-Config (@(Get-ConfigNames ([string]$_.CommandLine)))[0] $RepoDir) -eq $cfg })
    $log = Join-Path $cfgInfo[$cfg].state_path "goldbot.log"
    $fresh = (Test-Path $log) -and ((Get-Item $log).LastWriteTime -gt $changedAt)
    if ($copies.Count -eq 1 -and ($fresh -or -not ($toStart | Where-Object { $_.Configs -contains $cfg }))) {
        Ok "$cfg : one copy running (pid $($copies[0].ProcessId), session $($copies[0].SessionId))"
    } elseif ($copies.Count -gt 1) {
        Warn "$cfg : still $($copies.Count) copies running"
    } else {
        Warn "$cfg : the bot did not come up ($($copies.Count) running, log updated: $fresh)"
        $failed = $true
    }
    if (Test-Path $log) { Get-Content $log -Tail 3 | ForEach-Object { Info "    $_" } }
}

if ($failed -and $toDisable.Count) {
    Write-Host "`nRolling back: turning the disabled tasks back on so the bots keep running." -ForegroundColor Red
    foreach ($t in $toDisable) {
        Enable-ScheduledTask -TaskName $t.Name -TaskPath $t.Path | Out-Null
        Start-ScheduledTask -TaskName $t.Name -TaskPath $t.Path -ErrorAction SilentlyContinue
        Info "re-enabled and started '$($t.Name)'"
    }
    Write-Host "Send doctor-report.txt to Claude to work out why." -ForegroundColor Red
} elseif (-not $failed) {
    Write-Host "`nDone. One copy of each bot is running." -ForegroundColor Green
    if ($keep.Keys | Where-Object { ($tasks | Where-Object Name -eq $_).Interactive }) {
        Write-Host "Interactive tasks run only while you are logged in: lock the screen, don't sign out, and keep MT5 open."
    }
}
Write-Host "Report saved to $(Join-Path $RepoDir 'doctor-report.txt')"
try { Stop-Transcript | Out-Null } catch { }
