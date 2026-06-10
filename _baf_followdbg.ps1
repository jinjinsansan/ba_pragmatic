# Detail: 18:03-18:06 follow chain - pattern_key, sides, outcomes, confirm timing
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 80000
Write-Output "--- dl_v4_544eed all lines ---"
$tail | Select-String -SimpleMatch 'dl_v4_544eed' | Select-Object -First 12 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(190, $_.Line.Length)) }
Write-Output "--- FOLLOW lines 18:03-18:06 ---"
$tail | Select-String -SimpleMatch '[FOLLOW]' | Where-Object { $_.Line -match ' 18:0[3-6]:' } | ForEach-Object { $_.Line.Substring(0, [Math]::Min(190, $_.Line.Length)) }
Write-Output "--- local bet settled full lines 18:03-18:06 ---"
$tail | Select-String -SimpleMatch 'local bet settled' | Where-Object { $_.Line -match ' 18:0[3-6]:' } | ForEach-Object { $_.Line.Substring(0, [Math]::Min(220, $_.Line.Length)) }
Write-Output "--- WS bet confirm lines 18:03-18:06 ---"
$tail | Select-String -Pattern 'lpbet|GAME-BET-CONFIRM|stake_delta|BET-CONFIRM' | Where-Object { $_.Line -match ' 18:0[3-6]:' } | Select-Object -First 20 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(190, $_.Line.Length)) }
