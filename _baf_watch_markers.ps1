# Bounded watcher: tails the engine log for ~WINDOW seconds capturing only the
# bet/guard/follow markers appended after start. Exits on its own (no orphan tail).
param([int]$Window = 240)
$ErrorActionPreference = 'SilentlyContinue'
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
if (-not (Test-Path -LiteralPath $log)) { Write-Output 'LOG_NOT_FOUND'; exit 0 }
$pos0 = (Get-Item -LiteralPath $log).Length
Write-Output ("WATCH_START win=" + $Window + "s pos0=" + $pos0)
Start-Sleep -Seconds $Window
$fs = [System.IO.File]::Open($log,'Open','Read','ReadWrite')
$fs.Seek($pos0,'Begin') | Out-Null
$sr = New-Object System.IO.StreamReader($fs)
$new = $sr.ReadToEnd()
$sr.Close(); $fs.Close()
$lines = $new -split "`n"
Write-Output ("NEW_LINES=" + $lines.Count + " NEW_BYTES=" + $new.Length)
$pat = 'PHANTOM-GUARD|FOLLOW\] side recomputed|TRY-BET\] follow side recomputed|BET-QUEUED|LIVE\] bet sent OK|local bet settled|DECIDE.*NOW|NOW-LOCK|phantom_no_server_confirm|\[ERROR\]|Traceback'
$hits = $lines | Select-String -Pattern $pat
if (-not $hits) { Write-Output 'NO_MARKERS_IN_WINDOW (feed-only, no bets fired)'; exit 0 }
Write-Output ("MARKER_COUNT=" + @($hits).Count)
$hits | Select-Object -Last 40 | ForEach-Object { Write-Output ("  " + $_.Line) }
