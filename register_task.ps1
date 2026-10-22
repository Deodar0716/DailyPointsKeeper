<#
Register (or remove) the one Windows scheduled task that runs DailyPointsKeeper.

ASCII only on purpose: PowerShell 5.1 mis-decodes a non-BOM UTF-8 .ps1 that
contains non-ASCII text and can silently swallow whole lines.

Two hard-won details encoded here:

1. Parsing slot times. Under a Chinese locale `[datetime]"2026-01-01 10:05"`
   drops the time component and five triggers collapse into one that fires at
   midnight. Use [datetime]::ParseExact with the invariant culture.
2. Do NOT rely on RestartCount/RestartInterval for retries: Task Scheduler only
   restarts a task that failed to *start* or was killed - a script that finishes
   with exit code 2/3/4 is "completed" and never retried. Retries come from
   having several slots per day (the scripts re-ask the server each time, so an
   extra slot cannot double-claim).

LogonType stays Interactive: two providers read credentials that only exist for
the logged-in Windows user (DPAPI master key / user profile files), so "run
whether user is logged on or not" would fail. The screen may be LOCKED - that
still works, which is the whole reason this is a script and not UI automation.

Examples:
    powershell -NoProfile -ExecutionPolicy Bypass -File register_task.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File register_task.ps1 `
        -Slots "09:55,12:30,18:00" -TaskName DailyPointsKeeper
    powershell -NoProfile -ExecutionPolicy Bypass -File register_task.ps1 -RemoveOnly
#>

[CmdletBinding()]
param(
    [string]$TaskName = "DailyPointsKeeper",
    [string[]]$Slots = @("10:05", "12:05", "15:05", "18:05", "21:30"),
    [string]$PythonExe = "",
    [string]$EntryScript = "",
    [int]$TimeLimitMinutes = 10,
    [switch]$RemoveOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($RemoveOnly) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host ("removed task: {0}" -f $TaskName)
    } else {
        Write-Host ("no task named {0}" -f $TaskName)
    }
    exit 0
}

# ---- interpreter ------------------------------------------------------------
if (-not $PythonExe) {
    # pythonw.exe = no console window. Prefer a real per-user CPython over PATH:
    # PATH often points at some other tool's virtualenv, and when that tool is
    # upgraded or removed the task silently loses its interpreter (and its
    # installed packages). Cheap probes only - never recurse a whole profile.
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $pyRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
        if (Test-Path $pyRoot) {
            $candidates += (Get-ChildItem -Path $pyRoot -Directory -ErrorAction SilentlyContinue |
                            Sort-Object Name -Descending |
                            ForEach-Object { Join-Path $_.FullName "pythonw.exe" })
        }
    }
    $candidates += ((Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source)
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $PythonExe = $c; break } }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "pythonw.exe not found - pass -PythonExe C:\path\to\pythonw.exe"
}
Write-Host ("interpreter : {0}" -f $PythonExe)

# Warn loudly if the chosen interpreter is a virtualenv: it belongs to another
# program's lifecycle, and `cryptography` (needed by qoder_cn) may not be there.
$envMarker = Join-Path (Split-Path -Parent $PythonExe) "pyvenv.cfg"
if (Test-Path $envMarker) {
    Write-Host ""
    Write-Host "  WARNING: this pythonw.exe is inside a virtual environment"
    Write-Host "           ($PythonExe)"
    Write-Host "           It works only as long as the owning tool keeps it"
    Write-Host "           alive. Install Python for the user and re-run with"
    Write-Host "           -PythonExe <real pythonw.exe> for a durable task."
    Write-Host ""
}

# ---- entry script -----------------------------------------------------------
if (-not $EntryScript) { $EntryScript = Join-Path $root "daily_points_keeper.py" }
if (-not (Test-Path $EntryScript)) { throw "entry script not found: $EntryScript" }
Write-Host ("entry       : {0}" -f $EntryScript)

# ---- triggers ---------------------------------------------------------------
$culture = [System.Globalization.CultureInfo]::InvariantCulture
$triggers = @($Slots | ForEach-Object {
    $slot = $_
    $dt = [datetime]::ParseExact("2026-01-01 $slot", "yyyy-MM-dd HH:mm", $culture)
    Write-Host ("  parsed    : {0} -> {1}" -f $slot, $dt.ToString("HH:mm"))
    New-ScheduledTaskTrigger -Daily -At $dt
})

$action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "-X utf8 `"$EntryScript`" claim" -WorkingDirectory $root

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force `
    -Description "Claim the free daily rewards of desktop AI clients (DailyPointsKeeper). Pure HTTP, no GUI, no model." |
    Out-Null

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host ("triggers    : {0}  state={1}  next={2}" -f $task.Triggers.Count, $task.State, $info.NextRunTime)
Write-Host ("slots       : " + ($Slots -join ", "))
Write-Host ("boundaries  : " + (($task.Triggers | ForEach-Object { $_.StartBoundary }) -join " "))
Write-Host ("startWhenAv : " + $task.Settings.StartWhenAvailable + "  logon=" + $task.Principal.LogonType)
Write-Host ""
Write-Host "Check it with:"
Write-Host ("  schtasks /Run /TN {0}" -f $TaskName)
Write-Host ('  powershell -NoProfile -Command "(Get-ScheduledTask -TaskName {0} | Get-ScheduledTaskInfo).LastTaskResult"' -f $TaskName)
Write-Host "Exit codes: 0 ok / 2 credential / 3 api / 4 nothing claimable today"
