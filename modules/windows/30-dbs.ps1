# Module 30 (Windows): test databases on the master, and their removal.
#
#   30-dbs.ps1 prepare --db-root DIR [--subdir tb] [--count 2] [--file-name employee.fdb]
#                      [--source FILE] [--fb-root DIR] [--port 3050] [--force false]
#   30-dbs.ps1 remove  --db-root DIR [--subdir tb]
#   30-dbs.ps1 list    --db-root DIR [--subdir tb]
#
# prepare makes <db-root>\<subdir>\db1..dbN\<file-name> from --source (default:
# the EMPLOYEE example of Firebird), copied under an nbackup lock (-L ... -N);
# -F then makes each copy a standalone database with its own GUID.
# On a replica, remove takes --db-root <replica root>\<master node id>.
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "30-dbs"
$p = Parse-TbArgs $args
$A = $p.Args
Need-Admin

$DbRoot = Arg $A "db_root"
if (-not $DbRoot) { Die "--db-root is required" }
$Sub = Arg $A "subdir" "tb"
if ($Sub -eq "" -or $Sub -eq "." -or $Sub -eq ".." -or $Sub -match '[\\/]') { Die "--subdir must be one folder name" }
$Dir = Join-Path $DbRoot $Sub

function List-Dbs {
  $files = @()
  if (Test-Path -LiteralPath $Dir) {
    $files = @(Get-ChildItem -Path $Dir -Recurse -File -Filter "*.fdb" | Sort-Object FullName | ForEach-Object { $_.FullName })
  }
  Result @{ dir = $Dir; files = $files }
}

switch ($p.Cmd) {
  "prepare" {
    Load-Secrets
    $fbRoot = Arg $A "fb_root" "C:\HQbird\Firebird50"
    $count = [int](Arg $A "count" "2")
    $name = Arg $A "file_name" "employee.fdb"
    $src = Arg $A "source" (Join-Path $fbRoot "examples\empbuild\employee.fdb")
    $nb = Fb-Tool $fbRoot "nbackup"
    if (-not (Test-Path -LiteralPath $src)) { Die "no source database $src" }
    if (-not $nb) { Die "no nbackup in $fbRoot" }
    if ($count -lt 1) { Die "--count must be >= 1" }
    $todo = @()
    for ($i = 1; $i -le $count; $i++) {
      $f = Join-Path $Dir "db$i\$name"
      if ((Test-Path -LiteralPath $f) -and (Arg $A "force" "false") -ne "true") { Log "exists, kept: $f"; continue }
      $todo += $f
    }
    if ($todo.Count -gt 0) {
      $srcDsn = "localhost/" + (Arg $A "port" "3050") + ":" + $src
      Log "copy $src -> $($todo.Count) database(s) under nbackup lock"
      if ((Invoke-Native $nb @("-L", $srcDsn)).Code -ne 0) { Die "nbackup -L failed" }
      $failed = $false
      foreach ($f in $todo) {
        try { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $f) | Out-Null; Copy-Item -LiteralPath $src -Destination $f -Force }
        catch { $failed = $true; Warn "copy to $f failed: $_" }
      }
      Invoke-Native $nb @("-N", $srcDsn) | Out-Null   # always unlock
      if ($failed) { Die "copy under nbackup lock failed" }
      if (Test-Path -LiteralPath "$src.delta") { Die "leftover $src.delta after unlock" }
      foreach ($f in $todo) { if ((Invoke-Native $nb @("-F", $f)).Code -ne 0) { Die "nbackup -F $f failed" } }
    }
    List-Dbs
  }

  "remove" {
    if (Test-Path -LiteralPath $Dir) { Remove-Item -LiteralPath $Dir -Recurse -Force; Log "removed $Dir" }
    else { Log "nothing to remove: $Dir" }
    Result @{ removed = $Dir }
  }

  "list" { List-Dbs }

  default { Die "usage: 30-dbs.ps1 prepare|remove|list [--key value ...]" }
}
