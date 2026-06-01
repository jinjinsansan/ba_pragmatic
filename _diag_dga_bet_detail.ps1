param([string]$ts = "12:14:2")
$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"
Write-Output ("detail window prefix=" + $ts)
# Pull a broad window around the bet and show click/chip/focus/betsopen lines
$pats = @($ts, ($ts + "9"))
(Get-Content -LiteralPath $log) |
  Select-String -Pattern 'CLICK-BET|CHIP|chip|\[FOCUS\]|SCROLL-PROBE|scroll|betsopen|BETSOPEN|send_bet|SEND-BET|TRY-BET|geometry|lpbet|partial|部分|amount=\$' |
  Where-Object { $_.Line -match ('12:1(4:[2-5]|5:[0-4])') -and $_.Line -match 'spto43|TRY-BET|CLICK-BET|CHIP|chip|FOCUS|SCROLL|MANUAL-ASSIST|geometry' } |
  Select-Object -First 45 |
  ForEach-Object { $ln = $_.Line; if ($ln.Length -gt 185) { $ln = $ln.Substring(0,185) }; Write-Output ("  " + $ln) }
Write-Output "=== BET DETAIL DONE ==="
