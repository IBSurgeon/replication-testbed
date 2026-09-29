# Module 06 (Windows): a second Firebird instance on the same host, as a
# copy of an installed HQbird root (hqcluster-node
# docs/hqbird-25-30-finish-plan.md, T-2; the 4.0 pair on the lab host is the
# model: Firebird40 + Firebird40R).
#
#   06-instance.ps1 create --source DIR --target DIR --port N --service NAME
#   06-instance.ps1 remove --target DIR --service NAME
#   06-instance.ps1 status --target DIR --service NAME
#
# create copies the root (without logs), gives the copy its own
# RemoteServicePort, IpcName and RemotePipeName (2.5: RemoteAuxPort 0 too),
# and registers the service "FirebirdServer<NAME>" (manual start) that runs
# the copy's server with "-s <NAME>". HQbird 2.5/3.0: replconf.properties of
# the copy points to a copy of the file the source reads now, never to the
# source's own file. A marker file (.hqtb-instance) tells a copy made here;
# remove deletes only such a copy.
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "06-instance"
$p = Parse-TbArgs $args
$A = $p.Args
$Marker = ".hqtb-instance"

function Svc-Name([string]$n) { if ($n -like "FirebirdServer*") { return $n } return "FirebirdServer$n" }
function Instance-Name([string]$n) { return ($n -replace '^FirebirdServer', '') }

function Server-Exe([string]$root) {
  foreach ($c in @("firebird.exe", "bin\fb_inet_server.exe", "bin\fbserver.exe")) {
    $f = Join-Path $root $c
    if (Test-Path -LiteralPath $f) { return $f }
  }
  return $null
}

function Set-ConfKeys([string]$path, [hashtable]$kv) {
  $lines = @(Get-Content -LiteralPath $path)
  $seen = @{}
  $out = foreach ($l in $lines) {
    $k = ($l -split '=', 2)[0].Trim()
    if ($l -match '=' -and -not $l.TrimStart().StartsWith('#') -and $kv.ContainsKey($k)) { $seen[$k] = $true; "$k = $($kv[$k])" } else { $l }
  }
  foreach ($k in $kv.Keys) { if (-not $seen[$k]) { $out += "$k = $($kv[$k])" } }
  [IO.File]::WriteAllLines($path, [string[]]$out)
}

$target = Arg $A "target"
$svc = Svc-Name (Arg $A "service")
if (-not $target -or -not (Arg $A "service")) { Die "--target and --service are required" }

switch ($p.Cmd) {
  "create" {
    Need-Admin
    $source = Arg $A "source"
    $port = [int](Arg $A "port" "0")
    if (-not $source -or $port -le 0) { Die "--source and --port are required" }
    if (-not (Server-Exe $source)) { Die "no Firebird server in $source" }
    if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath (Join-Path $target $Marker))) {
      Die "$target exists and was not made by the test bed"
    }
    $other = Get-CimInstance Win32_Service -Filter "Name='$svc'" -ErrorAction SilentlyContinue
    if ($other -and $other.PathName -notlike "*$target*") { Die "service $svc exists for another root: $($other.PathName)" }
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
      if (-not $other -or (Get-Service $svc).Status -ne "Running") { Die "port $port is taken" }
    }
    if (-not (Test-Path -LiteralPath $target)) {
      Log "copy $source -> $target"
      New-Item -ItemType Directory -Force -Path $target | Out-Null
      & robocopy.exe $source $target /E /NFL /NDL /NJH /NJS /NP /XF *.log *.lck "replication.log*" | Out-Null
      if ($LASTEXITCODE -ge 8) { Die "robocopy failed ($LASTEXITCODE)" }
      Set-Content -LiteralPath (Join-Path $target $Marker) -Value $source
    }
    $name = Instance-Name $svc
    $keys = @{ RemoteServicePort = "$port"; IpcName = "FB_TB_$name"; RemotePipeName = "fb_tb_$name" }
    $exe = Server-Exe $target
    $legacy25 = $exe -like "*fb_inet_server.exe" -or $exe -like "*fbserver.exe"
    if ($legacy25) { $keys["RemoteAuxPort"] = "0" }
    Set-ConfKeys (Join-Path $target "firebird.conf") $keys
    # HQbird 2.5/3.0: never share the source's replconf file.
    $props = Join-Path (Split-Path -Parent $exe) "replconf.properties"
    if (Test-Path -LiteralPath $props) {
      $inUse = (Get-Content -LiteralPath $props | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
      $copy = Join-Path $target "replconf.tb-initial.hqbird"
      if ($inUse -and (Test-Path -LiteralPath $inUse) -and -not ($inUse -like "$target*")) {
        Copy-Item -LiteralPath $inUse -Destination $copy -Force
        [IO.File]::WriteAllText($props, "$copy`r`n")
        Log "replconf.properties -> $copy (a copy of $inUse)"
      }
    }
    if (-not $other) {
      $bin = if ($legacy25) { "`"$exe`" -s $name -m" } else { "`"$exe`" -s $name" }
      & sc.exe create $svc binPath= $bin start= demand DisplayName= "Firebird Server - $name (test bed)" | Out-Null
      if ($LASTEXITCODE -ne 0) { Die "sc create $svc failed" }
    }
    if ((Get-Service $svc).Status -ne "Running") { Start-Service -Name $svc }
    if (-not (Wait-Until 60 { Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue })) {
      Die "the copy does not listen on port $port"
    }
    Result @{ target = $target; service = $svc; port = $port; server = $exe }
  }

  "remove" {
    Need-Admin
    if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath (Join-Path $target $Marker))) {
      Die "$target was not made by the test bed; not removing it"
    }
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
      $path = (Get-CimInstance Win32_Service -Filter "Name='$svc'").PathName
      if ($path -notlike "*$target*") { Die "service $svc belongs to $path; not removing it" }
      Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
      & sc.exe delete $svc | Out-Null
    }
    Stop-ProcessesUnder $target
    if (Test-Path -LiteralPath $target) {
      try { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop } catch { Die "cannot remove ${target}: $_" }
    }
    Result @{ removed = $target; service = $svc }
  }

  "status" {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    Result @{ target = $target; exists = (Test-Path -LiteralPath $target); marker = (Test-Path -LiteralPath (Join-Path $target $Marker))
              service = $svc; state = $(if ($s) { [string]$s.Status } else { "" }) }
  }

  default { Die "usage: 06-instance.ps1 create|remove|status [--key value ...]" }
}
