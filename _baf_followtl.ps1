# Timeline of the latest bet -> settle -> follow chain (diagnose missed last follow)
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 60000
$pat = 'FOLLOW|NOW-LOCK|local bet settled|SEND-BET|WS-BET-SEND|MISSED|discard|window|BETSOPEN-CONFIRM|rejected non-whitelist|DGA-VPS-SETTLE'
$hits = $tail | Select-String -Pattern $pat | Select-Object -Last 60
$hits | ForEach-Object { $_.Line.Substring(0, [Math]::Min(190, $_.Line.Length)) }
