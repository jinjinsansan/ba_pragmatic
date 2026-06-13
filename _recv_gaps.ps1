# Detect crash/restart: process start times, engine-log time gaps, traceback context.
$ErrorActionPreference = 'SilentlyContinue'
Write-Output "--- process start times (recent start = crashed & restarted) ---"
Get-Process BACOPYRECEIVER,bacopy_engine -ErrorAction SilentlyContinue |
  Sort-Object StartTime |
  ForEach-Object { Write-Output ("  " + $_.ProcessName + " pid=" + $_.Id + " start=" + $_.StartTime.ToString('MM-dd HH:mm:ss') + " WS=" + [math]::Round($_.WorkingSet64/1MB) + "MB") }

$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
Write-Output "--- engine-log time gaps > 90s in last ~20000 lines (= stalls/stops) ---"
$prev = $null; $prevline = ''
$lines = Get-Content -LiteralPath $log -Tail 20000
foreach ($ln in $lines) {
  if ($ln -match '^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)') {
    $t = [datetime]::ParseExact($matches[1] + ' ' + $matches[2], 'yyyy-MM-dd HH:mm:ss', $null)
    if ($prev -ne $null) {
      $gap = ($t - $prev).TotalSeconds
      if ($gap -gt 90) {
        Write-Output ("  GAP " + [math]::Round($gap) + "s : " + $prev.ToString('HH:mm:ss') + " -> " + $t.ToString('HH:mm:ss'))
      }
    }
    $prev = $t
  }
}
Write-Output "--- Traceback context (last occurrence, 6 lines) ---"
$idx = -1
for ($k=0; $k -lt $lines.Count; $k++) { if ($lines[$k] -match 'Traceback') { $idx = $k } }
if ($idx -ge 0) { for ($k=$idx-1; $k -le [math]::Min($idx+5,$lines.Count-1); $k++) { Write-Output ("  " + $lines[$k].Substring(0,[Math]::Min(150,$lines[$k].Length))) } }
else { Write-Output "  (no traceback in window)" }
