$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
Write-Output "--- last 14 DGA-SIGNAL ---"
(Select-String -LiteralPath $log -Pattern 'DGA-SIGNAL') | Select-Object -Last 14 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 150){$ln=$ln.Substring(0,150)}; Write-Output ("  " + $ln) }
Write-Output "--- last 20 DGA-BET-Q / PLACE / SETTLE / skip ---"
(Select-String -LiteralPath $log -Pattern 'DGA-BET-Q|DGA-BET-PLACE|DGA-SETTLE|\[DGA-BET\] skip') | Select-Object -Last 20 | ForEach-Object { $ln=$_.Line; if($ln.Length -gt 165){$ln=$ln.Substring(0,165)}; Write-Output ("  " + $ln) }
Write-Output "=== RECENT NOW DONE ==="
