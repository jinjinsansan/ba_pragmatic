# Diagnose: assist panel silent while Telegram shows v4 signals
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 40000
Write-Output "--- engine start / mode lines (last 5) ---"
$tail | Select-String -Pattern 'dual_mode|DUAL-MODE|mode=v' | Select-Object -Last 5 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- preposition activity (last 8) ---"
$tail | Select-String -SimpleMatch 'PREPOS' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- decision received (last 8) ---"
$tail | Select-String -Pattern '\[DECISION\] (received|new|handling)|_handle_decision|SKIP duplicate' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- API errors / slow (last 8) ---"
$tail | Select-String -Pattern 'API-SLOW|API\] GET .* failed|API\] POST .* failed|HTTPError|URLError|timed out' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- manual assist overlay (last 8) ---"
$tail | Select-String -Pattern 'ASSIST|manual_assist_overlay|YELLOW' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(170, $_.Line.Length)) }
Write-Output "--- last 3 log lines (engine alive?) ---"
$tail | Select-Object -Last 3 | ForEach-Object { $_.Substring(0, [Math]::Min(170, $_.Length)) }
