# Module 05 (Windows): check that HQbird/Firebird is ready for the test bed.
#
#   05-dbms.ps1 check --fb-root DIR --fb-service NAME [--port 3050]
#
# The HQbird Windows installer is interactive, so this module does not
# install Firebird; it checks the service and the SYSDBA login.
. (Join-Path $PSScriptRoot "common.ps1")
$TbName = "05-dbms"
$p = Parse-TbArgs $args
$A = $p.Args

switch ($p.Cmd) {
  { $_ -in @("check", "install") } {
    Need-Admin
    Load-Secrets
    $root = Arg $A "fb_root" "C:\HQbird\Firebird50"
    $svc = Arg $A "fb_service"
    $isql = Fb-Tool $root "isql"
    if (-not $isql) { Die "no Firebird in $root (install HQbird first)" }
    if (-not $svc -or -not (Get-Service -Name $svc -ErrorAction SilentlyContinue)) { Die "Firebird service '$svc' not found" }
    if ((Get-Service -Name $svc).Status -ne "Running") { Start-Service -Name $svc }
    $tmp = [IO.Path]::GetTempFileName()
    [IO.File]::WriteAllText($tmp, "select 1 from rdb`$database;`n")
    $db = "localhost/" + (Arg $A "port" "3050") + ":employee"
    $r = Invoke-Native $isql @("-q", "-i", $tmp, $db) -Quiet
    Remove-Item -LiteralPath $tmp -Force
    Result @{ fb_service = $svc; login = ($r.Code -eq 0) }
    if ($r.Code -ne 0) { Die "SYSDBA login failed: $($r.Out)" }
  }
  default { Die "usage: 05-dbms.ps1 check [--key value ...]" }
}
