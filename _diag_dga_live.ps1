$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss") + " logmod=" + (Get-Item -LiteralPath $log).LastWriteTime.ToString("HH:mm:ss"))
Write-Output "--- DGA-DIRECT (last 3) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-DIRECT') | Select-Object -Last 3 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- DGA-STAT (last 2) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-STAT') | Select-Object -Last 2 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- DGA-BET-Q / PLACE / SETTLE (last 25) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-BET-Q|DGA-BET-PLACE|DGA-SETTLE|\[DGA-BET\]') | Select-Object -Last 25 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 180){$ln=$ln.Substring(0,180)}; Write-Output ("  " + $ln) }
Write-Output "--- CLICK-BET / lpbet / confirm (last 12) ---"
(Select-String -LiteralPath $log -Pattern 'CLICK-BET-JS|lpbet|trusted_confirm|BET送信確定|bet send FAILED|部分BET') | Select-Object -Last 12 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 180){$ln=$ln.Substring(0,180)}; Write-Output ("  " + $ln) }
Write-Output "=== DGA LIVE DIAG DONE ==="
