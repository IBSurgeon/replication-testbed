# Module 50 (Windows): run fb-loadgen load on test databases.
#
#   50-load.ps1 start  (--dbs FILE1,FILE2 | --dir DIR) [--host localhost] [--port 3050]
#                      [--mode write|read|mixed|spike|oltp-emul] [--tx off|emul-safe|full]
#                      [--limbo false] [--extended true] [--conns 1:4] [--minutes 0]
#                      [--think-ms 50] [--tag load]
#   50-load.ps1 stop   [--tag TAG|all]
#   50-load.ps1 status [--tag TAG|all]
#
# Same options as the Linux module (see modules/linux/50-load.sh). The
# processes are started through WMI, outside the ssh session, so they keep
# running after tb.py disconnects. The password is on their command line.
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "50-load"
$p = Parse-TbArgs $args
$A = $p.Args

$Lg = Join-Path $TbWork "loadgen\fb-loadgen.exe"
$RunDir = Join-Path $TbWork "load"
$Tag = Arg $A "tag" "load"
if ($Tag -notmatch '^[A-Za-z0-9_.-]+$') { Die "--tag: letters, digits, _ . - only" }

function Pid-Files([string]$t) {
  if (-not (Test-Path -LiteralPath $RunDir)) { return @() }
  if ($t -eq "all") { return @(Get-ChildItem -Path $RunDir -Recurse -Filter "*.pid" -File) }
  $d = Join-Path $RunDir $t
  if (-not (Test-Path -LiteralPath $d)) { return @() }
  return @(Get-ChildItem -Path $d -Filter "*.pid" -File)
}

function Is-Running([int]$procId) { return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue) }

switch ($p.Cmd) {
  "start" {
    Load-Secrets
    if (-not (Test-Path -LiteralPath $Lg)) { Die "fb-loadgen is not installed (40-loadgen.ps1 install)" }
    $dbs = @()
    if (Arg $A "dbs") { $dbs = (Arg $A "dbs").Split(",") }
    else {
      $dir = Arg $A "dir"
      if (-not $dir) { Die "--dbs or --dir is required" }
      $dbs = @(Get-ChildItem -Path $dir -Recurse -File -Filter "*.fdb" | Sort-Object FullName | ForEach-Object { $_.FullName })
    }
    if ($dbs.Count -eq 0) { Die "no databases to load" }
    $profiles = switch (Arg $A "mode" "write") {
      "write" { @("write-heavy") } "read" { @("read-heavy") } "mixed" { @("write-heavy", "read-heavy") }
      "spike" { @("spike") } "oltp-emul" { @("oltp-emul") }
      default { Die "--mode: write|read|mixed|spike|oltp-emul" }
    }
    $tx = Arg $A "tx" "off"
    if (@("off", "emul-safe", "full") -notcontains $tx) { Die "--tx: off|emul-safe|full" }
    $conns = (Arg $A "conns" "1:4").Split(":")
    $min = [int](Arg $A "minutes" "0")
    $main = if ($min -gt 0) { $min * 60 } else { 604800 }
    $extra = @()
    if ((Arg $A "limbo" "false") -ne "true") { $extra += "--no-limbo" }
    $extra += "--extended-load=" + (Arg $A "extended" "true")
    $out = Join-Path $RunDir $Tag
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    if ((Pid-Files $Tag).Count -gt 0) { Die "tag '$Tag' is already running; stop it first" }
    $n = 0
    foreach ($db in $dbs) {
      if (-not (Test-Path -LiteralPath $db)) { Die "no database $db" }
      $base = (Split-Path -Leaf (Split-Path -Parent $db)) + "-" + (Split-Path -Leaf $db).Replace(".", "_")
      foreach ($prof in $profiles) {
        $name = "$base-$prof"
        $dsn = (Arg $A "host" "localhost") + "/" + (Arg $A "port" "3050") + ":" + $db
        $lgArgs = @("--profile", $prof, "--dsn", "`"$dsn`"", "--user", $env:ISC_USER, "--pass", "`"$($env:ISC_PASSWORD)`"",
          "--tx-variants", $tx) + $extra + @("--conn-min", $conns[0], "--conn-max", $conns[-1],
          "--think-ms", (Arg $A "think_ms" "50"), "--warmup", "5", "--main", "$main", "--cooldown", "5",
          "--report-every", "30", "--csv", "`"$(Join-Path $out "$name.txt")`"")
        $log = Join-Path $out "$name.log"
        $cmd = "cmd.exe /c `"`"$Lg`" $($lgArgs -join ' ') > `"$log`" 2>&1`""
        $procId = Start-Detached $cmd $out
        Set-Content -LiteralPath (Join-Path $out "$name.pid") -Value $procId
        Log "started $name pid $procId"
        $n++
      }
    }
    Start-Sleep -Seconds 3
    $dead = 0
    foreach ($f in (Pid-Files $Tag)) {
      if (-not (Is-Running ([int](Get-Content -LiteralPath $f.FullName)))) {
        $dead++; Warn "$($f.BaseName) exited early"
        $lf = [IO.Path]::ChangeExtension($f.FullName, ".log")
        if (Test-Path -LiteralPath $lf) { Get-Content -LiteralPath $lf -Tail 15 | ForEach-Object { [Console]::Error.WriteLine($_) } }
      }
    }
    Result @{ tag = $Tag; processes = $n; exited_early = $dead }
    if ($dead -gt 0) { exit 1 }
  }

  "stop" {
    $stopped = 0
    foreach ($f in (Pid-Files (Arg $A "tag" "all"))) {
      $procId = [int](Get-Content -LiteralPath $f.FullName)
      if (Is-Running $procId) { Stop-Tree $procId; $stopped++ }
      Remove-Item -LiteralPath $f.FullName -Force
    }
    Log "stopped $stopped process tree(s)"
    Result @{ stopped = $stopped }
  }

  "status" {
    $running = 0; $total = 0
    foreach ($f in (Pid-Files (Arg $A "tag" "all"))) {
      $total++
      $st = "exited"
      if (Is-Running ([int](Get-Content -LiteralPath $f.FullName))) { $running++; $st = "running" }
      $lf = [IO.Path]::ChangeExtension($f.FullName, ".log")
      $last = ""
      if (Test-Path -LiteralPath $lf) { $last = (Select-String -LiteralPath $lf -Pattern 'Status:|Total:' | Select-Object -Last 1).Line }
      Log "$($f.BaseName): $st  $last"
    }
    Result @{ running = $running; total = $total }
  }

  default { Die "usage: 50-load.ps1 start|stop|status [--key value ...]" }
}
