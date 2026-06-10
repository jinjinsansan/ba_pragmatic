# Check: WS skip-focus working (skip fired, TICK-SLOW _perform_switch ~gone, settle latency)
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 60000
Write-Output "--- skip focus (ws transport) count ---"
@($tail | Select-String -SimpleMatch 'skip focus (ws transport)').Count
Write-Output "--- _focus_table_in_multi executed count (should be ~0) ---"
@($tail | Select-String -SimpleMatch '[SWITCH] _focus_table_in_multi result').Count
Write-Output "--- TICK-SLOW lines (should be near zero) ---"
$tail | Select-String -SimpleMatch 'TICK-SLOW' | Select-Object -Last 6 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
Write-Output "--- DGA winner -> settle latency samples (last 6 of each) ---"
$tail | Select-String -Pattern 'DGA-VPS-SETTLE|local bet settled|NOW-LOCK\] release' | Select-Object -Last 12 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
Write-Output "--- missed/failed bets ---"
$tail | Select-String -Pattern 'MISSED|BET-FAILED|discard' | Select-Object -Last 6 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
