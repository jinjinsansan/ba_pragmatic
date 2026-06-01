$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
$L = Get-Content -LiteralPath $log -Tail 6000
Write-Output "--- lines mentioning dl_vps / dl_v4 / NOW signal (last 25) ---"
($L | Select-String -Pattern 'dl_vps|dl_v4|signal.*side=|NOW.*side|side=.*table=') |
  Select-Object -Last 25 |
  ForEach-Object { $ln = $_.Line; if ($ln.Length -gt 180) { $ln = $ln.Substring(0,180) }; Write-Output ("  " + $ln) }
Write-Output "=== NOW FORMAT DONE ==="
