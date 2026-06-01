# Idempotently set BACOPY_DGA_LOCAL_SIGNAL=shadow in bafather resources\.env
$ErrorActionPreference = 'Stop'
$envFile = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\.env"
if (-not (Test-Path -LiteralPath $envFile)) { Write-Output "ABORT: .env missing"; exit 1 }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item -LiteralPath $envFile -Destination ($envFile + ".bak_" + $stamp) -Force
$lines = Get-Content -LiteralPath $envFile
$key = "BACOPY_DGA_LOCAL_SIGNAL"
$kept = $lines | Where-Object { $_ -notmatch "^\s*$key\s*=" }
$kept += "$key=shadow"
Set-Content -LiteralPath $envFile -Value $kept -Encoding ASCII
Write-Output ("BACKUP=" + $envFile + ".bak_" + $stamp)
Write-Output "=== current dual-line env keys ==="
Get-Content -LiteralPath $envFile | Where-Object { $_ -match "BACOPY_DGA_LOCAL_SIGNAL|BACOPY_DUAL_MODE|BACOPY_CLICK_BET|BACOPY_MASTER_SETTLE" }
Write-Output "DGA_ENV_SET_OK"
