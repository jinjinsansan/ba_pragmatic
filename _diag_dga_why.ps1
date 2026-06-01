$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
Write-Output "--- .env DGA lines ---"
(Get-Content -LiteralPath (Join-Path $res ".env")) | Where-Object { $_ -match 'DGA' } | ForEach-Object { Write-Output ("  " + $_) }
$L = Get-Content -LiteralPath $log -Tail 20000
Write-Output "--- 'executor context injected' / 'executor setup' (last 6) ---"
($L | Select-String -Pattern 'executor context injected|executor setup|executor setup failed') | Select-Object -Last 6 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- any line containing 'dga' or 'DGA' (last 12) ---"
($L | Select-String -Pattern 'dga|DGA') | Select-Object -Last 12 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 160){$ln=$ln.Substring(0,160)}; Write-Output ("  " + $ln) }
Write-Output "--- 'dual_mode' / DUAL_MODE startup (last 4) ---"
($L | Select-String -Pattern 'dual_mode|DUAL_MODE|PATTERN MODE|mode=v') | Select-Object -Last 4 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- WS-EVENT / WS-FILTER dga (last 6) ---"
($L | Select-String -Pattern 'WS-FILTER. .dga|/dga') | Select-Object -Last 6 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 160){$ln=$ln.Substring(0,160)}; Write-Output ("  " + $ln) }
Write-Output "=== DGA WHY DONE ==="
