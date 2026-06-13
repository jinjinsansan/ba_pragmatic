# Extract non-spam log lines containing a time substring (e.g. "22:08:0").
param([string]$T = '22:08:0')
$ErrorActionPreference = 'SilentlyContinue'
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
$spam = 'AUTO-PROBE|BETSOPEN|GAME-ID|BETSCLOSED'
$hits = Get-Content -LiteralPath $log -Tail 60000 | Where-Object { $_.Contains($T) }
$hits = $hits | Where-Object { $_ -notmatch $spam }
foreach ($h in $hits) { Write-Output $h }
