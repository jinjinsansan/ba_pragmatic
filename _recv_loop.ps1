# Is the engine in a crash-restart loop, or recovered? Count cycles + watchdog activity.
$ErrorActionPreference = 'SilentlyContinue'
Get-Process bacopy_engine -ErrorAction SilentlyContinue | Sort-Object StartTime |
  ForEach-Object { Write-Output ('engine pid=' + $_.Id + ' start=' + $_.StartTime.ToString('HH:mm:ss') + ' WS=' + [math]::Round($_.WorkingSet64/1MB) + 'MB') }
$elog = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
$tail = Get-Content -LiteralPath $elog -Tail 4000
Write-Output ('ECONNREFUSED in last 4000 lines: ' + @($tail | Select-String 'ECONNREFUSED').Count)
Write-Output ('engine INIT (AUTO-PROBE init) count: ' + @($tail | Select-String 'AUTO-PROBE\] init').Count)
Write-Output '--- timestamps of each engine INIT (= each restart) ---'
$tail | Select-String 'AUTO-PROBE\] init' | ForEach-Object { if ($_.Line -match '(\d\d:\d\d:\d\d)') { Write-Output ('  ' + $matches[1]) } }
$mlog = "$env:APPDATA\bacopy-copytrade-gui\logs\main_cli_capture.log"
Write-Output '--- main log: cdp-watchdog + JS errors (last 4000 lines) ---'
Get-Content -LiteralPath $mlog -Tail 4000 | Select-String 'cdp-watchdog|Auto-restart|relaunch|Uncaught|TypeError|ReferenceError|is not defined|is not a function' | Select-Object -Last 15 | ForEach-Object { Write-Output ('  ' + $_.Line.Substring(0,[Math]::Min(150,$_.Line.Length))) }
