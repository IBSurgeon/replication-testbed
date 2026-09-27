# Install / remove steps shared by 10-local.ps1 and 20-goafts.ps1. Dot-sourced.
#
# Stage layout (filled by tb.py, or by hand):
#   <stage>\bin\fbagent.exe  <stage>\bin\hqclusternode.exe  <stage>\bin\hqbirdrcm.exe
#   <stage>\conf\node.json   <stage>\conf\rcm.json
#   <stage>\certs\           ca.crt + this node's <node_id>.crt/.key
#   <stage>\rcm-certs\       ca.crt + rcm.crt/.key

function Lock-Dir([string]$dir) {
  # Administrators + SYSTEM only: node.json and rcm.json hold passwords.
  & icacls.exe $dir /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" /T | Out-Null
}

# ---------------------------------------------------------------- Firebird --
function Fb-SavePristineConf([string]$root) {
  $conf = Join-Path $root "replication.conf"
  if ((Test-Path -LiteralPath $conf) -and -not (Test-Path -LiteralPath "$conf.tb-pristine")) {
    Copy-Item -LiteralPath $conf -Destination "$conf.tb-pristine"
    Log "saved pristine $conf"
  }
}

function Fb-RestorePristineConf([string]$root, [string]$service) {
  $conf = Join-Path $root "replication.conf"
  if (Test-Path -LiteralPath "$conf.tb-pristine") {
    Copy-Item -LiteralPath "$conf.tb-pristine" -Destination $conf -Force
    Remove-Item -LiteralPath "$conf.tb-pristine" -Force
    Log "restored pristine $conf"
    if ($service) { try { Restart-Service -Name $service -Force } catch { Warn "restart $service failed: $_" } }
  }
}

# ----------------------------------------------------------------- fbagent --
function Fbagent-Check([int]$apiPort, [string]$instance, [int]$fbPort) {
  $h = @{ Authorization = "Bearer $($env:TB_FBAGENT_TOKEN)" }
  $j = Invoke-RestMethod -UseBasicParsing -Headers $h -Uri "http://127.0.0.1:$apiPort/v1/instances/$instance" -TimeoutSec 10
  if ([int]$j.port -ne $fbPort) { throw "fbagent instance $instance has port $($j.port), want $fbPort" }
  Log "fbagent instance OK: port $($j.port) state $($j.state)"
  return $true
}

function Fbagent-LocalApi($cfg, [int]$apiPort, [string]$instance) {
  if (-not $env:TB_FBAGENT_TOKEN) { Die "TB_FBAGENT_TOKEN is not set (secrets file)" }
  $api = [pscustomobject]@{ enabled = $true; listen = "127.0.0.1:$apiPort"; token = $env:TB_FBAGENT_TOKEN; instance_id = $instance }
  Set-JsonProp $cfg "local_api" $api
  if (-not $cfg.PSObject.Properties["firebird"]) { Set-JsonProp $cfg "firebird" ([pscustomobject]@{}) }
  $pw = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($env:TB_FB_PASSWORD))
  Set-JsonProp $cfg.firebird "credentials" ([pscustomobject]@{ username = $env:TB_FB_USER; password_encrypted = $pw })
}

function Fbagent-WriteConfig([string]$dir, [string]$fbRoot, [int]$fbPort, [string]$fbService,
                             [int]$apiPort, [string]$instance, [string]$service) {
  $aid = ($env:COMPUTERNAME.ToLower()) + (Get-Date).ToUniversalTime().ToString("yyMMdd")
  $cfg = [pscustomobject]@{
    agent = [pscustomobject]@{ id = $aid; max_concurrent_long_tasks = 2 }
    firebird = [pscustomobject]@{
      install_path = $fbRoot; port = $fbPort; version = "auto"
      restart = [pscustomobject]@{ services = @([pscustomobject]@{ windows_name = $fbService; linux_unit = "firebird" }) }
    }
    goafts = [pscustomobject]@{
      agent_id = $aid; server_url = ""
      cert_path = "certs/agent.crt"; key_path = "certs/agent.key"; ca_path = "certs/ca.crt"
      outcoming_dir = "outcoming"; journal_path = "logs/sent_files.journal.jsonl"; delete_after_send = $true
      auto_update = [pscustomobject]@{ enabled = $false; systemd_unit = "hqbirdfbagent"; service_name = $service }
    }
    logging = [pscustomobject]@{ file = "logs/firebird-agent.jsonl"; level = "info" }
    storage = [pscustomobject]@{ data_path = "logs" }
    log_rotation = [pscustomobject]@{ enabled = $true; logs_path = "logs" }
  }
  Fbagent-LocalApi $cfg $apiPort $instance
  Write-JsonFile (Join-Path $dir "agent_config.json") $cfg
}

function Fbagent-InstallLocal([string]$stage, [string]$dir, [string]$fbRoot, [int]$fbPort, [string]$fbService,
                              [int]$apiPort, [string]$instance, [string]$service) {
  $bin = Join-Path $stage "bin\fbagent.exe"
  if (-not (Test-Path -LiteralPath $bin)) { Die "missing $bin" }
  $svc = Get-Service -Name $service -ErrorAction SilentlyContinue
  if ($svc) {
    $path = (Get-CimInstance Win32_Service -Filter "Name='$service'").PathName
    if ($path -notlike "*$dir*") { Die "service $service already exists ($path). Use fbagent mode 'existing' or another fbagent service name." }
    Stop-Service -Name $service -Force -ErrorAction SilentlyContinue
  }
  Log "fbagent -> $dir"
  foreach ($d in @("logs", "certs", "updates", "outcoming")) { New-Item -ItemType Directory -Force -Path (Join-Path $dir $d) | Out-Null }
  Copy-Item -LiteralPath $bin -Destination (Join-Path $dir "fbagent.exe") -Force
  Fbagent-WriteConfig $dir $fbRoot $fbPort $fbService $apiPort $instance $service
  Lock-Dir $dir
  Push-Location $dir
  try {
    $a = @("--install")
    if ($service -ne "HQbirdFBAgent") { $a += @("--service-name", $service) }
    $r = Invoke-Native (Join-Path $dir "fbagent.exe") $a
    if ($r.Code -ne 0) { Die "fbagent --install failed" }
  } finally { Pop-Location }
  Start-Service -Name $service
  if (-not (Wait-Until 60 { Fbagent-Check $apiPort $instance $fbPort })) { Die "fbagent local_api does not answer on 127.0.0.1:$apiPort" }
}

function Fbagent-Uninstall([string]$dir, [string]$service) {
  $exe = Join-Path $dir "fbagent.exe"
  if (Test-Path -LiteralPath $exe) {
    Push-Location $dir
    try {
      Invoke-Native $exe @("--stop") -Quiet | Out-Null
      $a = @("--uninstall")
      if ($service -ne "HQbirdFBAgent") { $a += @("--service-name", $service) }
      Invoke-Native $exe $a | Out-Null
    } finally { Pop-Location }
  }
  if (Get-Service -Name $service -ErrorAction SilentlyContinue) {
    Stop-Service -Name $service -Force -ErrorAction SilentlyContinue
    & sc.exe delete $service | Out-Null
  }
  foreach ($m in @(Get-Service -Name "HQbirdMonitor*" -ErrorAction SilentlyContinue)) {
    $p = (Get-CimInstance Win32_Service -Filter "Name='$($m.Name)'").PathName
    if ($p -like "*$dir*") { Stop-Service -Name $m.Name -Force -ErrorAction SilentlyContinue; & sc.exe delete $m.Name | Out-Null }
  }
  Start-Sleep -Seconds 2
  if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
  Log "fbagent removed from $dir"
}

# ----------------------------------------------------------- hqclusternode --
function Node-Install([string]$stage, [string]$dir, [string]$dbRoot) {
  $bin = Join-Path $stage "bin\hqclusternode.exe"
  $conf = Join-Path $stage "conf\node.json"
  if (-not (Test-Path -LiteralPath $bin)) { Die "missing $bin" }
  if (-not (Test-Path -LiteralPath $conf)) { Die "missing $conf" }
  Log "hqclusternode -> $dir"
  $exe = Join-Path $dir "hqclusternode.exe"
  $cfg = Join-Path $dir "node.json"
  $certs = Join-Path $dir "certs"
  if (Test-Path -LiteralPath $cfg) {
    Invoke-Native $exe @("svc", "stop", "-config", $cfg) -Quiet | Out-Null
    Invoke-Native $exe @("svc", "uninstall", "-config", $cfg) -Quiet | Out-Null
  }
  New-Item -ItemType Directory -Force -Path $certs, $dbRoot | Out-Null
  Copy-Item -LiteralPath $bin -Destination $exe -Force
  Copy-Item -LiteralPath $conf -Destination $cfg -Force
  Copy-Item -Path (Join-Path $stage "certs\*") -Destination $certs -Force
  Lock-Dir $dir
  $r = Invoke-Native $exe @("svc", "install", "-config", $cfg, "-certs", $certs, "-bin", $exe)
  if ($r.Code -ne 0) { Die "hqclusternode svc install failed" }
  $r = Invoke-Native $exe @("svc", "start", "-config", $cfg)
  if ($r.Code -ne 0) { Die "hqclusternode svc start failed" }
  $ok = Wait-Until 60 { (Invoke-Native $exe @("healthcheck", "-config", $cfg, "-certs", $certs) -Quiet).Code -eq 0 }
  if (-not $ok) { Die "node does not answer /v1/health" }
  Log "node healthy"
}

function Node-Uninstall([string]$dir) {
  $exe = Join-Path $dir "hqclusternode.exe"
  $cfg = Join-Path $dir "node.json"
  if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $cfg)) {
    Invoke-Native $exe @("svc", "stop", "-config", $cfg) -Quiet | Out-Null
    $r = Invoke-Native $exe @("svc", "uninstall", "-config", $cfg)
    if ($r.Code -ne 0) { Warn "svc uninstall failed" }
  }
  Start-Sleep -Seconds 2
  if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
  Log "node removed from $dir"
}

# --------------------------------------------------------------- hqbirdrcm --
function Rcm-Install([string]$stage, [string]$dir) {
  $bin = Join-Path $stage "bin\hqbirdrcm.exe"
  $conf = Join-Path $stage "conf\rcm.json"
  if (-not (Test-Path -LiteralPath $bin)) { Die "missing $bin" }
  if (-not (Test-Path -LiteralPath $conf)) { Die "missing $conf" }
  Log "hqbirdrcm -> $dir"
  $exe = Join-Path $dir "hqbirdrcm.exe"
  $cfg = Join-Path $dir "rcm.json"
  if (Test-Path -LiteralPath $cfg) {
    Invoke-Native $exe @("svc", "stop", "-config", $cfg) -Quiet | Out-Null
    Invoke-Native $exe @("svc", "uninstall", "-config", $cfg) -Quiet | Out-Null
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $dir "certs"), (Join-Path $dir "rcm-data") | Out-Null
  Copy-Item -LiteralPath $bin -Destination $exe -Force
  Copy-Item -LiteralPath $conf -Destination $cfg -Force
  Copy-Item -Path (Join-Path $stage "rcm-certs\*") -Destination (Join-Path $dir "certs") -Force
  Lock-Dir $dir
  if ((Invoke-Native $exe @("svc", "install", "-config", $cfg)).Code -ne 0) { Die "hqbirdrcm svc install failed" }
  if ((Invoke-Native $exe @("svc", "start", "-config", $cfg)).Code -ne 0) { Die "hqbirdrcm svc start failed" }
  if (-not (Wait-Until 30 { (Get-Service -Name "hqbirdrcm").Status -eq "Running" })) { Die "hqbirdrcm service is not running" }
  Log "rcm running"
}

function Rcm-Uninstall([string]$dir) {
  $exe = Join-Path $dir "hqbirdrcm.exe"
  $cfg = Join-Path $dir "rcm.json"
  if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $cfg)) {
    Invoke-Native $exe @("svc", "stop", "-config", $cfg) -Quiet | Out-Null
    if ((Invoke-Native $exe @("svc", "uninstall", "-config", $cfg)).Code -ne 0) { Warn "rcm svc uninstall failed" }
  }
  Start-Sleep -Seconds 2
  if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
  Log "rcm removed from $dir"
}
