$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("now=" + (Get-Date -Format "HH:mm:ss"))
Write-Output "--- DGA-BET-Q/PLACE/SETTLE 12:5x ---"
(Get-Content -LiteralPath $log) | Select-String -Pattern 'DGA-BET-Q|DGA-BET-PLACE|DGA-SETTLE|\[DGA-BET\]' |
  Where-Object { $_.Line -match '12:5[0-9]:' } |
  ForEach-Object { $ln=$_.Line; if($ln.Length -gt 170){$ln=$ln.Substring(0,170)}; Write-Output ("  " + $ln) }
Write-Output "--- click/chip/lpbet/confirm 12:53:3 - 12:56 ---"
(Get-Content -LiteralPath $log) |
  Select-String -Pattern 'TRY-BET|BETSOPEN-HIT|CLICK-BET-PLAN|CLICK-BET|CHIP-PRESELECT|click=[0-9]/|locator|mouse.click|lpbet|trusted_confirm|BET送信確定|BET-FAILED|LPBET-DECIDE|partial|部分|FOCUS. intent' |
  Where-Object { $_.Line -match '12:5(3:[3-9]|4:|5:|6:[0-2])' } |
  ForEach-Object { $ln=$_.Line; if($ln.Length -gt 185){$ln=$ln.Substring(0,185)}; Write-Output ("  " + $ln) }
Write-Output "=== BET 1254 DONE ==="
