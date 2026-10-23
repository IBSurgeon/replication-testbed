# Module 91 (Windows): host actions for the tests of the node's state journal,
# replica file move and replication.log rotation. A best-effort port of
# 91-journal.sh: the same commands and arguments; see there what each one is for.
#
#   91-journal.ps1 journal-info   --node-dir DIR
#   91-journal.ps1 journal-events --node-dir DIR [--db-id ID] [--types alert,db_move] [--code CODE] [--from LINE]
#   91-journal.ps1 journal-append --node-dir DIR --line-b64 B64 [--force false]
#   91-journal.ps1 snapshot-block --node-dir DIR --state on|off
#   91-journal.ps1 kill-when      --node-dir DIR --on witness|file|now [--db-id ID] [--path FILE] [--timeout 120] [--delay 0] [--action kill|stop]
#   91-journal.ps1 path-info      --paths 'P1;P2'
#   91-journal.ps1 file-partial   --from FILE --to FILE [--fraction 0.5]
#   91-journal.ps1 file-move      --from PATH --to PATH
#   91-journal.ps1 remove-path    --path PATH [--force false]
#   91-journal.ps1 replog-state   --path LOG --node-dir DIR
#   91-journal.ps1 replog-rotate  --path LOG --mode drain|truncate|rename|copytruncate [--bytes N]
#   91-journal.ps1 replog-cleanup --path LOG [--lock-created false] [--keep false]
#   91-journal.ps1 inject-while   --path LOG --db FILE --message-b64 B64 --watch 'P1;P2' [--role replica] [--level ERROR]
#                                 [--burst 3] [--every 2] [--timeout 1800] [--max 100] [--gap 10] [--first-timeout 0]
#
# Windows notes: the node rotates replication.log only around a Firebird
# restart there (RestartOnlyRotator); the rotate lock is a LockFileEx on the
# first byte of <log>.rotate.lock, which FileStream.Lock(0, 1) takes too.
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "91-journal"

$p = Parse-TbArgs $args
$A = $p.Args
$NodeDir = Arg $A "node_dir" "C:\hqclusternode"
$Utf8 = New-Object Text.UTF8Encoding($false)
$Epoch0 = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)

function Now-Epoch { return ([DateTime]::UtcNow - $Epoch0).TotalSeconds }

function Ts-Epoch([string]$ts) {
  if (-not $ts) { return 0 }
  $m = [regex]::Match($ts, '^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$')
  if (-not $m.Success) { return 0 }
  $frac = 0.0
  if ($m.Groups[2].Value) { $frac = [double]::Parse("0" + $m.Groups[2].Value, [Globalization.CultureInfo]::InvariantCulture) }
  $zone = $m.Groups[3].Value
  if ($zone -eq "Z") { $zone = "+00:00" }
  $dto = [DateTimeOffset]::ParseExact($m.Groups[1].Value + $zone, "yyyy-MM-ddTHH:mm:sszzz", [Globalization.CultureInfo]::InvariantCulture)
  return ($dto.UtcDateTime - $Epoch0).TotalSeconds + $frac
}

function File-Info([string]$path) {
  if (Test-Path -LiteralPath $path -PathType Leaf) {
    $i = Get-Item -LiteralPath $path
    return @{ path = $path; exists = $true; size = $i.Length; mtime = ($i.LastWriteTimeUtc - $Epoch0).TotalSeconds }
  }
  return @{ path = $path; exists = $false; size = 0; mtime = 0 }
}

function Line-Ts([string]$line) {
  $m = [regex]::Match($line, '"ts"\s*:\s*"([^"]*)"')
  if ($m.Success) { return $m.Groups[1].Value }
  return ""
}

function Node-Running {
  $exe = Join-Path $NodeDir "hqclusternode.exe"
  return [bool](Get-Process -Name "hqclusternode" -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
}

function Read-Drained([string]$log) {
  $d = "$log.drained"
  if (-not (Test-Path -LiteralPath $d)) { return $null }
  $t = ([IO.File]::ReadAllText($d)).Trim()
  $n = 0L
  if ([long]::TryParse($t, [ref]$n)) { return $n }
  return $null
}

function Add-Drained([string]$log, [long]$n) {
  if ($n -le 0) { return }
  $cur = Read-Drained $log
  if ($null -eq $cur) { $cur = 0L }
  $path = "$log.drained"
  [IO.File]::WriteAllText("$path.tmp", ([string]($cur + $n)) + "`n", $Utf8)
  Move-Item -LiteralPath "$path.tmp" -Destination $path -Force
}

function Make-Parents([string]$src, [string]$dst) {
  $made = @()
  $d = Split-Path -Parent $dst
  $todo = @()
  while ($d -and -not (Test-Path -LiteralPath $d -PathType Container)) { $todo += $d; $d = Split-Path -Parent $d }
  [array]::Reverse($todo)
  foreach ($x in $todo) { New-Item -ItemType Directory -Path $x | Out-Null; $made += $x }
  # The ACL of the source's folder, as the Linux module copies its owner and mode.
  $ref = Split-Path -Parent $src
  foreach ($x in $made) { try { Set-Acl -LiteralPath $x -AclObject (Get-Acl -LiteralPath $ref) } catch { Warn "ACL of ${x}: $($_.Exception.Message)" } }
  return ,$made
}

switch ($p.Cmd) {
  "journal-info" {
    $j = File-Info (Join-Path $NodeDir "journal.jsonl")
    $lines = 0; $first = ""; $last = ""
    if ($j.exists) {
      foreach ($line in [IO.File]::ReadLines($j.path)) {
        if (-not $line.Trim()) { continue }
        $lines++
        $ts = Line-Ts $line
        if ($ts) { if (-not $first) { $first = $ts }; $last = $ts }
      }
    }
    $j.lines = $lines; $j.first_ts = $first; $j.last_ts = $last
    $j.first_epoch = Ts-Epoch $first; $j.last_epoch = Ts-Epoch $last
    $baks = @(Get-ChildItem -LiteralPath $NodeDir -Filter "journal.jsonl.*.bak" -File -ErrorAction SilentlyContinue |
              Sort-Object Name | ForEach-Object { File-Info $_.FullName })
    $s = File-Info (Join-Path $NodeDir "state.json")
    $replies = @{ key = ""; count = 0; oldest_age_sec = 0; untimed = 0 }
    $schema = $null
    if ($s.exists) {
      try {
        $doc = ConvertFrom-Json ([IO.File]::ReadAllText($s.path))
        if ($doc.PSObject.Properties["schema_version"]) { $schema = $doc.schema_version }
        foreach ($key in @("request_replies", "request_replay")) {
          if (-not $doc.PSObject.Properties[$key] -or $null -eq $doc.$key) { continue }
          $props = @($doc.$key.PSObject.Properties)
          $ages = @()
          foreach ($e in $props) {
            $at = ""
            if ($e.Value -is [psobject] -and $e.Value.PSObject.Properties["at"]) { $at = [string]$e.Value.at }
            $t = Ts-Epoch $at
            if ($t) { $ages += ((Now-Epoch) - $t) }
          }
          $oldest = 0
          if ($ages.Count) { $oldest = [int](($ages | Measure-Object -Maximum).Maximum) }
          $replies = @{ key = $key; count = $props.Count; oldest_age_sec = $oldest; untimed = $props.Count - $ages.Count }
          break
        }
      } catch { $s.error = $_.Exception.Message }
    }
    Result @{ journal = $j; baks = $baks; bak_count = $baks.Count; state = $s; schema_version = $schema
              replies = $replies; tmp_blocked = (Test-Path -LiteralPath (Join-Path $NodeDir "state.json.tmp") -PathType Container)
              now = (Now-Epoch) }
  }

  "journal-events" {
    $path = Join-Path $NodeDir "journal.jsonl"
    $lines = @()
    if (Test-Path -LiteralPath $path) { $lines = @([IO.File]::ReadAllLines($path)) }
    $start = [int](Arg $A "from" "0")
    if ($start -lt 0) { Result @{ lines = $lines.Count; events = @() }; break }
    $compacted = $start -gt $lines.Count
    if ($compacted) { $start = 0 }
    $dbid = Arg $A "db_id"; $code = Arg $A "code"
    $want = @((Arg $A "types") -split "," | Where-Object { $_ })
    $ev = New-Object Collections.ArrayList
    for ($i = $start; $i -lt $lines.Count; $i++) {
      $line = $lines[$i]
      if ($dbid -and -not $line.Contains($dbid)) { continue }
      try { $e = ConvertFrom-Json $line } catch { continue }
      if ($dbid -and $e.db_id -ne $dbid) { continue }
      if ($want.Count -and $want -notcontains $e.type) { continue }
      $fields = $null
      if ($e.PSObject.Properties["fields"]) { $fields = $e.fields }
      if ($code -and (-not $fields -or -not $fields.PSObject.Properties["code"] -or $fields.code -ne $code)) { continue }
      [void]$ev.Add(@{ line = $i; ts = (Line-Ts $line); type = $e.type; db_id = $e.db_id; fields = $fields })
    }
    $out = @($ev)
    if ($out.Count -gt 500) { $out = $out[($out.Count - 500)..($out.Count - 1)] }
    Result @{ lines = $lines.Count; compacted = $compacted; events = $out }
  }

  "journal-append" {
    if ((Node-Running) -and (Arg $A "force" "false") -ne "true") { Die "the node runs: stop it first (or --force true)" }
    $raw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "line_b64")))
    $ev = ConvertFrom-Json $raw
    if (-not $ev.PSObject.Properties["type"] -or -not $ev.type) { Die "the event needs a type" }
    if (-not $ev.PSObject.Properties["ts"] -or -not $ev.ts) {
      $ev | Add-Member -Force -NotePropertyName ts -NotePropertyValue ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ"))
    }
    $path = Join-Path $NodeDir "journal.jsonl"
    $new = -not (Test-Path -LiteralPath $path)
    $lead = ""
    if (-not $new -and (Get-Item -LiteralPath $path).Length -gt 0) {
      $fs = [IO.File]::Open($path, "Open", "Read", "ReadWrite")
      try { [void]$fs.Seek(-1, "End"); if ($fs.ReadByte() -ne 10) { $lead = "`n" } } finally { $fs.Close() }
    }
    [IO.File]::AppendAllText($path, $lead + (ConvertTo-Json $ev -Compress -Depth 20) + "`n", $Utf8)
    Result @{ path = $path; created = $new; event = $ev }
  }

  "snapshot-block" {
    $t = Join-Path $NodeDir "state.json.tmp"
    switch (Arg $A "state") {
      "on" {
        if (Test-Path -LiteralPath $t -PathType Leaf) { Remove-Item -LiteralPath $t -Force }
        if (-not (Test-Path -LiteralPath $t)) { New-Item -ItemType Directory -Path $t | Out-Null }
      }
      "off" { if (Test-Path -LiteralPath $t -PathType Container) { Remove-Item -LiteralPath $t } }
      default { Die "--state on|off" }
    }
    Result @{ blocked = (Test-Path -LiteralPath $t -PathType Container) }
  }

  "kill-when" {
    $on = Arg $A "on" "witness"
    $end = (Get-Date).AddSeconds([double](Arg $A "timeout" "120"))
    $t0 = Get-Date
    $fired = $false
    if ($on -eq "now") { $fired = $true }
    elseif ($on -eq "file") {
      $f = Arg $A "path"
      if (-not $f) { Die "--path is required" }
      while ((Get-Date) -lt $end) { if (Test-Path -LiteralPath $f) { $fired = $true; break }; Start-Sleep -Milliseconds 5 }
    } elseif ($on -eq "witness") {
      $dbid = Arg $A "db_id"
      if (-not $dbid) { Die "--on witness needs --db-id" }
      $jp = Join-Path $NodeDir "journal.jsonl"
      $pos = 0L
      if (Test-Path -LiteralPath $jp) { $pos = (Get-Item -LiteralPath $jp).Length }
      $buf = ""
      while (-not $fired -and (Get-Date) -lt $end) {
        $size = 0L
        if (Test-Path -LiteralPath $jp) { $size = (Get-Item -LiteralPath $jp).Length }
        if ($size -lt $pos) { $pos = 0L; $buf = "" }
        if ($size -gt $pos) {
          $fs = [IO.File]::Open($jp, "Open", "Read", "ReadWrite")
          try {
            [void]$fs.Seek($pos, "Begin")
            $bytes = New-Object byte[] ($size - $pos)
            $n = $fs.Read($bytes, 0, $bytes.Length)
          } finally { $fs.Close() }
          $pos += $n
          $buf += $Utf8.GetString($bytes, 0, $n)
          $parts = $buf -split "`n"
          $buf = $parts[$parts.Count - 1]
          for ($i = 0; $i -lt $parts.Count - 1; $i++) {
            $l = $parts[$i]
            if ($l.Contains('"db_move"') -and $l.Contains('"start"') -and $l.Contains($dbid)) { $fired = $true; break }
          }
        }
        if (-not $fired) { Start-Sleep -Milliseconds 5 }
      }
    } else { Die "--on witness|file|now" }
    if (-not $fired) { Die "no $on event within $(Arg $A 'timeout' '120')s" }
    Start-Sleep -Milliseconds ([int]([double](Arg $A "delay" "0") * 1000))
    $exe = Join-Path $NodeDir "hqclusternode.exe"
    switch (Arg $A "action" "kill") {
      "kill" { Get-Process -Name "hqclusternode" -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force }
      "stop" { Invoke-Native $exe @("svc", "stop", "-config", (Join-Path $NodeDir "node.json")) | Out-Null }
      default { Die "--action kill|stop" }
    }
    Result @{ fired = $on; action = (Arg $A "action" "kill"); sec = [int]((Get-Date) - $t0).TotalSeconds }
  }

  "path-info" {
    $out = @()
    foreach ($x in ((Arg $A "paths") -split ";" | Where-Object { $_ })) {
      if (-not (Test-Path -LiteralPath $x)) { $out += @{ path = $x; exists = $false }; continue }
      $i = Get-Item -LiteralPath $x -Force
      $isDir = $i.PSIsContainer
      $size = 0
      if (-not $isDir) { $size = $i.Length }
      $owner = ""
      try { $owner = (Get-Acl -LiteralPath $x).Owner } catch { }
      $kind = "file"
      if ($isDir) { $kind = "dir" }
      $out += @{ path = $x; exists = $true; type = $kind; size = $size
                 mtime = ($i.LastWriteTimeUtc - $Epoch0).TotalSeconds; owner = $owner; group = ""; mode = "" }
    }
    Result $out
  }

  { $_ -eq "file-partial" -or $_ -eq "file-move" } {
    $src = Arg $A "from"; $dst = Arg $A "to"
    if (-not $src -or -not $dst) { Die "--from and --to are required" }
    if (-not (Test-Path -LiteralPath $src)) { Die "no $src" }
    if (Test-Path -LiteralPath $dst) { Die "$dst exists" }
    $made = Make-Parents $src $dst
    $out = @{ from = $src; to = $dst; made = $made }
    if ($p.Cmd -eq "file-move") {
      Move-Item -LiteralPath $src -Destination $dst
    } else {
      $size = (Get-Item -LiteralPath $src).Length
      $n = [long]([double]$size * [double]::Parse((Arg $A "fraction" "0.5"), [Globalization.CultureInfo]::InvariantCulture))
      $fi = [IO.File]::Open($src, "Open", "Read", "ReadWrite")
      $fo = [IO.File]::Open($dst, "CreateNew", "Write", "None")
      try {
        $buf = New-Object byte[] (1MB)
        $left = $n
        while ($left -gt 0) {
          $k = $fi.Read($buf, 0, [int][Math]::Min([long]$buf.Length, $left))
          if ($k -le 0) { break }
          $fo.Write($buf, 0, $k)
          $left -= $k
        }
      } finally { $fi.Close(); $fo.Close() }
      try { Set-Acl -LiteralPath $dst -AclObject (Get-Acl -LiteralPath $src) } catch { Warn "ACL of ${dst}: $($_.Exception.Message)" }
      $out.bytes = $n; $out.size = $size
    }
    Result $out
  }

  "remove-path" {
    $x = Arg $A "path"
    if (-not $x -or $x.Length -le 3) { Die "bad --path" }
    if (-not $x.Contains("tb-") -and -not $x.EndsWith(".hqc-move-tmp")) { Die "$x is not a test artefact (no 'tb-' in the path, not *.hqc-move-tmp)" }
    if (-not (Test-Path -LiteralPath $x)) { Result @{ path = $x; removed = $false }; break }
    if (Test-Path -LiteralPath $x -PathType Container) {
      $dbs = @(Get-ChildItem -LiteralPath $x -Recurse -File -Filter "*.fdb" -ErrorAction SilentlyContinue)
      if ($dbs.Count -and (Arg $A "force" "false") -ne "true") { Die "$x holds databases: --force true to remove it" }
      Remove-Item -LiteralPath $x -Recurse -Force
    } else { Remove-Item -LiteralPath $x -Force }
    Result @{ path = $x; removed = $true }
  }

  "replog-state" {
    $log = Arg $A "path"
    $size = -1
    if (Test-Path -LiteralPath $log) { $size = (Get-Item -LiteralPath $log).Length }
    $off = @{ exists = $false }
    $op = Join-Path $NodeDir "replication.log.offset"
    if (Test-Path -LiteralPath $op) {
      $parts = @(([IO.File]::ReadAllText($op)).Trim() -split "\s+" | Where-Object { $_ })
      $o = $null; $d = $null
      if ($parts.Count -gt 0) { $o = [long]$parts[0] }
      if ($parts.Count -gt 1) { $d = [long]$parts[1] }
      $off = @{ exists = $true; offset = $o; drained = $d; mtime = ((Get-Item -LiteralPath $op).LastWriteTimeUtc - $Epoch0).TotalSeconds }
    }
    $dir = Split-Path -Parent $log
    $leaf = Split-Path -Leaf $log
    $arch = @(Get-ChildItem -LiteralPath $dir -Filter "$leaf.tb-*" -File -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object { $_.FullName })
    Result @{ path = $log; size = $size; drained = (Read-Drained $log); lock = (Test-Path -LiteralPath "$log.rotate.lock")
              offset = $off; archives = $arch; now = (Now-Epoch) }
  }

  "replog-rotate" {
    $log = Arg $A "path"
    $mode = Arg $A "mode"
    if (@("drain", "truncate", "rename", "copytruncate") -notcontains $mode) { Die "--mode drain|truncate|rename|copytruncate" }
    $before = (Get-Item -LiteralPath $log).Length
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmss")
    $arch = "$log.tb-$stamp-$mode"
    $i = 0
    while (Test-Path -LiteralPath $arch) { $i++; $arch = "$log.tb-$stamp-$i-$mode" }
    $res = @{ mode = $mode; size_before = $before; drained_before = (Read-Drained $log); archive = $arch; lock_created = $false }
    if ($mode -eq "drain" -or $mode -eq "truncate") {
      $lockPath = "$log.rotate.lock"
      $res.lock_created = -not (Test-Path -LiteralPath $lockPath)
      $lf = [IO.File]::Open($lockPath, "OpenOrCreate", "Read", "ReadWrite")
      $end = (Get-Date).AddSeconds(30)
      while ($true) {
        try { $lf.Lock(0, 1); break } catch [IO.IOException] {
          if ((Get-Date) -gt $end) { $lf.Close(); Die "the rotate lock stays busy" }
          Start-Sleep -Milliseconds 50
        }
      }
      try {
        $f = [IO.File]::Open($log, "Open", "ReadWrite", "ReadWrite")
        try {
          $size = $f.Length
          if ($mode -eq "drain") {
            $drop = [long](Arg $A "bytes" "0")
            if ($drop -le 0) { $drop = [long][Math]::Floor($size / 2) }
            if ($drop -gt $size) { $drop = $size }
            $head = New-Object byte[] $drop
            [void]$f.Seek(0, "Begin"); [void]$f.Read($head, 0, $drop)
            [IO.File]::WriteAllBytes($arch, $head)
            $buf = New-Object byte[] (256KB)
            $dst = 0L; $srcOff = $drop; $idle = 0
            while ($idle -lt 3) {
              [void]$f.Seek($srcOff, "Begin")
              $k = $f.Read($buf, 0, $buf.Length)
              if ($k -gt 0) { [void]$f.Seek($dst, "Begin"); $f.Write($buf, 0, $k); $dst += $k; $srcOff += $k; $idle = 0; continue }
              if ($f.Length -le $srcOff) { break }
              $idle++
            }
            $f.SetLength($dst)
            $removed = $drop
          } else {
            $all = New-Object byte[] $size
            [void]$f.Seek(0, "Begin"); [void]$f.Read($all, 0, $size)
            [IO.File]::WriteAllBytes($arch, $all)
            $removed = [Math]::Max([long]$size, $f.Length)
            $f.SetLength(0)
          }
        } finally { $f.Close() }
        Add-Drained $log $removed
      } finally { try { $lf.Unlock(0, 1) } catch { }; $lf.Close() }
      $res.dropped = $removed
    } elseif ($mode -eq "rename") {
      $acl = Get-Acl -LiteralPath $log
      Move-Item -LiteralPath $log -Destination $arch
      [IO.File]::WriteAllBytes($log, (New-Object byte[] 0))
      try { Set-Acl -LiteralPath $log -AclObject $acl } catch { Warn "ACL of ${log}: $($_.Exception.Message)" }
      $res.dropped = $before
    } else {
      Copy-Item -LiteralPath $log -Destination $arch
      $f = [IO.File]::Open($log, "Open", "ReadWrite", "ReadWrite")
      try { $res.dropped = $f.Length; $f.SetLength(0) } finally { $f.Close() }
    }
    $res.size_after = (Get-Item -LiteralPath $log).Length
    $res.drained_after = (Read-Drained $log)
    Result $res
  }

  "replog-cleanup" {
    $log = Arg $A "path"
    $gone = @()
    if ((Arg $A "keep" "false") -ne "true") {
      $dir = Split-Path -Parent $log
      $leaf = Split-Path -Leaf $log
      foreach ($f in @(Get-ChildItem -LiteralPath $dir -Filter "$leaf.tb-*" -File -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $f.FullName -Force; $gone += $f.FullName
      }
    }
    if ((Arg $A "lock_created" "false") -eq "true" -and (Test-Path -LiteralPath "$log.rotate.lock")) {
      Remove-Item -LiteralPath "$log.rotate.lock" -Force; $gone += "$log.rotate.lock"
    }
    Result @{ removed = $gone }
  }

  "inject-while" {
    $log = Arg $A "path"
    $msg = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "message_b64")))
    $role = Arg $A "role" "replica"; $level = Arg $A "level" "ERROR"; $db = Arg $A "db"
    $watch = @((Arg $A "watch") -split ";" | Where-Object { $_ })
    if (-not $watch.Count) { Die "--watch is required" }
    $burst = [int](Arg $A "burst" "3"); $every = [double](Arg $A "every" "2"); $most = [int](Arg $A "max" "100")
    $end = (Get-Date).AddSeconds([double](Arg $A "timeout" "1800"))
    $gap = [double](Arg $A "gap" "10")
    $t0 = Get-Date
    $firstTimeout = [double](Arg $A "first_timeout" "0")
    if ($firstTimeout -le 0) { $firstTimeout = [double](Arg $A "timeout" "1800") }
    $seen = $false; $n = 0; $nxt = Get-Date; $first = Get-Date; $ex = $false; $lastEx = Get-Date; $lastWrite = Get-Date
    while ((Get-Date) -lt $end) {
      $ex = $false
      foreach ($w in $watch) { if (Test-Path -LiteralPath $w) { $ex = $true; break } }
      $now = Get-Date
      if ($ex) { $lastEx = $now }
      $k = 0
      if ($ex -and -not $seen) { $seen = $true; $first = $now; $k = [Math]::Min($burst, $most); $nxt = $now.AddSeconds($every) }
      elseif ($ex -and $now -ge $nxt -and $n -lt $most) { $k = 1; $nxt = $now.AddSeconds($every) }
      if ($k -gt 0) { $lastWrite = $now }
      if ($k -gt 0) {
        $text = ""
        for ($i = 0; $i -lt $k; $i++) {
          $stamp = $now.ToString("ddd MMM dd HH:mm:ss yyyy", [Globalization.CultureInfo]::InvariantCulture)
          $text += "$env:COMPUTERNAME ($role) $stamp`r`n`tDatabase: $db`r`n`t${level}: $msg`r`n`r`n"
        }
        [IO.File]::AppendAllText($log, $text, $Utf8)
        $n += $k
      }
      if ($seen -and -not $ex -and ($now - $lastEx).TotalSeconds -ge $gap) { break }
      if ($n -ge $most -or (-not $seen -and ($now - $t0).TotalSeconds -ge $firstTimeout)) { break }
      Start-Sleep -Milliseconds 20
    }
    $sec = 0; $lw = $null
    if ($seen) {
      $sec = [Math]::Round(($lastEx - $first).TotalSeconds, 1)
      $lw = [Math]::Round(($lastEx - $lastWrite).TotalSeconds, 2)
    }
    Result @{ seen = $seen; injected = $n; gone = ($seen -and -not $ex); watched_sec = $sec; last_write_before_gone = $lw }
  }

  default { Die "usage: 91-journal.ps1 journal-info|journal-events|journal-append|snapshot-block|kill-when|path-info|file-partial|file-move|remove-path|replog-state|replog-rotate|replog-cleanup|inject-while ..." }
}
