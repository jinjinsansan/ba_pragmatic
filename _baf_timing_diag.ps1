# Diagnose slow-flash / follow-timing: log bloat, AUTO-PROBE rate, and per-bet latency.
$ErrorActionPreference = 'SilentlyContinue'
$dir = "$env:APPDATA\bacopy-copytrade-gui\logs"
$log = Join-Path $dir 'engine_cli_capture.log'
Write-Output '=== LOG FILES (rotation check) ==='
Get-ChildItem $dir -Filter 'engine_cli_capture*' | Sort-Object LastWriteTime -Descending |
  ForEach-Object { Write-Output ("  " + $_.Name + "  " + [math]::Round($_.Length/1MB,1) + "MB  " + $_.LastWriteTime.ToString('HH:mm:ss')) }

$tail = Get-Content -LiteralPath $log -Tail 6000
Write-Output ''
Write-Output '=== AUTO-PROBE spam rate (last 6000 lines) ==='
$ap = ($tail | Select-String 'AUTO-PROBE').Count
$first = ($tail | Select-Object -First 1)
$last  = ($tail | Select-Object -Last 1)
Write-Output ("  AUTO-PROBE lines=" + $ap + " of 6000")

Write-Output ''
Write-Output '=== Recent follow recompute events (last 6000) ==='
$tail | Select-String 'side recomputed at send' | Select-Object -Last 8 | ForEach-Object { Write-Output ("  " + $_.Line) }

Write-Output ''
Write-Output '=== Latency: BET-QUEUED -> bet sent OK / try-bet (last bets) ==='
# Pull queued+sent+lpbet lines with timestamps
$ev = $tail | Select-String 'BET-QUEUED\] side=|LIVE\] bet sent OK|TRY-BET\] follow side|lpbet=|firing _try_execute_bet|bet_window_open='
$ev | Select-Object -Last 30 | ForEach-Object { Write-Output ("  " + $_.Line) }

Write-Output ''
Write-Output '=== NOW-LOCK start -> first BET-QUEUED gap (proxy for flash->send) ==='
$tail | Select-String 'NOW-LOCK\] start did=|firing _try_execute_bet immediately|no active betsopen' | Select-Object -Last 20 | ForEach-Object { Write-Output ("  " + $_.Line) }
