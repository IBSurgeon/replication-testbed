# Module 10 (Windows): install from LOCAL COPIES of the binaries, no goafts
# enrollment; and remove what it installed.
#
#   10-local.ps1 detect      [--fb-root DIR] [--fb-service NAME]
#   10-local.ps1 install     --components fbagent,node,rcm --stage DIR
#                            --fb-root DIR --fb-port 3050 --fb-service NAME
#                            [--fbagent-mode install|existing] [--fbagent-dir DIR]
#                            [--fbagent-port 13050] [--fbagent-instance ID] [--fbagent-service HQbirdFBAgent]
#                            [--node-dir C:\hqclusternode] [--db-root DIR] [--rcm-dir C:\hqbirdrcm]
#   10-local.ps1 uninstall   --components fbagent,node,rcm [same dirs] [--restore-conf true|false]
#   10-local.ps1 wipe        [same dirs]   (everything the test bed installed; checks what is left)
#   10-local.ps1 fbagent-info --fbagent-dir DIR
#
# fbagent-mode existing: keep the fbagent that HQbird installed; only check it.
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $PSScriptRoot "products.ps1")
$TbName = "10-local"
$p = Parse-TbArgs $args
$A = $p.Args
if ($p.Cmd -ne "detect") { Need-Admin }

$FbRoot = Arg $A "fb_root" "C:\HQbird\Firebird50"
$FbPort = [int](Arg $A "fb_port" "3050")
$FbService = Arg $A "fb_service"
$FbaMode = Arg $A "fbagent_mode" "existing"
$FbaDir = Arg $A "fbagent_dir" (Join-Path $TbWork "fbagent")
$FbaPort = [int](Arg $A "fbagent_port" "13055")
$FbaInstance = Arg $A "fbagent_instance" ("tb-" + $env:COMPUTERNAME.ToLower() + "-" + $FbPort)
$FbaService = Arg $A "fbagent_service" "HQbirdFBAgent"
$NodeDir = Arg $A "node_dir" "C:\hqclusternode"
$RcmDir = Arg $A "rcm_dir" "C:\hqbirdrcm"
$DbRoot = Arg $A "db_root"
$Components = Arg $A "components" "fbagent,node"
$Stage = Arg $A "stage" (Join-Path $TbWork "stage")

switch ($p.Cmd) {
  "detect" {
    $isql = Fb-Tool $FbRoot "isql"
    $svcOk = $false
    if ($FbService) { $svcOk = [bool](Get-Service -Name $FbService -ErrorAction SilentlyContinue) }
    $ips = @(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "IPEnabled=True" -ErrorAction SilentlyContinue |
      ForEach-Object { $_.IPAddress } | Where-Object { $_ -and $_ -notmatch ':' })
    Result @{ hostname = $env:COMPUTERNAME; hostname_full = [System.Net.Dns]::GetHostName(); ips = $ips
              fb_conf_port = (Fb-ConfPort $FbRoot); fb_root_ok = [bool]$isql; fb_service = $FbService; fb_service_ok = $svcOk }
  }

  "install" {
    Load-Secrets
    if (-not (Fb-Tool $FbRoot "isql")) { Die "no Firebird in $FbRoot" }
    if (-not $FbService -or -not (Get-Service -Name $FbService -ErrorAction SilentlyContinue)) { Die "Firebird service '$FbService' not found; set firebird.service" }
    Fb-SavePristineConf $FbRoot
    if (Has $Components "fbagent") {
      if ($FbaMode -eq "install") {
        Fbagent-InstallLocal $Stage $FbaDir $FbRoot $FbPort $FbService $FbaPort $FbaInstance $FbaService
      } else {
        Log "fbagent mode 'existing': checking the agent on 127.0.0.1:$FbaPort"
        try { Fbagent-Check $FbaPort $FbaInstance $FbPort | Out-Null } catch { Die "existing fbagent check failed: $_" }
      }
    }
    if (Has $Components "node") {
      if (-not $DbRoot) { Die "--db-root is required for the node" }
      Node-Install $Stage $NodeDir $DbRoot
    }
    if (Has $Components "rcm") { Rcm-Install $Stage $RcmDir }
    # node.json / rcm.json hold the Firebird password: keep them only in place.
    foreach ($d in @("conf", "certs", "rcm-certs")) { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Stage $d) }
    Result @{ installed = $true }
  }

  { $_ -in @("uninstall", "wipe") } {
    # uninstall: the components asked for. wipe: everything the test bed puts
    # on a host -- load processes, rcm, node, and an fbagent it installed (an
    # 'existing' agent is kept). Both end by checking what is left.
    $wipe = $p.Cmd -eq "wipe"
    if ($wipe) {
      $Components = "fbagent,node,rcm"
      Stop-ProcessesUnder (Join-Path $TbWork "load")
      Stop-ProcessesUnder (Join-Path $TbWork "loadgen")
    }
    if (Has $Components "rcm") { Rcm-Uninstall $RcmDir }
    if (Has $Components "node") { Node-Uninstall $NodeDir }
    if ((Has $Components "fbagent") -and $FbaMode -eq "install") { Fbagent-Uninstall $FbaDir $FbaService }
    if ((Arg $A "restore_conf" "true") -eq "true") { Fb-RestorePristineConf $FbRoot $FbService }
    $left = @()
    if (Has $Components "rcm") { $left += Leftovers-Of "rcm" $RcmDir }
    if (Has $Components "node") { $left += Leftovers-Of "node" $NodeDir }
    if ((Has $Components "fbagent") -and $FbaMode -eq "install") { $left += Fbagent-Leftovers $FbaDir $FbaService }
    if ($wipe) { foreach ($x in Get-ProcessesUnder $TbWork) { $left += "work: process $($x.ProcessId) $($x.Name)" } }
    Report-Uninstall $left
  }

  "fbagent-info" {
    $cfgPath = Join-Path $FbaDir "agent_config.json"
    if (-not (Test-Path -LiteralPath $cfgPath)) { Die "no $cfgPath" }
    $cfg = Read-Json $cfgPath
    $api = $cfg.local_api
    $token = [string]$api.token
    if (-not $token) {
      $tf = Join-Path $FbaDir "local_api.token.txt"
      if (Test-Path -LiteralPath $tf) { $token = (Get-Content -LiteralPath $tf -Raw).Trim() }
    }
    Result @{ listen = [string]$api.listen; instance_id = [string]$api.instance_id; enabled = [string]$api.enabled }
    [Console]::Out.WriteLine("TBSECRET fbagent_token=$token")
  }

  default { Die "usage: 10-local.ps1 detect|install|uninstall|wipe|fbagent-info [--key value ...]" }
}
