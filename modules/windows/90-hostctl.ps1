# Module 90 (Windows): small host actions for tb.py and the tests.
#
#   90-hostctl.ps1 secure-file PATH
#   90-hostctl.ps1 node-api     --node-dir DIR --method GET --path /v1/status [--body-b64 B64] [--timeout 60]
#   90-hostctl.ps1 node-svc     --node-dir DIR --action stop|start|restart|kill
#   90-hostctl.ps1 fb-svc       --fb-service NAME --action stop|start|restart|status
#   90-hostctl.ps1 counts       --db FILE [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 limbo        --db FILE [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 files        --glob PATTERN
#   90-hostctl.ps1 remove-file  --path FILE
#   90-hostctl.ps1 block-peer   --addr ADDR [--port PORT]    (Windows Firewall, both ways)
#   90-hostctl.ps1 unblock-peer --addr ADDR [--port PORT]
#   90-hostctl.ps1 tail         --path FILE [--lines 50]
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "90-hostctl"

if ($args.Count -ge 2 -and $args[0] -eq "secure-file") { Secure-File ([string]$args[1]); exit 0 }
$p = Parse-TbArgs $args
$A = $p.Args
$NodeDir = Arg $A "node_dir" "C:\hqclusternode"
$FbRoot = Arg $A "fb_root" "C:\HQbird\Firebird50"
$Port = Arg $A "port" "3050"

function Isql([string]$db, [string]$sql) {
  $isql = Fb-Tool $FbRoot "isql"
  if (-not $isql) { Die "no isql in $FbRoot" }
  $tmp = [IO.Path]::GetTempFileName()
  [IO.File]::WriteAllText($tmp, $sql, (New-Object Text.UTF8Encoding($false)))
  try { $r = Invoke-Native $isql @("-q", "-pag", "0", "-i", $tmp, "localhost/${Port}:$db") -Quiet }
  finally { Remove-Item -LiteralPath $tmp -Force }
  if ($r.Code -ne 0) { Die "isql failed on ${db}: $($r.Out)" }
  return $r.Out
}

switch ($p.Cmd) {
  "node-api" {
    $exe = Join-Path $NodeDir "hqclusternode.exe"
    $a = @("api", (Arg $A "method" "GET"), (Arg $A "path" "/v1/status"), "-i", "-timeout-sec", (Arg $A "timeout" "60"),
           "-config", (Join-Path $NodeDir "node.json"), "-certs", (Join-Path $NodeDir "certs"))
    $tmp = ""
    if (Arg $A "body_b64") {
      $tmp = [IO.Path]::GetTempFileName()
      [IO.File]::WriteAllBytes($tmp, [Convert]::FromBase64String((Arg $A "body_b64")))
      $a += @("-body-file", $tmp)
    }
    try { $r = Invoke-Native $exe $a -Quiet } finally { if ($tmp) { Remove-Item -LiteralPath $tmp -Force } }
    if ($r.Code -ne 0) { [Console]::Error.WriteLine($r.Out); Die "api call failed (exit $($r.Code))" }
    # Pretty JSON: its line breaks are whitespace only, so joining the lines
    # keeps the document intact without a ConvertFrom/To-Json round trip.
    [Console]::Out.WriteLine("TBRESULT " + (($r.Out -split "`r?`n") -join " "))
  }

  "node-svc" {
    $exe = Join-Path $NodeDir "hqclusternode.exe"
    $cfg = Join-Path $NodeDir "node.json"
    switch (Arg $A "action") {
      "stop" { Invoke-Native $exe @("svc", "stop", "-config", $cfg) | Out-Null }
      "start" { Invoke-Native $exe @("svc", "start", "-config", $cfg) | Out-Null }
      "restart" { Invoke-Native $exe @("svc", "stop", "-config", $cfg) | Out-Null; Invoke-Native $exe @("svc", "start", "-config", $cfg) | Out-Null }
      # A crash: the service recovery settings start the node again.
      "kill" { Get-Process -Name "hqclusternode" -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force }
      default { Die "--action stop|start|restart|kill" }
    }
    Result @{ node_svc = (Arg $A "action") }
  }

  "fb-svc" {
    $svc = Arg $A "fb_service"
    if (-not $svc) { Die "--fb-service is required" }
    switch (Arg $A "action") {
      "stop" { Stop-Service -Name $svc -Force }
      "start" { Start-Service -Name $svc }
      "restart" { Restart-Service -Name $svc -Force }
      "status" { }
      default { Die "--action stop|start|restart|status" }
    }
    Result @{ unit = $svc; active = [string](Get-Service -Name $svc).Status }
  }

  "counts" {
    Load-Secrets
    $db = Arg $A "db"
    $list = Isql $db @"
set heading off;
select trim(r.rdb`$relation_name) || '|' ||
  iif(exists(select 1 from rdb`$indices i where i.rdb`$relation_name = r.rdb`$relation_name
             and i.rdb`$unique_flag = 1 and coalesce(i.rdb`$index_inactive, 0) = 0), 'K', 'N')
from rdb`$relations r
where coalesce(r.rdb`$system_flag, 0) = 0 and r.rdb`$view_blr is null
order by 1;
"@
    $parts = @()
    foreach ($line in ($list -split "`n")) {
      $t = $line.Trim()
      if (-not $t.Contains("|")) { continue }
      $name, $k = $t.Split("|") | ForEach-Object { $_.Trim() }
      $parts += "select 'RC|$name|$k|' || count(*) from `"$name`""
    }
    if ($parts.Count -eq 0) { Die "no user tables in $db" }
    $out = Isql $db ("set heading off; set transaction read only ignore limbo;`n" + ($parts -join "`nunion all ") + ";`ncommit;`n")
    $rows = @{}
    foreach ($line in ($out -split "`n")) {
      $t = $line.Trim()
      if ($t.StartsWith("RC|")) { $f = $t.Split("|") | ForEach-Object { $_.Trim() }; $rows[$f[1]] = @{ keyed = ($f[2] -eq "K"); rows = [long]$f[3] } }
    }
    Result $rows
  }

  "limbo" {
    Load-Secrets
    $gfix = Fb-Tool $FbRoot "gfix"
    $r = Invoke-Native $gfix @("-list", ("localhost/${Port}:" + (Arg $A "db"))) -Quiet
    Result @{ limbo = ([regex]::Matches($r.Out, '(?i)transaction \d+')).Count }
  }

  "files" {
    $fs = @(Get-ChildItem -Path (Arg $A "glob") -File -ErrorAction SilentlyContinue | Sort-Object FullName |
      ForEach-Object { @{ path = $_.FullName; size = $_.Length; mtime = [double](Get-Date $_.LastWriteTimeUtc -UFormat %s) } })
    [Console]::Out.WriteLine("TBRESULT " + (ConvertTo-Json @($fs) -Compress -Depth 4))
  }

  "remove-file" {
    $f = Arg $A "path"
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Die "no file $f" }
    Remove-Item -LiteralPath $f -Force
    Result @{ removed = $f }
  }

  { $_ -in @("block-peer", "unblock-peer") } {
    $addr = Arg $A "addr"
    if (-not $addr) { Die "--addr is required" }
    $name = "tb-block-$addr"
    Get-NetFirewallRule -DisplayName "$name*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if ($p.Cmd -eq "block-peer") {
      $extra = @{}
      if (Arg $A "port") { $extra = @{ Protocol = "TCP"; RemotePort = (Arg $A "port") } }
      New-NetFirewallRule -DisplayName "$name-out" -Direction Outbound -RemoteAddress $addr -Action Block @extra | Out-Null
      if (Arg $A "port") { $extra = @{ Protocol = "TCP"; LocalPort = (Arg $A "port") } }
      New-NetFirewallRule -DisplayName "$name-in" -Direction Inbound -RemoteAddress $addr -Action Block @extra | Out-Null
    }
    Result @{ action = $p.Cmd; addr = $addr }
  }

  "tail" { Get-Content -LiteralPath (Arg $A "path") -Tail ([int](Arg $A "lines" "50")) | ForEach-Object { [Console]::Out.WriteLine($_) } }

  default { Die "usage: 90-hostctl.ps1 secure-file|node-api|node-svc|fb-svc|counts|limbo|files|remove-file|block-peer|unblock-peer|tail" }
}
