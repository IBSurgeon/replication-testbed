# Module 93 (Windows): host actions for the tests of the hqcluster-node review
# 2026-10-22, stage 1. The same commands and arguments as 93-stage1.sh (see
# there what each one is for); a best-effort port, the stage 1 lab is Linux.
#
#   93-stage1.ps1 sql-utf8     --db FILE --sql-b64 B64 [--fb-root DIR] [--port 3050]
#   93-stage1.ps1 count-tables --db FILE --tables-b64 B64 [--fb-root DIR] [--port 3050]
#   93-stage1.ps1 journal      --node-dir DIR [--file journal.jsonl] [--from 0] [--types T1,T2]
#                              [--db-id ID] [--grep TEXT] [--limit 500]
#   93-stage1.ps1 age-files    --glob PATTERN --days N
#   93-stage1.ps1 fblog-inject --path FIREBIRD_LOG --count N [--tag tbflood]
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "93-stage1"

$p = Parse-TbArgs $args
$A = $p.Args
$FbRoot = Arg $A "fb_root" "C:\HQbird\Firebird50"
$Port = Arg $A "port" "3050"

# IsqlUtf8 DB SQL -> @{ Code; Out }: the script goes in a UTF-8 file (no BOM),
# the connection charset is UTF8.
function IsqlUtf8([string]$db, [string]$sql) {
  $isql = Fb-Tool $FbRoot "isql"
  if (-not $isql) { Die "no isql in $FbRoot" }
  $tmp = [IO.Path]::GetTempFileName()
  [IO.File]::WriteAllText($tmp, $sql, (New-Object Text.UTF8Encoding($false)))
  try { $r = Invoke-Native $isql @("-q", "-pag", "0", "-ch", "UTF8", "-i", $tmp, "localhost/${Port}:$db") -Quiet }
  finally { Remove-Item -LiteralPath $tmp -Force }
  return $r
}

switch ($p.Cmd) {
  "sql-utf8" {
    Load-Secrets
    $db = Arg $A "db"
    if (-not (Test-Path -LiteralPath $db)) { Die "no database $db" }
    $sql = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "sql_b64")))
    $r = IsqlUtf8 $db $sql
    $out = [string]$r.Out
    $bad = $out.Contains("Statement failed") -or $out.Contains("SQLSTATE =")
    if ($out.Length -gt 4000) { $out = $out.Substring($out.Length - 4000) }
    Result @{ ok = ($r.Code -eq 0 -and -not $bad); out = $out }
  }

  "count-tables" {
    Load-Secrets
    $db = Arg $A "db"
    if (-not (Test-Path -LiteralPath $db)) { Die "no database $db" }
    $names = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "tables_b64"))))
    $out = @()
    foreach ($n in @($names)) {
      $q = '"' + ([string]$n).Replace('"', '""') + '"'
      $r = IsqlUtf8 $db "set heading off;`nset transaction read only;`nselect 'RC|' || count(*) from $q;`ncommit;`n"
      $rows = $null
      foreach ($line in ([string]$r.Out -split "`n")) {
        $t = $line.Trim()
        if ($t.StartsWith("RC|")) { $v = 0; if ([long]::TryParse($t.Substring(3).Trim(), [ref]$v)) { $rows = $v } }
      }
      $err = ""
      if ($null -eq $rows) { $err = ([string]$r.Out).Trim(); if ($err.Length -gt 300) { $err = $err.Substring($err.Length - 300) } }
      $out += @{ name = [string]$n; rows = $rows; error = $err }
    }
    Result @{ tables = @($out) }
  }

  "journal" {
    $path = Join-Path (Arg $A "node_dir" "C:\hqclusternode") (Arg $A "file" "journal.jsonl")
    $from = [int](Arg $A "from" "0")
    $want = @((Arg $A "types") -split "," | Where-Object { $_ })
    $dbid = Arg $A "db_id"
    $grep = Arg $A "grep"
    $limit = [int](Arg $A "limit" "500")
    $lines = @()
    if (Test-Path -LiteralPath $path) { $lines = [IO.File]::ReadAllLines($path) }
    if ($from -lt 0) { Result @{ lines = $lines.Count; events = @() }; break }
    if ($from -gt $lines.Count) { $from = 0 }
    $ev = New-Object System.Collections.ArrayList
    for ($i = $from; $i -lt $lines.Count; $i++) {
      $l = $lines[$i]
      if ($grep -and -not $l.Contains($grep)) { continue }
      try { $e = ConvertFrom-Json $l } catch { continue }
      if ($null -eq $e) { continue }
      $type = ""
      if ($e.PSObject.Properties["type"]) { $type = [string]$e.type } elseif ($e.PSObject.Properties["op"]) { $type = [string]$e.op }
      if ($want.Count -gt 0 -and $want -notcontains $type) { continue }
      $id = ""
      if ($e.PSObject.Properties["db_id"]) { $id = [string]$e.db_id }
      if ($dbid -and $id -ne $dbid) { continue }
      foreach ($k in @("before", "after")) { if ($e.PSObject.Properties[$k]) { $e.PSObject.Properties.Remove($k) } }
      [void]$ev.Add($e)
    }
    $start = [Math]::Max(0, $ev.Count - $limit)
    $tail = @()
    if ($ev.Count -gt 0) { $tail = @($ev.GetRange($start, $ev.Count - $start)) }
    Result @{ lines = $lines.Count; events = $tail }
  }

  "age-files" {
    $g = Arg $A "glob"
    if (-not $g) { Die "--glob is required" }
    $d = Arg $A "days"
    if ($d -notmatch '^\d+$') { Die "--days N (whole days)" }
    $t = (Get-Date).AddDays(-[int]$d)
    $fs = @(Get-ChildItem -Path $g -File -ErrorAction SilentlyContinue | Sort-Object FullName)
    foreach ($f in $fs) { $f.LastWriteTime = $t; $f.LastAccessTime = $t }
    Result @{ aged = $fs.Count; mtime = [double](Get-Date $t.ToUniversalTime() -UFormat %s); files = @($fs | ForEach-Object { $_.FullName }) }
  }

  "fblog-inject" {
    $path = Arg $A "path"
    if (-not $path) { Die "--path is required" }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Die "no file $path (the test appends to Firebird's own log, it never creates one)" }
    $count = [int](Arg $A "count" "1")
    $tag = Arg $A "tag" "tbflood"
    $nets = @("192.0.2.", "198.51.100.", "203.0.113.")
    $kinds = @("read errno = 10054", "send errno = 10053", "read errno = 10060")
    $rnd = New-Object Random
    $before = (Get-Item -LiteralPath $path).Length
    $sb = New-Object Text.StringBuilder
    $last = ""
    for ($i = 0; $i -lt $count; $i++) {
      if ($i -eq $count - 1) { $client = "$tag-last" } else { $client = "$tag-ws$($i % 97)" }
      $addr = "$($nets[$i % 3])$(1 + ($i * 7) % 254)/$(1024 + $rnd.Next(0, 64000))"
      $last = "INET/inet_error: $($kinds[$i % 3]), client host = $client, address = $addr, user = TB_FLOOD"
      $stamp = (Get-Date).ToString("ddd MMM d HH:mm:ss yyyy", [Globalization.CultureInfo]::InvariantCulture)
      [void]$sb.Append("`r`n$env:COMPUTERNAME`t$stamp`r`n`t$last`r`n`r`n")
    }
    # Append only: the log is never cut.
    [IO.File]::AppendAllText($path, $sb.ToString(), (New-Object Text.UTF8Encoding($false)))
    Result @{ path = $path; entries = $count; size_before = $before; size_after = (Get-Item -LiteralPath $path).Length; last_line = $last }
  }

  default { Die "usage: 93-stage1.ps1 sql-utf8|count-tables|journal|age-files|fblog-inject" }
}
