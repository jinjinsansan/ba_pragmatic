$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
(Get-Content -LiteralPath $log) |
  Select-String -Pattern 'TRY-BET|BETSOPEN-HIT|PREPARE-NOW|CLICK-BET-PLAN|CLICK-BET|CHIP-PRESELECT|click=[0-9]/|locator|mouse.click|lpbet|LPBET|CONFIRM-PROBE|BET-FAILED|FOCUS. intent|skip place|not prepared|VERIFY' |
  Where-Object { $_.Line -match '13:3(0:5|1:[0-2])' } |
  Select-Object -First 50 |
  ForEach-Object { $ln=$_.Line; if($ln.Length -gt 180){$ln=$ln.Substring(0,180)}; Write-Output ("  " + $ln) }
Write-Output "=== 1331 DONE ==="
