# Module 92 (Windows): host actions of the tests csrf and ghost_autoreinit.
# The same commands and arguments as 92-ident.sh (see there what each one
# does); a best-effort port:
#
#   92-ident.ps1 rcm-http        --auth digest|session|none --method GET --path /v1/alerts
#                                [--body-b64 B64] [--content-type application/json|none]
#                                [--headers-b64 B64] [--login-headers same-origin|none|cross-site]
#   92-ident.ps1 rcm-digest-fail --count 11 [--user NAME] [--then-real true]
#   92-ident.ps1 rcm-svc         --action stop|start|restart|status [--unit NAME]
#   92-ident.ps1 rcm-block       --action on|off --port PORT [--unit NAME]   (Windows Firewall, outbound, the RCM program)
#   92-ident.ps1 rcm-ghost       --action add|remove|list --rcm-dir DIR --donor-node ID --donor-db ID
#                                [--old-master-node ID --old-master-db ID --rel-path P --guid G --promoted-to-node ID]
#   92-ident.ps1 rcm-reinits     --rcm-dir DIR
#
# --rcm-addr HOST:PORT changes the RCM operator address (default 127.0.0.1:7444).
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "92-ident"

$p = Parse-TbArgs $args
$A = $p.Args
$Addr = Arg $A "rcm_addr" "127.0.0.1:7444"
$Keep = @("content-type", "x-frame-options", "content-security-policy", "referrer-policy",
          "x-content-type-options", "location")

function Read-RcmSecrets {
  if (Test-Path -LiteralPath $TbSecrets) {
    foreach ($line in Get-Content -LiteralPath $TbSecrets) {
      $eq = $line.IndexOf("=")
      if ($eq -gt 0) { Set-Item -Path ("env:" + $line.Substring(0, $eq).Trim()) -Value $line.Substring($eq + 1) }
    }
  }
  if (-not $env:TB_RCM_USER -or -not $env:TB_RCM_PASSWORD) { Die "TB_RCM_USER / TB_RCM_PASSWORD are not set (secrets rcm_user, rcm_password)" }
}

function Send-Rcm([string]$method, [string]$path, $data, [hashtable]$headers) {
  $req = [Net.HttpWebRequest]::Create("http://" + $Addr + $path)
  $req.Method = $method
  $req.AllowAutoRedirect = $false
  $req.Timeout = 60000
  foreach ($k in $headers.Keys) {
    $v = [string]$headers[$k]
    switch ($k.ToLower()) {
      "accept" { $req.Accept = $v }
      "content-type" { $req.ContentType = $v }
      default { $req.Headers[$k] = $v }
    }
  }
  if ($null -ne $data) {
    $req.ContentLength = $data.Length
    $s = $req.GetRequestStream(); $s.Write($data, 0, $data.Length); $s.Close()
  } elseif ($method -ne "GET" -and $method -ne "HEAD") {
    $req.ContentLength = 0
  }
  try { $resp = $req.GetResponse() } catch [Net.WebException] { $resp = $_.Exception.Response; if (-not $resp) { Die $_.Exception.Message } }
  $hs = @{}
  foreach ($k in $resp.Headers.AllKeys) { $hs[$k.ToLower()] = [string]$resp.Headers[$k] }
  $ms = New-Object IO.MemoryStream
  $resp.GetResponseStream().CopyTo($ms)
  $st = [int]$resp.StatusCode
  $resp.Close()
  return @{ Status = $st; Headers = $hs; Raw = $ms.ToArray() }
}

function Md5Hex([string]$s) {
  $m = [Security.Cryptography.MD5]::Create()
  return (($m.ComputeHash([Text.Encoding]::UTF8.GetBytes($s)) | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Get-Challenge {
  $r = Send-Rcm "GET" "/v1/alerts" $null @{ "Accept" = "application/json" }
  $w = [string]$r.Headers["www-authenticate"]
  if (-not $w.ToLower().StartsWith("digest ")) { Die "no Digest challenge from RCM on $Addr (HTTP $($r.Status))" }
  $ch = @{}
  foreach ($m in [regex]::Matches($w.Substring(7), '(\w+)=(?:"([^"]*)"|([^,\s]*))')) {
    $v = $m.Groups[2].Value; if (-not $m.Groups[2].Success) { $v = $m.Groups[3].Value }
    $ch[$m.Groups[1].Value.ToLower()] = $v
  }
  return $ch
}

function Get-Digest([string]$method, [string]$path, [string]$user, [string]$password) {
  $ch = Get-Challenge
  $realm = [string]$ch["realm"]; $nonce = [string]$ch["nonce"]
  $ha1 = Md5Hex "${user}:${realm}:${password}"
  $ha2 = Md5Hex "${method}:${path}"
  $qops = @(([string]$ch["qop"]).Split(",") | ForEach-Object { $_.Trim() })
  if ($qops -contains "auth") {
    $nc = "00000001"; $cnonce = [Guid]::NewGuid().ToString("N").Substring(0, 16)
    $resp = Md5Hex "${ha1}:${nonce}:${nc}:${cnonce}:auth:${ha2}"
    return "Digest username=`"$user`", realm=`"$realm`", nonce=`"$nonce`", uri=`"$path`", algorithm=MD5, response=`"$resp`", qop=auth, nc=$nc, cnonce=`"$cnonce`""
  }
  $resp = Md5Hex "${ha1}:${nonce}:${ha2}"
  return "Digest username=`"$user`", realm=`"$realm`", nonce=`"$nonce`", uri=`"$path`", algorithm=MD5, response=`"$resp`""
}

function Decode-Body($r) {
  $text = [Text.Encoding]::UTF8.GetString($r.Raw)
  if (([string]$r.Headers["content-type"]) -match "json" -and $text) {
    try { return (ConvertFrom-Json $text) } catch { }
  }
  return $text.Substring(0, [Math]::Min(1500, $text.Length))
}

# A property of a JSON object, or $null (strict mode refuses a missing one).
function Prop($o, [string]$name) {
  if ($null -ne $o -and $o.PSObject.Properties[$name]) { return $o.$name }
  return $null
}

function Rcm-Service {
  $unit = Arg $A "unit"
  if ($unit) {
    $s = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq $unit } | Select-Object -First 1
    if ($s) { return $s }
  }
  $s = Get-CimInstance Win32_Service | Where-Object { $_.PathName -and $_.PathName -match 'hqbirdrcm' } | Select-Object -First 1
  if (-not $s) { Die "no hqbirdrcm service" }
  return $s
}

function Rcm-Up {
  try {
    $r = Send-Rcm "GET" "/v1/health" $null @{ "Accept" = "application/json" }
    return ($r.Status -eq 200)
  } catch { return $false }
}

switch ($p.Cmd) {
  "rcm-http" {
    Read-RcmSecrets
    $method = Arg $A "method" "GET"
    $path = Arg $A "path" "/v1/alerts"
    $ctype = Arg $A "content_type" "application/json"
    $data = $null
    if (Arg $A "body_b64") { $data = [Convert]::FromBase64String((Arg $A "body_b64")) }
    $h = @{ "Accept" = "application/json" }
    if ($null -ne $data -and $ctype -ne "none") { $h["Content-Type"] = $ctype }
    $out = [ordered]@{ auth = (Arg $A "auth" "digest") }
    switch ($out.auth) {
      "session" {
        $form = "username=" + [Uri]::EscapeDataString($env:TB_RCM_USER) + "&password=" + [Uri]::EscapeDataString($env:TB_RCM_PASSWORD)
        $lh = @{ "Content-Type" = "application/x-www-form-urlencoded" }
        $lmode = Arg $A "login_headers" "same-origin"
        if ($lmode -eq "same-origin") {
          $lh["Origin"] = "http://" + $Addr
          $lh["Sec-Fetch-Site"] = "same-origin"
        } elseif ($lmode -eq "cross-site") {
          $lh["Origin"] = "http://127.0.0.1:7999"
          $lh["Sec-Fetch-Site"] = "cross-site"
        }
        $lr = Send-Rcm "POST" "/login" ([Text.Encoding]::UTF8.GetBytes($form)) $lh
        $m = [regex]::Match([string]$lr.Headers["set-cookie"], 'rcm_session=[^;,\s]+')
        $out.login_status = $lr.Status
        $out.login_cookie = $m.Success
        if (-not $m.Success) { $out.status = $null; Result $out; exit 0 }
        $h["Cookie"] = $m.Value
      }
      "digest" { $h["Authorization"] = Get-Digest $method $path $env:TB_RCM_USER $env:TB_RCM_PASSWORD }
    }
    if (Arg $A "headers_b64") {
      $extra = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Arg $A "headers_b64"))))
      foreach ($prop in $extra.PSObject.Properties) { $h[$prop.Name] = [string]$prop.Value }
    }
    $r = Send-Rcm $method $path $data $h
    $hs = [ordered]@{}
    foreach ($k in $Keep) { if ($r.Headers.ContainsKey($k)) { $hs[$k] = $r.Headers[$k] } }
    $out.status = $r.Status
    $out.headers = $hs
    $out.body = Decode-Body $r
    Result $out
  }

  "rcm-digest-fail" {
    Read-RcmSecrets
    $bad = Arg $A "user" "tb-no-such-user"
    $statuses = @()
    $last = $null
    for ($i = 0; $i -lt [int](Arg $A "count" "11"); $i++) {
      $auth = Get-Digest "GET" "/v1/alerts" $bad ([Guid]::NewGuid().ToString("N"))
      $r = Send-Rcm "GET" "/v1/alerts" $null @{ "Accept" = "application/json"; "Authorization" = $auth }
      $statuses += $r.Status
      $last = Decode-Body $r
    }
    $out = [ordered]@{ statuses = $statuses; last_body = $last; user = $bad }
    if ((Arg $A "then_real" "false") -eq "true") {
      $auth = Get-Digest "GET" "/v1/alerts" $env:TB_RCM_USER $env:TB_RCM_PASSWORD
      $out.real_status = (Send-Rcm "GET" "/v1/alerts" $null @{ "Accept" = "application/json"; "Authorization" = $auth }).Status
    }
    Result $out
  }

  "rcm-svc" {
    $svc = Rcm-Service
    $action = Arg $A "action" "status"
    switch ($action) {
      "stop" { Stop-Service -Name $svc.Name -Force }
      "start" { Start-Service -Name $svc.Name }
      "restart" { Restart-Service -Name $svc.Name -Force }
      "status" { }
      default { Die "--action stop|start|restart|status" }
    }
    if ($action -eq "start" -or $action -eq "restart") { $up = Wait-Until 90 { Rcm-Up } } else { $up = Rcm-Up }
    Result @{ unit = $svc.Name; action = $action; active = [string](Get-Service -Name $svc.Name).Status; answering = [bool]$up
              local_time = (Get-Date).ToString("HH:mm") }
  }

  "rcm-block" {
    $port = Arg $A "port"
    if ($port -notmatch '^\d+$') { Die "--port is required" }
    switch (Arg $A "action") {
      "on" {
        $svc = Rcm-Service
        $pn = [string]$svc.PathName
        if ($pn.StartsWith('"')) { $exe = $pn.Substring(1, $pn.IndexOf('"', 1) - 1) }
        else { $exe = ($pn -split '\.exe', 2)[0] + ".exe" }
        New-NetFirewallRule -DisplayName "tb-ident-rcm-block" -Direction Outbound -Program $exe -Protocol TCP `
          -RemotePort $port -Action Block | Out-Null
        Result @{ action = "on"; port = [int]$port; program = $exe }
      }
      "off" {
        $rules = @(Get-NetFirewallRule -DisplayName "tb-ident-rcm-block" -ErrorAction SilentlyContinue)
        if ($rules.Count -gt 0) { $rules | Remove-NetFirewallRule }
        Result @{ action = "off"; removed = $rules.Count }
      }
      default { Die "--action on|off" }
    }
  }

  "rcm-ghost" {
    $action = Arg $A "action" "list"
    if (@("add", "remove", "list") -notcontains $action) { Die "--action add|remove|list" }
    $dir = Arg $A "rcm_dir" "C:\hqbirdrcm"
    $conf = Join-Path $dir "rcm.json"
    if (-not (Test-Path -LiteralPath $conf)) { Die "no $conf" }
    $dd = [string](Prop (Read-Json $conf) "data_dir")
    if (-not $dd) { $dd = ".\rcm-data" }
    if (-not [IO.Path]::IsPathRooted($dd)) { $dd = [IO.Path]::GetFullPath((Join-Path $dir $dd)) }
    $state = Join-Path $dd "state.json"
    $svc = $null
    if ($action -ne "list") { $svc = Rcm-Service; Stop-Service -Name $svc.Name -Force }
    try {
      $st = Read-Json $state
      if (-not $st.PSObject.Properties["ghost_replicas"] -or $null -eq $st.ghost_replicas) {
        Set-JsonProp $st "ghost_replicas" (New-Object PSObject)
      }
      $g = $st.ghost_replicas
      $key = (Arg $A "donor_node") + "|" + (Arg $A "donor_db")
      if ($action -eq "add") {
        foreach ($n in @("donor_node", "donor_db", "old_master_node", "old_master_db")) {
          if (-not (Arg $A $n)) { Die "add needs --donor-node, --donor-db, --old-master-node, --old-master-db" }
        }
        $rec = [ordered]@{
          donor_node_id = Arg $A "donor_node"; donor_db_id = Arg $A "donor_db"
          old_master_node_id = Arg $A "old_master_node"; old_master_db_id = Arg $A "old_master_db"
          rel_path = Arg $A "rel_path"; promoted_to_node_id = (Arg $A "promoted_to_node" (Arg $A "donor_node"))
          at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        }
        if (Arg $A "guid") { $rec.guid = Arg $A "guid" }
        Set-JsonProp $g $key ([PSCustomObject]$rec)
      } elseif ($action -eq "remove" -and $g.PSObject.Properties[$key]) {
        $g.PSObject.Properties.Remove($key)
      }
      $names = @($g.PSObject.Properties | ForEach-Object { $_.Name } | Sort-Object)
      if ($action -ne "list") {
        if ($names.Count -eq 0) { $st.PSObject.Properties.Remove("ghost_replicas") }
        $tmp = "$state.tb-ident"
        [IO.File]::WriteAllText($tmp, (ConvertTo-Json $st -Depth 100), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $state -Force
      }
    } finally {
      if ($svc) { Start-Service -Name $svc.Name }
    }
    $up = Wait-Until 90 { Rcm-Up }
    Result @{ action = $action; state = $state; ghosts = $names; answering = [bool]$up }
  }

  "rcm-reinits" {
    $dir = Arg $A "rcm_dir" "C:\hqbirdrcm"
    $conf = Join-Path $dir "rcm.json"
    if (-not (Test-Path -LiteralPath $conf)) { Die "no $conf" }
    $dd = [string](Prop (Read-Json $conf) "data_dir")
    if (-not $dd) { $dd = ".\rcm-data" }
    if (-not [IO.Path]::IsPathRooted($dd)) { $dd = [IO.Path]::GetFullPath((Join-Path $dir $dd)) }
    $state = Join-Path $dd "state.json"
    $st = Read-Json $state
    $jobs = @()
    if ($st.PSObject.Properties["reinits"]) {
      foreach ($j in @($st.reinits)) {
        if ($null -eq $j) { continue }
        $jobs += [PSCustomObject][ordered]@{
          id = (Prop $j "id"); master_node = (Prop $j "master_node"); master_db = (Prop $j "master_db")
          replica_node = (Prop $j "replica_node"); command_id = (Prop $j "command_id")
          status = (Prop $j "status"); phase = (Prop $j "phase"); error = (Prop $j "error")
          created_at = [string](Prop $j "created_at")
        }
      }
    }
    if ($jobs.Count -gt 100) { $jobs = $jobs[($jobs.Count - 100)..($jobs.Count - 1)] }
    Result @{ state = $state; reinits = $jobs }
  }

  default { Die "usage: 92-ident.ps1 rcm-http|rcm-digest-fail|rcm-svc|rcm-block|rcm-ghost|rcm-reinits [--key value ...]" }
}
