$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
Write-Output "--- DGA-DIRECT connected (last 2) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-DIRECT. connected') | Select-Object -Last 2 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- PREPOS candidate / skip fast (last 16) ---"
(Select-String -LiteralPath $log -Pattern 'PREPOS. candidate|PREPOS. skip fast|PREPOS. ignored') | Select-Object -Last 16 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 150){$ln=$ln.Substring(0,150)}; Write-Output ("  " + $ln) }
Write-Output "--- DGA-BET-Q / PLACE / SETTLE / skip fast (last 12) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-BET-Q|DGA-BET-PLACE|DGA-SETTLE|skip fast') | Select-Object -Last 12 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 160){$ln=$ln.Substring(0,160)}; Write-Output ("  " + $ln) }
Write-Output "=== PREPOS REG DONE ==="
