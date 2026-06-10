# Check: bline|telecho|B decisions accepted (not rejected) after V4 set update
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 40000
Write-Output "--- rejected non-whitelist (last 5; should stop appearing after restart) ---"
$tail | Select-String -SimpleMatch 'rejected non-whitelist' | Select-Object -Last 5 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
Write-Output "--- bline decisions handled (last 8) ---"
$tail | Select-String -SimpleMatch 'bline|telecho|B' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
Write-Output "--- NOW-LOCK start / SEND-BET (last 8) ---"
$tail | Select-String -Pattern '\[NOW-LOCK\] start|\[SEND-BET\] called' | Select-Object -Last 8 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
