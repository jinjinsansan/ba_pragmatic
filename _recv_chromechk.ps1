# Check whether the running Chrome has --remote-debugging-port=9222 + dedicated profile.
$ErrorActionPreference = 'SilentlyContinue'
# is :9222 actually listening?
try {
  $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 3 -UseBasicParsing
  Write-Output ('CDP :9222 = UP (' + $r.StatusCode + ')')
} catch { Write-Output 'CDP :9222 = DOWN (ECONNREFUSED)' }
Write-Output '--- Chrome command lines (debug-port? profile?) ---'
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  ForEach-Object {
    $cl = $_.CommandLine
    $dbg = if ($cl -match 'remote-debugging-port=(\d+)') { 'port=' + $matches[1] } else { 'NO-DEBUG-PORT' }
    $prof = if ($cl -match 'user-data-dir=([^"]+?)(?:\s--|\s*$|")') { 'profile=' + (Split-Path $matches[1] -Leaf) } else { 'default-profile' }
    Write-Output ('  pid=' + $_.ProcessId + ' ' + $dbg + ' ' + $prof)
  } | Select-Object -First 12
Write-Output '--- listening on 9222? (netstat) ---'
$n = netstat -ano | Select-String ':9222\s'
if ($n) { $n | ForEach-Object { Write-Output ('  ' + $_.Line.Trim()) } } else { Write-Output '  nothing listening on 9222' }
