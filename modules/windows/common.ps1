# Shared helpers for the Windows modules. Dot-sourced, not run.
#
# Every module runs ON the test bed host in an elevated Windows PowerShell 5.1.
# The work folder is the parent of the modules folder (default C:\hqtb).
# Secrets come from <work>\secrets.env (KEY=VALUE lines), written by tb.py
# or by hand; its ACL allows Administrators and SYSTEM only.
#
# Arguments: every module takes "<command> --key value ...", the same as the
# Linux modules, so tb.py builds one command line for both.
#
# Output: Log/Result write straight to the console, never to the pipeline,
# so they cannot leak into a function's return value.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$TbModules = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $TbModules) { $TbModules = $PSScriptRoot }
$TbWork = if ($env:TB_WORK) { $env:TB_WORK } else { Split-Path -Parent $TbModules }
$TbSecrets = if ($env:TB_SECRETS) { $env:TB_SECRETS } else { Join-Path $TbWork "secrets.env" }
$TbName = "module"

function Log([string]$m) { [Console]::Out.WriteLine("[$TbName] $m") }
function Warn([string]$m) { [Console]::Error.WriteLine("[$TbName] WARN: $m") }
function Die([string]$m) { [Console]::Error.WriteLine("[$TbName] ERROR: $m"); exit 1 }
function Result($obj) { [Console]::Out.WriteLine("TBRESULT " + (ConvertTo-Json $obj -Compress -Depth 30)) }

function Parse-TbArgs([object[]]$a) {
  $cmd = ""
  $map = @{}
  $i = 0
  if ($a.Count -gt 0 -and -not ([string]$a[0]).StartsWith("--")) { $cmd = [string]$a[0]; $i = 1 }
  while ($i -lt $a.Count) {
    $k = [string]$a[$i]
    if (-not $k.StartsWith("--")) { Die "unexpected argument: $k" }
    if ($i + 1 -ge $a.Count) { Die "missing value for $k" }
    $map[$k.Substring(2).Replace("-", "_")] = [string]$a[$i + 1]
    $i += 2
  }
  return @{ Cmd = $cmd; Args = $map }
}

function Arg([hashtable]$m, [string]$name, [string]$default = "") {
  if ($m.ContainsKey($name) -and $m[$name] -ne "") { return $m[$name] }
  return $default
}

function Has([string]$list, [string]$item) { return ("," + $list + ",").Contains("," + $item + ",") }

function Need-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Die "run elevated (Administrator)" }
}

# Load secrets and export the Firebird client variables (isql, gfix, nbackup
# read ISC_USER / ISC_PASSWORD, so the password is not on a command line).
function Load-Secrets {
  if (Test-Path -LiteralPath $TbSecrets) {
    foreach ($line in Get-Content -LiteralPath $TbSecrets) {
      $t = $line.Trim()
      if ($t -eq "" -or $t.StartsWith("#")) { continue }
      $eq = $t.IndexOf("=")
      if ($eq -lt 1) { continue }
      Set-Item -Path ("env:" + $t.Substring(0, $eq)) -Value $t.Substring($eq + 1)
    }
  }
  if (-not $env:TB_FB_USER) { $env:TB_FB_USER = "SYSDBA" }
  if (-not $env:TB_FB_PASSWORD) { Die "TB_FB_PASSWORD is not set (secrets file $TbSecrets)" }
  $env:ISC_USER = $env:TB_FB_USER
  $env:ISC_PASSWORD = $env:TB_FB_PASSWORD
}

function Secure-File([string]$path) {
  & icacls.exe $path /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
}

# Run a native program; returns @{ Code; Out } with stdout+stderr as text.
# PS 5.1 turns native stderr into errors under "Stop", hence the switch.
function Invoke-Native([string]$exe, [string[]]$argv, [switch]$Quiet) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $exe @argv 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
  } finally { $ErrorActionPreference = $old }
  $text = ($out -join "`n")
  if (-not $Quiet -and $text) { [Console]::Out.WriteLine($text) }
  return @{ Code = $code; Out = $text }
}

function Fb-Tool([string]$root, [string]$name) {
  foreach ($p in @((Join-Path $root "$name.exe"), (Join-Path $root "bin\$name.exe"))) {
    if (Test-Path -LiteralPath $p) { return $p }
  }
  return ""
}

function Wait-Until([int]$seconds, [scriptblock]$cond) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $end) {
    try { if (& $cond) { return $true } } catch { }
    Start-Sleep -Seconds 2
  }
  return $false
}

function Read-Json([string]$path) { return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json) }

function Write-JsonFile([string]$path, $obj) {
  $json = ConvertTo-Json $obj -Depth 20
  [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Set-JsonProp($obj, [string]$name, $value) {
  if ($obj.PSObject.Properties[$name]) { $obj.$name = $value }
  else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value }
}

# HTTPS download that trusts the server ONLY by its SPKI SHA-256 pin (the
# goafts bootstrap pin). The certificate chain itself is not trusted.
$TbPinnedSource = @"
using System;
using System.IO;
using System.Net;
using System.Security.Cryptography;
public static class TbPinned {
  static int Len(byte[] b, ref int i) {
    int l = b[i++]; if (l < 0x80) return l;
    int n = l & 0x7f; l = 0;
    for (int k = 0; k < n; k++) l = (l << 8) | b[i++];
    return l;
  }
  static void Tlv(byte[] b, int at, out int content, out int end) {
    int i = at + 1; int l = Len(b, ref i); content = i; end = i + l;
  }
  public static string SpkiSha256(byte[] der) {
    int c, e;
    Tlv(der, 0, out c, out e);            // Certificate
    Tlv(der, c, out c, out e);            // TBSCertificate
    int p = c;
    if (der[p] == 0xA0) { Tlv(der, p, out c, out e); p = e; }       // [0] version
    for (int k = 0; k < 5; k++) { Tlv(der, p, out c, out e); p = e; } // serial..subject
    Tlv(der, p, out c, out e);            // SubjectPublicKeyInfo
    byte[] spki = new byte[e - p]; Array.Copy(der, p, spki, 0, e - p);
    using (SHA256 sha = SHA256.Create())
      return BitConverter.ToString(sha.ComputeHash(spki)).Replace("-", "").ToLowerInvariant();
  }
  public static string Seen = "";
  public static void Download(string url, string pin, string outFile) {
    ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
    HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
    string want = pin.ToLowerInvariant();
    req.ServerCertificateValidationCallback = (s, cert, chain, errs) => {
      Seen = SpkiSha256(cert.GetRawCertData()); return Seen == want; };
    req.Timeout = 600000; req.ReadWriteTimeout = 600000;
    using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
    using (Stream rs = resp.GetResponseStream())
    using (FileStream fs = File.Create(outFile)) { rs.CopyTo(fs); }
  }
}
"@

function Invoke-PinnedDownload([string]$url, [string]$pin, [string]$outFile) {
  if (-not ("TbPinned" -as [type])) { Add-Type -TypeDefinition $TbPinnedSource -Language CSharp }
  try { [TbPinned]::Download($url, $pin, $outFile) }
  catch {
    if ([TbPinned]::Seen -and [TbPinned]::Seen -ne $pin.ToLowerInvariant()) {
      Die "server pin mismatch: server SPKI is $([TbPinned]::Seen)"
    }
    Die "download failed: $url : $($_.Exception.Message)"
  }
}

# Start a process OUTSIDE the ssh session, so it keeps running after tb.py
# disconnects (Windows OpenSSH ends the processes of a closed session).
function Start-Detached([string]$commandLine, [string]$dir) {
  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
    -Arguments @{ CommandLine = $commandLine; CurrentDirectory = $dir }
  if ($r.ReturnValue -ne 0) { Die "Win32_Process.Create failed ($($r.ReturnValue)): $commandLine" }
  return [int]$r.ProcessId
}

function Stop-Tree([int]$processId) {
  Invoke-Native "taskkill.exe" @("/T", "/F", "/PID", "$processId") -Quiet | Out-Null
}
