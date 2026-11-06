#Requires -Version 5.1
<#
.SYNOPSIS
    Drives the full-shutdown hang (IBSurgeon/hqbird issue 12) on the stand
    that hang-prepare.ps1 built. Keep this script small: all setup is done by
    hang-prepare.ps1; this one only repeats the shutdown cycle and watches
    for the deadlock.

.DESCRIPTION
    One cycle: start the load, start a NEW user trace session, full-shut the
    database, stop the load, then probe with a fresh attach. In the deadlock
    that probe never returns; the script then saves thread stacks and a dump
    (cdb, read-only) and stops with exit code 2. Exit 0 means no hang.

    Set $env:ISC_PASSWORD (the same password hang-prepare.ps1 used); the load
    and trace processes inherit it.

.PARAMETER WorkDir     The -WorkDir of hang-prepare.ps1 (reads run\stand.psd1).
.PARAMETER Iterations  Shutdown cycles to run. The hang is usually in cycle 1-2.
.PARAMETER OnlineSec   Seconds of load before each shutdown.
.PARAMETER BurstSec    Seconds of rejected load right after the shutdown.
.PARAMETER ShutSec     Seconds of quiet before the probe (the cycle forms here).
.PARAMETER ShutMode    'full' is the bug; 'multi' is a control run (no hang).
.PARAMETER Trace       '1' a new session each cycle (bug); 'once' one session; '0' none.
.PARAMETER TimeoutSec  Seconds to wait for a tool; longer counts as a hang.
.PARAMETER CdbExe      cdb.exe for read-only stacks+dump on a hang (optional).

.EXAMPLE
    $env:ISC_PASSWORD = '<SYSDBA password>'
    .\hang-repro.ps1 -WorkDir C:\fbrepro -Iterations 40
#>

param(
    [string] $WorkDir    = 'C:\fbrepro',
    [int]    $Iterations = 40,
    [int]    $OnlineSec  = 3,
    [int]    $BurstSec   = 1,
    [int]    $ShutSec    = 2,
    [ValidateSet('full', 'multi')] [string] $ShutMode = 'full',
    [ValidateSet('1', 'once', '0')] [string] $Trace   = '1',
    [int]    $TimeoutSec = 30,
    [string] $CdbExe     = 'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe'
)

$ErrorActionPreference = 'Stop'

# Load the stand description written by hang-prepare.ps1.
$StandPsd = Join-Path $WorkDir 'run\stand.psd1'
if (-not (Test-Path $StandPsd)) { throw "no stand: run hang-prepare.ps1 -WorkDir $WorkDir first" }
if (-not $env:ISC_PASSWORD) { throw 'set $env:ISC_PASSWORD' }
$S = Import-PowerShellDataFile $StandPsd
$env:ISC_USER = $S.User            # load/trace children inherit user+password
$env:FIREBIRD = $S.Fb
$env:FIREBIRD_LOCK = Join-Path $S.Run 'lock'
$env:FIREBIRD_TMP  = Join-Path $S.Run 'tmp'

$svc = "localhost/$($S.Port):service_mgr"   # service manager, for fbtracemgr
$Iter = 0                                   # current cycle, for the log
$Load = @()                                 # the running load processes
$TraceProcs = @{}                           # name -> background fbtracemgr process
$HangDir = $null

function Write-Log { param($m) Write-Host ('{0} it={1} {2}' -f (Get-Date -Format 'HH:mm:ss'), $Iter, $m) }

# Run one Firebird tool with a time limit. A timeout is the deadlock -> save
# evidence and exit. Any other non-zero exit stops the script (unless
# -AllowFail). Uses System.Diagnostics.Process so the exit code is reliable
# (Start-Process -PassThru leaves ExitCode empty on Windows PowerShell 5.1).
function Invoke-FbTool {
    param([string] $Exe, [string[]] $FbArgs, [string] $What, [switch] $AllowFail)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    # Our arguments never contain spaces, so quoting each one is enough.
    $psi.Arguments = ($FbArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $psi.UseShellExecute = $false        # inherit our environment (ISC_USER, ISC_PASSWORD, FIREBIRD)
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $outText = $p.StandardOutput.ReadToEndAsync()   # drain the pipes so the tool never blocks
    $errText = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) { try { $p.Kill() } catch { }; Save-Hang $What }
    Write-Log "  $What rc=$($p.ExitCode)"
    if ($p.ExitCode -ne 0 -and -not $AllowFail) {
        Write-Log ('  output: ' + (($outText.Result + ' ' + $errText.Result) -replace '\s+', ' ').Trim())
        if (Get-Process -Id $S.ServerPid -ErrorAction SilentlyContinue) { Write-Log '  the server process still runs' }
        else { Write-Log '  the server process is GONE (it crashed)' }
        Write-Log 'FAILED'; exit 3
    }
}

# Start the load: Writers processes that update rows (make garbage) and
# Connects processes that attach again and again (the flood). They run the
# .sql files hang-prepare.ps1 wrote and are killed by Stop-Load.
function Start-Load {
    $script:Load = @()
    for ($w = 1; $w -le $S.Writers; $w++) {
        $script:Load += Start-Process $S.Isql -ArgumentList @('-q', '-i', "$($S.Run)\writer$w.sql", $S.Db) -WindowStyle Hidden -PassThru
    }
    for ($c = 1; $c -le $S.Connects; $c++) {
        $script:Load += Start-Process $S.Isql -ArgumentList @('-q', '-i', "$($S.Run)\connect.sql") -WindowStyle Hidden -PassThru
    }
}

# Stop the load now, so the shutdown window is really without load (the cycle
# forms only when the last attachment leaves while a rejected one holds the
# mapping mutex). Kills the load processes and any stray isql of this stand.
function Stop-Load {
    foreach ($p in $script:Load) { try { if (-not $p.HasExited) { $p.Kill() } } catch { } }
    Get-Process -Name isql -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $S.Isql } | Stop-Process -Force -ErrorAction SilentlyContinue
    $script:Load = @()
}

# Start a user trace session over TCP as SYSDBA (so it has an auth block and
# is NOT an admin session -- the kind that triggers the bug). fbtracemgr
# stays in the background streaming; we keep its process to stop it later.
function Start-Trace {
    param([string] $Name)
    $TraceProcs[$Name] = Start-Process $S.Fbtracemgr -ArgumentList @('-se', $svc, '-start', '-name', $Name, '-config', "$($S.Run)\trace.conf") `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput "$($S.Run)\trace.out" -RedirectStandardError "$($S.Run)\trace.err"
    Start-Sleep -Seconds 1
}

# Stop a trace session by its id, and kill its background fbtracemgr.
function Stop-Trace {
    param([string] $Name)
    $list = & $S.Fbtracemgr -se $svc -list 2>&1 | Out-String
    $id = $null; $last = $null
    foreach ($ln in $list -split "`r?`n") {
        if ($ln -match 'Session ID:\s*(\d+)') { $last = $Matches[1] }
        if ($ln -match ('name:\s*' + [regex]::Escape($Name) + '\s*$')) { $id = $last }
    }
    if ($id) { & $S.Fbtracemgr -se $svc -stop -id $id 2>&1 | Out-Null }
    $p = $TraceProcs[$Name]; if ($p -and -not $p.HasExited) { try { $p.Kill() } catch { } }
    $TraceProcs.Remove($Name)
}

# A hang was found: save version info, read-only thread stacks + a full dump
# (cdb -pv does not stop or change the server), and firebird.log. Then exit 2.
function Save-Hang {
    param([string] $What)
    $script:HangDir = Join-Path $WorkDir ('hang-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force -Path $script:HangDir | Out-Null
    Write-Log "HANG in cycle ${Iter}: '$What' got no answer in $TimeoutSec s; saving $script:HangDir"
    Get-Item $S.Firebird, (Join-Path $S.Fb 'plugins\Engine13.dll') -ErrorAction SilentlyContinue |
        ForEach-Object { '{0} {1} {2} bytes' -f $_.Name, $_.VersionInfo.FileVersion, $_.Length } |
        Set-Content (Join-Path $script:HangDir 'version.txt') -Encoding UTF8
    if ((Test-Path $CdbExe) -and (Get-Process -Id $S.ServerPid -ErrorAction SilentlyContinue)) {
        $cmd = ".dump /ma $($script:HangDir)\firebird.dmp; ~*kb 200; q"
        Start-Process $CdbExe -ArgumentList @('-pv', '-p', $S.ServerPid, '-c', $cmd) -NoNewWindow -Wait `
            -RedirectStandardOutput "$($script:HangDir)\stacks.txt" -RedirectStandardError "$($script:HangDir)\cdb.err"
        Write-Log '  stacks and a dump saved'
    } else { Write-Log '  cdb or server not found; no stacks' }
    Copy-Item (Join-Path $S.Fb 'firebird.log') $script:HangDir -ErrorAction SilentlyContinue
    exit 2
}

# ---- the shutdown cycles ---------------------------------------------------
try {
    Write-Log "stand pid $($S.ServerPid), port $($S.Port); $Iterations cycles, ShutMode=$ShutMode Trace=$Trace"
    if ($Trace -eq 'once') { Start-Trace 'repro' }

    for ($Iter = 1; $Iter -le $Iterations; $Iter++) {
        Start-Load
        Start-Sleep -Seconds $OnlineSec
        if ($Trace -eq '1') {
            Start-Trace "repro-$Iter"
            if ($Iter -gt 1) { Stop-Trace "repro-$($Iter - 1)" }   # one new session each cycle
        }
        Invoke-FbTool $S.Isql @('-q', '-i', "$($S.Run)\gc.sql", $S.Db) 'gc check'
        Invoke-FbTool $S.Gfix @('-shut', $ShutMode, '-force', '0', $S.Db) "shut $ShutMode"
        Start-Sleep -Seconds $BurstSec
        Stop-Load
        Start-Sleep -Seconds $ShutSec
        Invoke-FbTool $S.Isql @('-q', '-i', "$($S.Run)\one.sql", $S.ProbeDb) 'probe attach'   # the detector
        Invoke-FbTool $S.Gfix @('-online', 'normal', $S.Db) 'online normal' -AllowFail
    }
    $Iter = $Iterations   # the loop left it one higher
    Write-Log "END: $Iterations of $Iterations cycles, no hang"
}
finally {
    # Always clean up the load and trace helpers (the server stays up for a
    # look, or for another run; stop it with hang-prepare.ps1 -Teardown).
    Stop-Load
    foreach ($p in $TraceProcs.Values) { try { if (-not $p.HasExited) { $p.Kill() } } catch { } }
    if ($HangDir) { Write-Host "hang evidence: $HangDir" -ForegroundColor Yellow }
}
