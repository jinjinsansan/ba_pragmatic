# Speed up multi-chip placement: skip the slow locator.click path (which timed
# out 1800ms + 2500ms landed-wait on the first chip) and go straight to the fast
# mouse.click(geometry) path for every chip. Cuts ~5s off the first click → the
# multi-chip sequence fits the Fast-table window. No rebuild; GUI restart applies.
$ErrorActionPreference = 'Stop'
$envFile = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\.env"
if (-not (Test-Path -LiteralPath $envFile)) { Write-Output "ABORT: .env missing"; exit 1 }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item -LiteralPath $envFile -Destination ($envFile + ".bak_" + $stamp) -Force
$lines = Get-Content -LiteralPath $envFile
$set = @{
  "BACOPY_CLICK_BET_USE_LOCATOR" = "0"      # mouse-first (skip locator timeout)
  "BACOPY_CLICK_BET_INTER_CLICK_MS" = "250" # tighter spacing between chip clicks
}
foreach ($k in $set.Keys) {
  $lines = $lines | Where-Object { $_ -notmatch "^\s*$k\s*=" }
  $lines += ("$k=" + $set[$k])
}
Set-Content -LiteralPath $envFile -Value $lines -Encoding ASCII
Write-Output ("BACKUP=" + $envFile + ".bak_" + $stamp)
Get-Content -LiteralPath $envFile | Where-Object { $_ -match "BACOPY_CLICK_BET|BACOPY_DGA_LOCAL_SIGNAL" } | ForEach-Object { Write-Output ("  " + $_) }
Write-Output "CLICK_FAST_ENV_SET_OK (GUI再起動で反映)"
