#Requires -Version 5.1
<#
.SYNOPSIS
    Optional helper: attaches the cdb debugger to the private server of a
    stand (built by hang-prepare.ps1), so that a CRASH of the server leaves a
    full dump and the stacks of all threads.

.DESCRIPTION
    Run it after hang-prepare.ps1 and before hang-repro.ps1. It returns at
    once; cdb stays attached in the background and the server runs on.

    What cdb does (the commands are in WorkDir\crash.cdb):
      * sxi eh -- C++ exceptions are ignored. Firebird uses them for normal
        errors (for example "database shutdown"), so they are not a crash.
      * on an access violation that the server does not handle (a real crash),
        cdb writes WorkDir\crash.dmp (full memory), prints the stacks of all
        threads to WorkDir\cdb-crash.txt, and quits. The server dies then.
      * g -- let the server run.

    Without a crash nothing is written; hang-prepare.ps1 -Teardown stops cdb
    and with it the server.
    Note: a hang is detected by hang-repro.ps1 itself (it uses cdb -pv, which
    works also while this debugger is attached).

.PARAMETER WorkDir  The -WorkDir of hang-prepare.ps1 (reads run\stand.psd1).
.PARAMETER Cdb      cdb.exe from the Windows SDK "Debugging Tools for Windows".

.EXAMPLE
    .\catch-crash.ps1 -WorkDir C:\fbrepro
#>

param(
    [string] $WorkDir = 'C:\fbrepro',
    [string] $Cdb     = 'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe'
)

$ErrorActionPreference = 'Stop'
$S = Import-PowerShellDataFile (Join-Path $WorkDir 'run\stand.psd1')

# Inside a quoted cdb command a backslash is an escape character, so every
# backslash of the dump path is doubled.
$dump = (Join-Path $WorkDir 'crash.dmp') -replace '\\', '\\'
$script = Join-Path $WorkDir 'crash.cdb'
Set-Content -Path $script -Encoding ASCII -Value @(
    'sxi eh',
    "sxd -c2 `".dump /ma $dump; ~*kb 100; q`" av",
    'g'
)

Start-Process $Cdb -ArgumentList @('-p', $S.ServerPid, '-cf', $script) -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $WorkDir 'cdb-crash.txt') -RedirectStandardError (Join-Path $WorkDir 'cdb-crash.err')
Start-Sleep -Seconds 3   # give cdb time to attach before the load starts
Write-Host "cdb attached to the server, pid $($S.ServerPid); a crash goes to $WorkDir\crash.dmp"
