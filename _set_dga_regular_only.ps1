# Restrict dga live betting to regular (long-window ~60s) tables only — skip
# Speed/Turbo (fast ~13s window) where multi-chip placement can miss the window.
# Edge is table-type independent (verified), so this trades volume for reliable
# multi-chip landing. Reuses _is_fast_table_name in the engine.
$ErrorActionPreference = 'Stop'
$envFile = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\.env"
if (-not (Test-Path -LiteralPath $envFile)) { Write-Output "ABORT: .env missing"; exit 1 }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item -LiteralPath $envFile -Destination ($envFile + ".bak_" + $stamp) -Force
$k = "BACOPY_DGA_REGULAR_ONLY"
$lines = (Get-Content -LiteralPath $envFile) | Where-Object { $_ -notmatch "^\s*$k\s*=" }
$lines += "$k=1"
Set-Content -LiteralPath $envFile -Value $lines -Encoding ASCII
Write-Output ("BACKUP=" + $envFile + ".bak_" + $stamp)
Get-Content -LiteralPath $envFile | Where-Object { $_ -match "BACOPY_DGA_" } | ForEach-Object { Write-Output ("  " + $_) }
Write-Output "DGA_REGULAR_ONLY_SET_OK (GUI再起動で反映)"
