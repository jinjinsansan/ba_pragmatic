# Set BACOPY_DGA_LOCAL_SIGNAL=live in bafather resources\.env (Phase 2b: dga drives real bets)
$ErrorActionPreference = 'Stop'
$envFile = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\.env"
if (-not (Test-Path -LiteralPath $envFile)) { Write-Output "ABORT: .env missing"; exit 1 }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item -LiteralPath $envFile -Destination ($envFile + ".bak_" + $stamp) -Force
$key = "BACOPY_DGA_LOCAL_SIGNAL"
$kept = (Get-Content -LiteralPath $envFile) | Where-Object { $_ -notmatch "^\s*$key\s*=" }
$kept += "$key=live"
Set-Content -LiteralPath $envFile -Value $kept -Encoding ASCII
Write-Output ("BACKUP=" + $envFile + ".bak_" + $stamp)
Get-Content -LiteralPath $envFile | Where-Object { $_ -match "BACOPY_DGA_LOCAL_SIGNAL|BACOPY_DUAL_MODE" } | ForEach-Object { Write-Output ("  " + $_) }
Write-Output "DGA_LIVE_ENV_SET_OK (GUI再起動で反映 / dga が実BETを駆動)"
