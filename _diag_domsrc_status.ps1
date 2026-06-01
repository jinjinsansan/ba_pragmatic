$ErrorActionPreference = 'SilentlyContinue'
$res = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources"
$log = Join-Path $res "engine\dual_line_pragmatic_bot.log"

Write-Output ("now=" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))

Write-Output "--- processes ---"
Get-Process | Where-Object { $_.ProcessName -match 'bacopy|chrome' } |
  Select-Object ProcessName, Id | Sort-Object ProcessName |
  ForEach-Object { Write-Output ("  " + $_.ProcessName + " pid=" + $_.Id) }

Write-Output "--- CDP tabs (9222) ---"
try {
  $t = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json" -UseBasicParsing -TimeoutSec 6
  $tabs = $t.Content | ConvertFrom-Json
  foreach ($tab in @($tabs)) {
    $u = [string]$tab.url
    if ($u.Length -gt 95) { $u = $u.Substring(0,95) }
    Write-Output ("  type=" + $tab.type + " url=" + $u)
  }
} catch { Write-Output ("  CDP error: " + $_.Exception.Message) }

Write-Output "--- bot log last-modified ---"
if (Test-Path -LiteralPath $log) {
  $fi = Get-Item -LiteralPath $log
  Write-Output ("  " + $fi.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss") + "  size=" + $fi.Length)
} else {
  Write-Output "  (bot log not found)"
}

Write-Output "--- recent FOCUS / multi-area / NOW / TRY-BET (last 12) ---"
if (Test-Path -LiteralPath $log) {
  $L = Get-Content -LiteralPath $log -Tail 4000
  ($L | Select-String -Pattern 'FOCUS|MULTI-AREA|multibaccarat|\[TRY-BET\]|MANUAL-ASSIST. NOW') |
    Select-Object -Last 12 |
    ForEach-Object { $ln = $_.Line; if ($ln.Length -gt 170) { $ln = $ln.Substring(0,170) }; Write-Output ("  " + $ln) }
}

Write-Output "--- DGA env in resources\.env ---"
$envf = Join-Path $res ".env"
if (Test-Path -LiteralPath $envf) {
  (Get-Content -LiteralPath $envf | Select-String -Pattern 'DGA|DOM_RESULT|DUAL_MODE') |
    ForEach-Object { Write-Output ("  " + $_.Line) }
}
Write-Output "=== DOMSRC STATUS DONE ==="
