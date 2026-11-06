#Requires -Version 5.1
<#
.SYNOPSIS
    Prepares a private Firebird 5.0 stand for the full-shutdown hang repro
    (IBSurgeon/hqbird issue 12) on Windows. Run this once, then run
    hang-repro.ps1 against the stand it leaves.

.DESCRIPTION
    Only binaries are used: a COPY of an installed Firebird 5.0 runs as a
    plain application (firebird.exe -a). No service, no VM, no Docker.

    SAFETY -- no installed Firebird service is touched. The engine is copied
    into WorkDir\run\fb and run on its own TCP port, bound to 127.0.0.1, with
    its own lock folder, temp folder and security database. The script
    refuses to start if the port is already in use. Teardown (-Teardown)
    stops only the processes started from inside WorkDir\run\fb.

    ONE STAND PER HOST -- all Firebird 5 processes share kernel object names
    (for example fb50_trace). A second private stand that runs at the same
    time fails with "Wrong file for memory mapping" and its logins fail. The
    script refuses to start while another private stand runs.

    OVER SSH -- Windows OpenSSH kills all child processes when the session
    ends, the private server too. Run this script and hang-repro.ps1 in ONE
    ssh command. In a normal console window this does not matter.

    WHAT YOU HAVE AT THE END (the prepared stand):
      * a private Firebird 5.0 SuperServer running as firebird.exe -a,
        127.0.0.1:<Port>, on the cores of <CpuMask> only (CpuAffinityMask),
        its process id saved in WorkDir\run\stand.psd1;
      * two databases: test.fdb (with table T, seeded with <Writers> rows)
        and probe.fdb (empty, used only by the hang probe);
      * helper files in WorkDir\run: writer1.sql..writerN.sql (make garbage),
        connect.sql (flood of attaches), gc.sql (counts GC transactions),
        one.sql (the probe query), trace.conf (a trace session config);
      * stand.psd1 -- the paths, the port and the server pid, which
        hang-repro.ps1 reads. The installed services are left as they were.

    Set $env:ISC_PASSWORD to the SYSDBA password of FbRoot\security5.fdb
    (that security database is copied). On a test host this is usually the
    Firebird default password.

.PARAMETER FbRoot
    Folder of the installed Firebird 5.0 to copy, e.g. C:\HQbird\Firebird50.
    Read only, never changed.

.PARAMETER WorkDir
    Private work folder (keep it on a path WITHOUT spaces). Everything of the
    stand lives under WorkDir\run.

.PARAMETER Port
    TCP port of the private server, 127.0.0.1 only. Change it if 3071 is busy.

.PARAMETER Writers
    How many writer loops hang-repro.ps1 will run. One writerI.sql per writer
    is prepared, and T is seeded with that many rows (one per writer).

.PARAMETER Connects
    How many attach-flood loops hang-repro.ps1 will run. Stored for the repro.

.PARAMETER CpuMask
    CpuAffinityMask of the private server: a bit mask of the CPU cores it may
    use. 1 = core 0 only. The deadlock is a race between two engine threads;
    on one core they switch slowly, as on the 2-vCPU Linux stand where the
    bash script hangs every time. On a 16-core host without a mask it does
    not hang in 10 cycles.

.PARAMETER Teardown
    Stop the stand and delete WorkDir\run. Nothing is set up.

.EXAMPLE
    $env:ISC_PASSWORD = '<SYSDBA password>'
    .\hang-prepare.ps1 -FbRoot C:\HQbird\Firebird50 -WorkDir C:\fbrepro
    .\hang-repro.ps1   -WorkDir C:\fbrepro -Iterations 40
    .\hang-prepare.ps1 -WorkDir C:\fbrepro -Teardown
#>

param(
    [string] $FbRoot   = 'C:\HQbird\Firebird50',
    [string] $WorkDir  = 'C:\fbrepro',
    [int]    $Port     = 3071,
    [int]    $Writers  = 2,
    [int]    $Connects = 4,
    [int]    $CpuMask  = 1,
    [string] $User     = 'SYSDBA',
    [switch] $Teardown
)

$ErrorActionPreference = 'Stop'

# --- paths of the stand (kept in step with hang-repro.ps1 through stand.psd1)
$Run      = Join-Path $WorkDir 'run'
$Fb       = Join-Path $Run 'fb'
$StandPsd = Join-Path $Run 'stand.psd1'
$Isql     = Join-Path $Fb 'isql.exe'
$Gstat    = Join-Path $Fb 'gstat.exe'
$Firebird = Join-Path $Fb 'firebird.exe'
$Db       = "localhost/${Port}:$Run\test.fdb"

# Firebird tools (and the server) read these; they keep the copy apart from
# the installed instances. Child processes inherit them, so the password is
# never put on a command line.
$env:ISC_USER      = $User
$env:FIREBIRD      = $Fb
$env:FIREBIRD_LOCK = Join-Path $Run 'lock'
$env:FIREBIRD_TMP  = Join-Path $Run 'tmp'

# Write one timestamped line to the screen.
function Write-Log { param([string] $Message) Write-Host ('{0} {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message) }

# Run a Firebird tool and stop the whole script if it fails. Used only for
# the quick setup steps (create database, create table), which must succeed.
function Invoke-Setup {
    param([string] $Exe, [string[]] $FbArgs, [string] $What)
    # A tool writing to stderr must not abort the script by itself; we judge
    # it by its exit code only.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & $Exe @FbArgs 2>&1 | Out-String
    $ErrorActionPreference = $old
    if ($LASTEXITCODE -ne 0) { throw "$What failed (rc=$LASTEXITCODE): $($out.Trim())" }
    Write-Log "  $What ok"
}

# -------- Teardown: stop the stand, then leave. -----------------------------
# Stops only processes whose exe lives inside $Fb (the private copy), so the
# installed Firebird services are never affected, then removes WorkDir\run.
if ($Teardown) {
    # First the cdb of catch-crash.ps1 (its command line names WorkDir\crash.cdb):
    # a server under a debugger does not end while the debugger runs, and cdb
    # keeps the DLL files of the server open. Stopping cdb also ends the server.
    Get-CimInstance Win32_Process -Filter "Name='cdb.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$(Join-Path $WorkDir 'crash.cdb')*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (Test-Path $Fb) {
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($Fb, [System.StringComparison]::OrdinalIgnoreCase) } |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2   # the stopped processes free their files
    if (Test-Path $Run) { Remove-Item -Recurse -Force $Run }
    Write-Log "stand removed: $Run"
    return
}

# -------- Checks before we build the stand. ---------------------------------
if (-not $env:ISC_PASSWORD) { Write-Host 'ERROR: set $env:ISC_PASSWORD' -ForegroundColor Red; exit 64 }
if (-not (Test-Path (Join-Path $FbRoot 'firebird.exe'))) { Write-Host "ERROR: no firebird.exe in $FbRoot" -ForegroundColor Red; exit 64 }
# Another private stand (a firebird.exe under some ...\run\fb\) would share
# the kernel objects with ours -- see ONE STAND PER HOST above.
$other = Get-Process -Name firebird -ErrorAction SilentlyContinue | Where-Object { $_.Path -match '\\run\\fb\\firebird\.exe$' }
if ($other) { Write-Host "ERROR: another stand runs: $($other[0].Path) -- tear it down first" -ForegroundColor Red; exit 64 }
if (Test-Path $Run) { Write-Host "ERROR: $Run exists -- run with -Teardown first" -ForegroundColor Red; exit 64 }
if (Test-NetConnection -ComputerName '127.0.0.1' -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Write-Host "ERROR: port $Port is already in use -- pick another -Port" -ForegroundColor Red; exit 64
}

New-Item -ItemType Directory -Force -Path $Run, $env:FIREBIRD_LOCK, $env:FIREBIRD_TMP | Out-Null

# -------- 1. Copy the engine and write a private config. --------------------
Write-Log "copying $FbRoot -> $Fb"
Copy-Item -Path $FbRoot -Destination $Fb -Recurse -Force
Remove-Item (Join-Path $Fb 'firebird.log') -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $Fb 'replication.conf') -Value '' -Encoding ASCII

# firebird.conf: drop the keys we own, then append our values.
$conf = Join-Path $Fb 'firebird.conf'
$lines = Get-Content $conf | Where-Object {
    $_ -notmatch '^\s*(ServerMode|RemoteServicePort|RemoteAuxPort|RemoteBindAddress|SecurityDatabase|CpuAffinityMask)\s*='
}
$lines += 'ServerMode = Super', "RemoteServicePort = $Port", 'RemoteAuxPort = 0',
          'RemoteBindAddress = 127.0.0.1', "SecurityDatabase = $Fb\security5.fdb",
          "CpuAffinityMask = $CpuMask"
Set-Content -Path $conf -Value $lines -Encoding ASCII

# databases.conf: point the security alias at our copy.
$dbconf = Join-Path $Fb 'databases.conf'
if (Test-Path $dbconf) {
    (Get-Content $dbconf) -replace '^(\s*security\.db\s*=).*', "`$1 $Fb\security5.fdb" |
        Set-Content -Path $dbconf -Encoding ASCII
}

# -------- 2. Start the private server (detached; it stays up after exit). ----
$proc = Start-Process -FilePath $Firebird -ArgumentList '-a' -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Run 'server.out') -RedirectStandardError (Join-Path $Run 'server.err')
$ServerPid = $proc.Id
$up = $false
for ($i = 0; $i -lt 30; $i++) {
    if (Test-NetConnection -ComputerName '127.0.0.1' -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) { $up = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $up) { throw "the server did not open port $Port (see $Run\server.err)" }
$banner = (Get-Item $Firebird).VersionInfo.FileVersion   # e.g. WI-V5.0.5.1880
$cpus = (Get-Process -Id $ServerPid).ProcessorAffinity    # the mask Windows really applied
Write-Log "server up: $banner, pid $ServerPid, port $Port, CPU affinity mask $cpus"

# -------- 3. Create the databases and table T. ------------------------------
foreach ($name in 'test', 'probe') {
    $sql = Join-Path $Run 'create.sql'
    Set-Content $sql "create database 'localhost/${Port}:$Run\$name.fdb';" -Encoding ASCII
    Invoke-Setup -Exe $Isql -FbArgs @('-q', '-i', $sql) -What "create $name.fdb"
}
# One table T; one seed row per writer, so each writer updates its own row
# (makes back-record versions for the garbage collector, without lock clashes).
$seed = @('create table T (W integer, TXT varchar(100));', 'commit;')
for ($w = 1; $w -le $Writers; $w++) { $seed += "insert into T values ($w, 'seed');" }
$seed += 'commit;'
$schema = Join-Path $Run 'schema.sql'
Set-Content $schema $seed -Encoding ASCII
Invoke-Setup -Exe $Isql -FbArgs @('-q', '-i', $schema, $Db) -What "table T + $Writers rows"

# -------- 4. Write the helper files the repro uses. -------------------------
# writerI.sql: update one row again and again -> a stream of garbage so the GC
# thread keeps an open transaction. Enough lines to last the whole load window.
for ($w = 1; $w -le $Writers; $w++) {
    $body = 1..20000 | ForEach-Object { "update T set TXT = 'v$_' where W = $w; commit;" }
    Set-Content -Path (Join-Path $Run "writer$w.sql") -Value $body -Encoding ASCII
}
# connect.sql: many CONNECTs fed to one isql -> a near-constant attach flood,
# with no process start per attach. During full shutdown each one is rejected.
Set-Content -Path (Join-Path $Run 'connect.sql') -Value (1..50000 | ForEach-Object { "connect '$Db';" }) -Encoding ASCII
# gc.sql: how many transactions the Garbage Collector attachment holds now.
Set-Content -Path (Join-Path $Run 'gc.sql') -Encoding ASCII -Value @'
set heading off;
select 'garbage collector transactions: ' || count(*)
from MON$TRANSACTIONS t
join MON$ATTACHMENTS a on a.MON$ATTACHMENT_ID = t.MON$ATTACHMENT_ID
where a.MON$USER = 'Garbage Collector';
'@
# one.sql: the probe query. A fresh attach runs it; in the deadlock it hangs.
Set-Content -Path (Join-Path $Run 'one.sql') -Value 'select 1 from rdb$database;' -Encoding ASCII
# trace.conf: a user trace session that logs connections of one database.
Set-Content -Path (Join-Path $Run 'trace.conf') -Encoding ASCII -Value @'
database
{
	enabled = true
	log_connections = true
}
'@

# -------- 5. Save the stand description for hang-repro.ps1. ------------------
@"
@{
    WorkDir   = '$WorkDir'
    Run       = '$Run'
    Fb        = '$Fb'
    Port      = $Port
    ServerPid = $ServerPid
    User      = '$User'
    Writers   = $Writers
    Connects  = $Connects
    Db        = '$Db'
    ProbeDb   = 'localhost/${Port}:$Run\probe.fdb'
    Isql      = '$Isql'
    Gfix      = '$(Join-Path $Fb 'gfix.exe')'
    Gstat     = '$Gstat'
    Firebird  = '$Firebird'
    Fbtracemgr= '$(Join-Path $Fb 'fbtracemgr.exe')'
}
"@ | Set-Content -Path $StandPsd -Encoding ASCII

# -------- Done. Describe the end state. -------------------------------------
Write-Log ''
Write-Log 'STAND READY:'
Write-Log "  server    : firebird.exe -a, 127.0.0.1:$Port, pid $ServerPid"
Write-Log "  databases : $Run\test.fdb (table T, $Writers rows), $Run\probe.fdb"
Write-Log "  helpers   : writer1..$Writers.sql, connect.sql, gc.sql, one.sql, trace.conf"
Write-Log "  stand file: $StandPsd"
Write-Log "  next      : .\hang-repro.ps1 -WorkDir $WorkDir"
Write-Log "  teardown  : .\hang-prepare.ps1 -WorkDir $WorkDir -Teardown"
