# Module 20 (Windows): install everything FROM A GOAFTS SERVER, and remove it.
#
#   20-goafts.ps1 download  --url https://HOST:9443 --pin HEX [--channel stable]
#                           [--products fbagent,hqclusternode,hqbirdrcm] [--stage DIR]
#   20-goafts.ps1 enroll    --url URL --pin HEX [--enroll-timeout 30m]
#                           --fb-root DIR --fb-port 3050 --fb-service NAME
#                           [--fbagent-dir DIR] [--fbagent-port 13050] [--fbagent-instance ID]
#                           [--fbagent-service HQbirdFBAgent]
#   20-goafts.ps1 install   --components node,rcm [--product-install direct|agent] [--channel stable]
#                           [--stage DIR] [--node-dir DIR] [--db-root DIR] [--rcm-dir DIR]
#                           [--fbagent-dir DIR] [--fbagent-service NAME]
#   20-goafts.ps1 uninstall --components fbagent,node,rcm [dirs as above]
#   20-goafts.ps1 agent-id  [--fbagent-dir DIR]
#
# --pin is the SPKI SHA-256 pin of the goafts TLS certificate (64 hex chars).
# Downloads trust the server only by this pin, and check the sha256 from the
# release metadata. enroll blocks until the agent's CSR is approved on goafts.
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $PSScriptRoot "products.ps1")
$TbName = "20-goafts"
$p = Parse-TbArgs $args
$A = $p.Args
Need-Admin

$Url = (Arg $A "url").TrimEnd("/")
$Pin = Arg $A "pin"
$Channel = Arg $A "channel" "stable"
$Stage = Arg $A "stage" (Join-Path $TbWork "stage")
$FbRoot = Arg $A "fb_root" "C:\HQbird\Firebird50"
$FbPort = [int](Arg $A "fb_port" "3050")
$FbService = Arg $A "fb_service"
$FbaDir = Arg $A "fbagent_dir" "C:\Program Files\HQbird FBAgent"
$FbaPort = [int](Arg $A "fbagent_port" "13050")
$FbaInstance = Arg $A "fbagent_instance" ("tb-" + $env:COMPUTERNAME.ToLower() + "-" + $FbPort)
$FbaService = Arg $A "fbagent_service" "HQbirdFBAgent"
$NodeDir = Arg $A "node_dir" "C:\hqclusternode"
$RcmDir = Arg $A "rcm_dir" "C:\hqbirdrcm"
$Components = Arg $A "components" "node,rcm"

function Need-Pin {
  if (-not $Url.StartsWith("https://")) { Die "--url must be https://HOST:9443" }
  if ($Pin -notmatch '^[0-9a-fA-F]{64}$') { Die "--pin must be 64 hex characters (SPKI SHA-256)" }
}

function Download-Product([string]$product, [string]$dest) {
  $meta = [IO.Path]::GetTempFileName()
  Invoke-PinnedDownload "$Url/v1/bootstrap/releases/$product/windows-amd64?channel=$Channel" $Pin $meta
  $m = Read-Json $meta
  Remove-Item -LiteralPath $meta -Force
  if (-not $m.sha256) { Die "release metadata of $product has no sha256" }
  Invoke-PinnedDownload "$Url/v1/bootstrap/download/$product/windows-amd64?channel=$Channel" $Pin "$dest.part"
  $got = (Get-FileHash -Algorithm SHA256 -LiteralPath "$dest.part").Hash.ToLower()
  if ($got -ne ([string]$m.sha256).ToLower()) { Remove-Item "$dest.part" -Force; Die "$product sha256 mismatch: want $($m.sha256) got $got" }
  Move-Item -LiteralPath "$dest.part" -Destination $dest -Force
  Log "$product $($m.version) downloaded (sha256 OK)"
  return [string]$m.version
}

switch ($p.Cmd) {
  "download" {
    Need-Pin
    $bin = Join-Path $Stage "bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $vers = @{}
    foreach ($prod in (Arg $A "products" "fbagent,hqclusternode,hqbirdrcm").Split(",")) {
      $vers[$prod] = Download-Product $prod (Join-Path $bin "$prod.exe")
    }
    Result $vers
  }

  "enroll" {
    Need-Pin
    Load-Secrets
    $src = Join-Path $Stage "bin\fbagent.exe"
    if (-not (Test-Path -LiteralPath $src)) { Die "run 'download' first: no $src" }
    if (-not $FbService -or -not (Get-Service -Name $FbService -ErrorAction SilentlyContinue)) { Die "Firebird service '$FbService' not found" }
    Fb-SavePristineConf $FbRoot
    if (Get-Service -Name $FbaService -ErrorAction SilentlyContinue) {
      $path = (Get-CimInstance Win32_Service -Filter "Name='$FbaService'").PathName
      if ($path -notlike "*$FbaDir*") { Die "service $FbaService already exists ($path); set another fbagent.service or remove it" }
      Stop-Service -Name $FbaService -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $FbaDir "certs"), (Join-Path $FbaDir "logs") | Out-Null
    $exe = Join-Path $FbaDir "fbagent.exe"
    Copy-Item -LiteralPath $src -Destination $exe -Force
    $cfgPath = Join-Path $FbaDir "agent_config.json"
    $setup = if (Test-Path -LiteralPath $cfgPath) { @("--enroll") } else { @("--setup", $FbRoot) }
    Log "fbagent $($setup -join ' ') --bootstrap-url $Url (waits for CSR approval)"
    Push-Location $FbaDir
    try {
      $r = Invoke-Native $exe ($setup + @("--bootstrap-url", $Url, "--server-pin", $Pin, "--enroll-timeout", (Arg $A "enroll_timeout" "30m")))
      if ($r.Code -ne 0) { Die "fbagent enrollment failed" }
      $cfg = Read-Json $cfgPath
      Fbagent-LocalApi $cfg $FbaPort $FbaInstance
      Write-JsonFile $cfgPath $cfg
      Lock-Dir $FbaDir
      $a = @("--install", "agent_config.json")
      if ($FbaService -ne "HQbirdFBAgent") { $a += @("--service-name", $FbaService) }
      if ((Invoke-Native $exe $a).Code -ne 0) { Die "fbagent --install failed" }
    } finally { Pop-Location }
    Start-Service -Name $FbaService
    if (-not (Wait-Until 60 { Fbagent-Check $FbaPort $FbaInstance $FbPort })) { Die "fbagent local_api does not answer" }
    $cfg = Read-Json $cfgPath
    Result @{ agent_id = [string]$cfg.goafts.agent_id; server_url = [string]$cfg.goafts.server_url }
  }

  "install" {
    Load-Secrets
    $mode = Arg $A "product_install" "direct"
    $dbRoot = Arg $A "db_root"
    if ($mode -eq "direct") {
      if (Has $Components "node") {
        if (-not $dbRoot) { Die "--db-root is required for the node" }
        Node-Install $Stage $NodeDir $dbRoot
      }
      if (Has $Components "rcm") { Rcm-Install $Stage $RcmDir }
    } else {
      $cfgPath = Join-Path $FbaDir "agent_config.json"
      $cfg = Read-Json $cfgPath
      foreach ($c in @("node", "rcm")) {
        if (-not (Has $Components $c)) { continue }
        if ($c -eq "node") { $id = "hqclusternode"; $dir = $NodeDir; $conf = "node.json"; $certs = Join-Path $Stage "certs"
          New-Item -ItemType Directory -Force -Path $dbRoot | Out-Null }
        else { $id = "hqbirdrcm"; $dir = $RcmDir; $conf = "rcm.json"; $certs = Join-Path $Stage "rcm-certs" }
        New-Item -ItemType Directory -Force -Path (Join-Path $dir "certs") | Out-Null
        Copy-Item -LiteralPath (Join-Path $Stage "conf\$conf") -Destination (Join-Path $dir $conf) -Force
        Copy-Item -Path (Join-Path $certs "*") -Destination (Join-Path $dir "certs") -Force
        Lock-Dir $dir
        $upd = [pscustomobject]@{ enabled = $true; install_enabled = $true; apply_automatically = $true; channel = $Channel }
        Set-JsonProp $cfg $id ([pscustomobject]@{ install_path = $dir; config_path = (Join-Path $dir $conf); certs_path = (Join-Path $dir "certs"); update = $upd })
      }
      Write-JsonFile $cfgPath $cfg
      Restart-Service -Name $FbaService -Force
      Start-Sleep -Seconds 3
      $exe = Join-Path $FbaDir "fbagent.exe"
      foreach ($c in @("node", "rcm")) {
        if (-not (Has $Components $c)) { continue }
        $id = if ($c -eq "node") { "hqclusternode" } else { "hqbirdrcm" }
        Log "fbagent --product-update $id --apply"
        Push-Location $FbaDir
        try {
          if ((Invoke-Native $exe @("--product-update", $id, "--apply")).Code -ne 0) { Die "fbagent could not install $id" }
          Invoke-Native $exe @("--product-status", $id) | Out-Null
        } finally { Pop-Location }
      }
      if (Has $Components "node") {
        $nexe = Join-Path $NodeDir "hqclusternode.exe"
        $ok = Wait-Until 120 { (Invoke-Native $nexe @("healthcheck", "-config", (Join-Path $NodeDir "node.json"), "-certs", (Join-Path $NodeDir "certs")) -Quiet).Code -eq 0 }
        if (-not $ok) { Die "node installed by fbagent does not answer /v1/health" }
      }
    }
    # node.json / rcm.json hold the Firebird password: keep them only in place.
    foreach ($d in @("conf", "certs", "rcm-certs")) { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Stage $d) }
    Result @{ installed = $true }
  }

  "uninstall" {
    if (Has $Components "rcm") { Rcm-Uninstall $RcmDir }
    if (Has $Components "node") { Node-Uninstall $NodeDir }
    if (Has $Components "fbagent") {
      Fbagent-Uninstall $FbaDir $FbaService
      Fb-RestorePristineConf $FbRoot $FbService
    }
    Result @{ uninstalled = $true }
  }

  "agent-id" {
    $cfg = Read-Json (Join-Path $FbaDir "agent_config.json")
    Result @{ agent_id = [string]$cfg.goafts.agent_id; hostname = $env:COMPUTERNAME }
  }

  default { Die "usage: 20-goafts.ps1 download|enroll|install|uninstall|agent-id [--key value ...]" }
}
