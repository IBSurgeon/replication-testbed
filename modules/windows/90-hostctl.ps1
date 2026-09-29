# Module 90 (Windows): small host actions for tb.py and the tests.
#
#   90-hostctl.ps1 secure-file PATH
#   90-hostctl.ps1 node-api     --node-dir DIR --method GET --path /v1/status [--body-b64 B64] [--timeout 60] [--addr HOST:PORT]
#   90-hostctl.ps1 node-svc     --node-dir DIR --action stop|start|restart|kill
#   90-hostctl.ps1 fb-svc       --fb-service NAME --action stop|start|restart|status
#   90-hostctl.ps1 counts       --db FILE [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 limbo        --db FILE [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 files        --glob PATTERN
#   90-hostctl.ps1 remove-file  --path FILE
#   90-hostctl.ps1 block-peer   --addr ADDR [--port PORT]    (Windows Firewall, both ways)
#   90-hostctl.ps1 unblock-peer --addr ADDR [--port PORT]
#   90-hostctl.ps1 tail         --path FILE [--lines 50]
#   90-hostctl.ps1 replctl      --dir JOURNAL_SOURCE_DIR   (replica control files)
#   90-hostctl.ps1 statelog     --node-dir DIR --db-id ID [--from LINE]
#   90-hostctl.ps1 replog-inject --path REPLICATION_LOG --db FILE --message-b64 B64 [--role replica] [--level ERROR] [--count 1]
#   90-hostctl.ps1 peer-push    --node-dir DIR --addr HOST:PORT --meta-b64 B64 [--file F | --random N]
#   90-hostctl.ps1 node-on-file --node-dir DIR --path FILE --event locked|unlocked --action stop|kill|kill-stay [--timeout 900] [--delay SEC]
#   90-hostctl.ps1 nbackup-unlock --db FILE [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 nbackup-lock   --db FILE [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 fb-tool        --name nbackup --state off|on [--fb-root DIR]
#   90-hostctl.ps1 db-new-guid  --db FILE --fb-service NAME [--fb-root DIR] [--port 3050]
#   90-hostctl.ps1 rcm-api      --method GET --path /v1/alerts [--body-b64 B64] [--then-restart false]
#   (the same commands as 90-hostctl.sh; see there what each one is for)
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
    if (Arg $A "addr") { $a += @("-addr", (Arg $A "addr")) }
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

  "replctl" {
    # Firebird's replica control files ({GUID}) in a journal source folder:
    # the position applied so far and the transactions held as active.
    $out = @()
    foreach ($f in @(Get-ChildItem -LiteralPath (Arg $A "dir") -File -Filter "{*}" -ErrorAction SilentlyContinue | Sort-Object Name)) {
      $b = [IO.File]::ReadAllBytes($f.FullName)
      if ($b.Length -lt 40 -or [Text.Encoding]::ASCII.GetString($b, 0, 9) -ne "FBREPLCTL") {
        $out += @{ file = $f.FullName; error = "not a control file" }; continue
      }
      $n = [BitConverter]::ToUInt32($b, 12)
      $act = @()
      for ($i = 0; $i -lt $n; $i++) {
        $act += @{ tra = [BitConverter]::ToUInt64($b, 40 + 16 * $i); seq = [BitConverter]::ToUInt64($b, 48 + 16 * $i) }
      }
      $out += @{ file = $f.FullName; sequence = [BitConverter]::ToUInt64($b, 16); offset = [BitConverter]::ToUInt32($b, 24)
                 db_sequence = [BitConverter]::ToUInt64($b, 32); active = $act }
    }
    [Console]::Out.WriteLine("TBRESULT " + (ConvertTo-Json @($out) -Compress -Depth 5))
  }

  "statelog" {
    $path = Join-Path $NodeDir "journal.jsonl"
    $dbid = Arg $A "db_id"
    $from = [int](Arg $A "from" "0")
    $lines = @()
    if (Test-Path -LiteralPath $path) { $lines = [IO.File]::ReadAllLines($path) }
    if ($from -lt 0) { Result @{ lines = $lines.Count; events = @() }; break }
    if ($from -gt $lines.Count) { $from = 0 }
    $ev = @()
    for ($i = $from; $i -lt $lines.Count; $i++) {
      $l = $lines[$i]
      if (-not $l.Contains($dbid) -or -not ($l.Contains('"db_state"') -or $l.Contains('"reinit_step"'))) { continue }
      try { $e = ConvertFrom-Json $l } catch { continue }
      if ($e.db_id -ne $dbid) { continue }
      $f = $e.fields
      $ev += @{ ts = [string]$e.ts; type = $e.type; state = $f.state; reason = $f.reason; generation = $f.generation
                phase = $f.phase; error = $f.error }
    }
    Result @{ lines = $lines.Count; events = @($ev) }
  }

  "replog-inject" {
    $msg = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "message_b64")))
    $role = Arg $A "role" "replica"
    $level = Arg $A "level" "ERROR"
    $text = ""
    for ($i = 0; $i -lt [int](Arg $A "count" "1"); $i++) {
      $stamp = (Get-Date).ToString("ddd MMM dd HH:mm:ss yyyy", [Globalization.CultureInfo]::InvariantCulture)
      $text += "$env:COMPUTERNAME ($role) $stamp`r`n`tDatabase: $(Arg $A 'db')`r`n`t${level}: $msg`r`n`r`n"
    }
    [IO.File]::AppendAllText((Arg $A "path"), $text, (New-Object Text.UTF8Encoding($false)))
    Result @{ path = (Arg $A "path"); blocks = [int](Arg $A "count" "1") }
  }

  "peer-push" {
    $meta = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "meta_b64"))))
    if (Arg $A "file") { $body = [IO.File]::ReadAllBytes((Arg $A "file")) }
    else { $n = [int](Arg $A "random" "4096"); if ($n -le 0) { $n = 4096 }; $body = New-Object byte[] $n; (New-Object Random).NextBytes($body) }
    if (-not $meta.sha256 -or $meta.sha256 -eq "auto") {
      $h = [Security.Cryptography.SHA256]::Create().ComputeHash($body)
      $meta | Add-Member -Force -NotePropertyName sha256 -NotePropertyValue (($h | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    if (-not $meta.uncompressed_size) { $meta | Add-Member -Force -NotePropertyName uncompressed_size -NotePropertyValue $body.Length }
    if (-not $meta.compress) { $meta | Add-Member -Force -NotePropertyName compress -NotePropertyValue "none" }
    $tmp = [IO.Path]::GetTempFileName()
    [IO.File]::WriteAllBytes($tmp, $body)
    # PS 5.1 does not escape inner quotes for a native program: do it here.
    $hdr = "X-HQCluster-Segment: " + (ConvertTo-Json $meta -Compress)
    $exe = Join-Path $NodeDir "hqclusternode.exe"
    $a = @("api", "POST", "/v1/peer/segments", "-i", "-addr", (Arg $A "addr"), "-config", (Join-Path $NodeDir "node.json"),
           "-certs", (Join-Path $NodeDir "certs"), "-body-file", $tmp, "-content-type", "application/octet-stream",
           "-H", ('"' + $hdr.Replace('"', '\"') + '"'))
    try { $r = Invoke-Native $exe $a -Quiet } finally { Remove-Item -LiteralPath $tmp -Force }
    if ($r.Code -ne 0) { [Console]::Error.WriteLine($r.Out); Die "peer push failed (exit $($r.Code))" }
    $out = ConvertFrom-Json (($r.Out -split "`r?`n") -join " ")
    $out | Add-Member -Force -NotePropertyName meta -NotePropertyValue $meta
    Result $out
  }

  "node-on-file" {
    $f = Arg $A "path"
    if (-not $f) { Die "--path is required" }
    $evt = Arg $A "event" "locked"
    $end = (Get-Date).AddSeconds([double](Arg $A "timeout" "900"))
    $seen = $false; $fired = $false
    while ((Get-Date) -lt $end) {
      $ex = Test-Path -LiteralPath $f
      if ($ex -and -not $seen) { $seen = $true; if ($evt -eq "locked") { $fired = $true; break } }
      if ($seen -and -not $ex) { $fired = $true; break }
      Start-Sleep -Milliseconds 20
    }
    if (-not $fired) { Die "no $evt event for $f" }
    Start-Sleep -Seconds ([double](Arg $A "delay" "0"))
    $exe = Join-Path $NodeDir "hqclusternode.exe"
    switch (Arg $A "action" "stop") {
      "stop" { Invoke-Native $exe @("svc", "stop", "-config", (Join-Path $NodeDir "node.json")) | Out-Null }
      "kill" { Get-Process -Name "hqclusternode" -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force }
      "kill-stay" {
        Get-Process -Name "hqclusternode" -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
        Start-Sleep -Seconds 1
        Invoke-Native $exe @("svc", "stop", "-config", (Join-Path $NodeDir "node.json")) | Out-Null
      }
      default { Die "--action stop|kill|kill-stay" }
    }
    Result @{ fired = $evt; action = (Arg $A "action" "stop"); delta_exists = (Test-Path -LiteralPath $f) }
  }

  "nbackup-unlock" {
    Load-Secrets
    $db = Arg $A "db"
    Invoke-Native (Fb-Tool $FbRoot "nbackup") @("-N", "localhost/${Port}:$db") | Out-Null
    Result @{ unlocked = $db; delta_left = (Test-Path -LiteralPath "$db.delta") }
  }

  "fb-tool" {
    $n = Arg $A "name"
    if (-not $n) { Die "--name is required" }
    $t = Fb-Tool $FbRoot $n
    switch (Arg $A "state") {
      "off" { if (Test-Path -LiteralPath $t) { Move-Item -LiteralPath $t -Destination "$t.tb-off" -Force } }
      "on"  { if (Test-Path -LiteralPath "$t.tb-off") { Move-Item -LiteralPath "$t.tb-off" -Destination $t -Force } }
      default { Die "--state off|on" }
    }
    Result @{ tool = $t; present = (Test-Path -LiteralPath $t) }
  }

  "nbackup-lock" {
    Load-Secrets
    $db = Arg $A "db"
    Invoke-Native (Fb-Tool $FbRoot "nbackup") @("-L", "localhost/${Port}:$db") | Out-Null
    Result @{ locked = $db; delta = (Test-Path -LiteralPath "$db.delta") }
  }

  "db-new-guid" {
    Load-Secrets
    $db = Arg $A "db"
    $svc = Arg $A "fb_service"
    if (-not (Test-Path -LiteralPath $db)) { Die "no database $db" }
    if (-not $svc) { Die "--fb-service is required" }
    $nb = Fb-Tool $FbRoot "nbackup"
    $tmp = "$db.tbnewguid"
    Invoke-Native $nb @("-L", "localhost/${Port}:$db") | Out-Null
    $ok = $true
    try { Copy-Item -LiteralPath $db -Destination $tmp -Force } catch { $ok = $false }
    Invoke-Native $nb @("-N", "localhost/${Port}:$db") | Out-Null
    if (-not $ok) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; Die "copy under nbackup lock failed" }
    Invoke-Native $nb @("-F", $tmp) | Out-Null
    Stop-Service -Name $svc -Force
    Move-Item -LiteralPath $tmp -Destination $db -Force
    Start-Service -Name $svc
    Result @{ replaced = $db }
  }

  "rcm-api" {
    if (Test-Path -LiteralPath $TbSecrets) {
      foreach ($line in Get-Content -LiteralPath $TbSecrets) {
        $eq = $line.IndexOf("=")
        if ($eq -gt 0) { Set-Item -Path ("env:" + $line.Substring(0, $eq).Trim()) -Value $line.Substring($eq + 1) }
      }
    }
    if (-not $env:TB_RCM_USER -or -not $env:TB_RCM_PASSWORD) { Die "TB_RCM_USER / TB_RCM_PASSWORD are not set (secrets rcm_user, rcm_password)" }
    # .NET answers the Digest challenge by itself when it has a credential.
    $req = [Net.HttpWebRequest]::Create("http://127.0.0.1:7444" + (Arg $A "path" "/v1/alerts"))
    $req.Method = Arg $A "method" "GET"
    $req.Accept = "application/json"
    $req.Credentials = New-Object Net.NetworkCredential($env:TB_RCM_USER, $env:TB_RCM_PASSWORD)
    $req.Timeout = 60000
    if (Arg $A "body_b64") {
      $bytes = [Convert]::FromBase64String((Arg $A "body_b64"))
      $req.ContentType = "application/json"
      $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
    }
    try { $resp = $req.GetResponse() } catch [Net.WebException] { $resp = $_.Exception.Response; if (-not $resp) { Die $_.Exception.Message } }
    $st = [int]$resp.StatusCode
    $raw = (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
    $resp.Close()
    if ((Arg $A "then_restart" "false") -eq "true") {
      $rcm = Get-CimInstance Win32_Service | Where-Object { $_.PathName -and $_.PathName -match 'hqbirdrcm' } | Select-Object -First 1
      if ($rcm) { Restart-Service -Name $rcm.Name -Force }
    }
    $b = $null
    if ($raw) { try { $b = ConvertFrom-Json $raw } catch { $b = $raw.Substring(0, [Math]::Min(2000, $raw.Length)) } }
    Result @{ status = $st; body = $b }
  }

  default { Die "usage: 90-hostctl.ps1 secure-file|node-api|node-svc|fb-svc|counts|limbo|files|remove-file|block-peer|unblock-peer|tail|replctl|statelog|replog-inject|peer-push|node-on-file|nbackup-unlock|nbackup-lock|fb-tool|db-new-guid|rcm-api" }
}
