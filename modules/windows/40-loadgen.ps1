# Module 40 (Windows): put fb-loadgen on this host and check it with a 5 s run.
#
#   40-loadgen.ps1 install [--binary <stage>\bin\fb-loadgen.exe]
#   40-loadgen.ps1 smoke   --db FILE [--host localhost] [--port 3050]
#                          [--profile write-heavy] [--seconds 5]
#
# smoke passes when fb-loadgen exits 0, reaches its final report, and did at
# least one operation ("Total: N" > 0).
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "40-loadgen"
$p = Parse-TbArgs $args
$A = $p.Args

$LgDir = Join-Path $TbWork "loadgen"
$Lg = Join-Path $LgDir "fb-loadgen.exe"

switch ($p.Cmd) {
  "install" {
    $bin = Arg $A "binary" (Join-Path $TbWork "stage\bin\fb-loadgen.exe")
    if (-not (Test-Path -LiteralPath $bin)) { Die "no binary $bin" }
    New-Item -ItemType Directory -Force -Path $LgDir | Out-Null
    Copy-Item -LiteralPath $bin -Destination $Lg -Force
    if ((Invoke-Native $Lg @("--help") -Quiet).Code -ne 0) { Die "fb-loadgen does not run on this host" }
    Log "installed $Lg"
    Result @{ binary = $Lg }
  }

  "smoke" {
    Load-Secrets
    if (-not (Test-Path -LiteralPath $Lg)) { Die "fb-loadgen is not installed (run install)" }
    $db = Arg $A "db"
    if (-not $db) { Die "--db is required" }
    $secs = Arg $A "seconds" "5"
    $out = Join-Path $LgDir "smoke"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    Remove-Item -Path (Join-Path $out "smoke*") -Force -ErrorAction SilentlyContinue
    $prof = Arg $A "profile" "write-heavy"
    Log "smoke: $prof for ${secs}s on $db"
    $dsn = (Arg $A "host" "localhost") + "/" + (Arg $A "port" "3050") + ":" + $db
    $r = Invoke-Native $Lg @("--profile", $prof, "--dsn", $dsn, "--user", $env:ISC_USER, "--pass", $env:ISC_PASSWORD,
      "--warmup", "0", "--main", $secs, "--cooldown", "0", "--conn-min", "1", "--conn-max", "2", "--think-ms", "0",
      "--extended-load=false", "--csv", (Join-Path $out "smoke.txt")) -Quiet
    [IO.File]::WriteAllText((Join-Path $out "smoke.log"), $r.Out)
    $m = [regex]::Matches($r.Out, 'Total: (\d+)')
    $total = if ($m.Count -gt 0) { [int]$m[$m.Count - 1].Groups[1].Value } else { 0 }
    Log "exit=$($r.Code) total=$total"
    if ($r.Code -ne 0 -or $total -eq 0 -or $r.Out -notmatch 'FINAL LOAD TEST REPORT') {
      [Console]::Error.WriteLine((($r.Out -split "`n") | Select-Object -Last 30) -join "`n")
      Result @{ ok = $false; exit = $r.Code }
      exit 1
    }
    Result @{ ok = $true; total = $total }
  }

  default { Die "usage: 40-loadgen.ps1 install|smoke [--key value ...]" }
}
