# Check: lpbet-fast confirm working (instant confirm, fast settle, follow in window)
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 60000
Write-Output "--- LPBET-FAST confirms (last 8) ---"
$tail | Select-String -SimpleMatch 'LPBET-FAST' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- confirm types used (last 8 BET-CONFIRMED) ---"
$tail | Select-String -SimpleMatch 'BET-CONFIRMED' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- dga winner vs settle pairs (last 12) ---"
$tail | Select-String -Pattern 'DGA-VPS-SETTLE|local bet settled' | Select-Object -Last 12 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(180, $_.Line.Length)) }
Write-Output "--- follow / TIE push (last 8) ---"
$tail | Select-String -Pattern '\[FOLLOW\]|\[CHAIN\] TIE push' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(180, $_.Line.Length)) }
Write-Output "--- missed/failed bets (last 6) ---"
$tail | Select-String -Pattern 'MISSED|BET-FAILED|discard' | Select-Object -Last 6 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
