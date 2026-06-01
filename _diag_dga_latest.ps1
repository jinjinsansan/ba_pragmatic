$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
Write-Output "--- LAST 6 DGA-DIRECT (full file) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-DIRECT') | Select-Object -Last 6 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- LAST 10 DGA-STAT (full file) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-STAT') | Select-Object -Last 10 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- LAST 12 DGA-SIGNAL (full file) ---"
(Select-String -LiteralPath $log -Pattern 'DGA-SIGNAL') | Select-Object -Last 12 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "=== DGA LATEST DONE ==="
