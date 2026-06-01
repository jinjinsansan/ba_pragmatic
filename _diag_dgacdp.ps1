$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
Write-Output "--- processes ---"
Get-Process | Where-Object { $_.ProcessName -match 'bacopy_engine|BACOPYRECEIVER' } |
  Select-Object ProcessName, Id | ForEach-Object { Write-Output ("  " + $_.ProcessName + " pid=" + $_.Id) }
Write-Output "--- bot log last-modified ---"
if (Test-Path -LiteralPath $log) {
  $fi = Get-Item -LiteralPath $log
  Write-Output ("  " + $fi.LastWriteTime.ToString("HH:mm:ss") + " size=" + $fi.Length)
}
$L = Get-Content -LiteralPath $log -Tail 9000
Write-Output "--- [DGA-CDP] (last 8) ---"
($L | Select-String -Pattern 'DGA-CDP') | Select-Object -Last 8 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- [DGA-LOCAL]/callback register (last 4) ---"
($L | Select-String -Pattern 'DGA-LOCAL|callback registered') | Select-Object -Last 4 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- [DGA-STAT] (last 6) ---"
($L | Select-String -Pattern 'DGA-STAT') | Select-Object -Last 6 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "--- [DGA-SIGNAL] (last 15) ---"
($L | Select-String -Pattern 'DGA-SIGNAL') | Select-Object -Last 15 | ForEach-Object { Write-Output ("  " + $_.Line) }
Write-Output "=== DGA-CDP DIAG DONE ==="
