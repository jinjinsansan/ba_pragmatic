# Comprehensive receiver health snapshot.
$ErrorActionPreference = 'SilentlyContinue'
$gui = @(Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue)
$eng = @(Get-Process bacopy_engine -ErrorAction SilentlyContinue)
$chr = @(Get-Process chrome -ErrorAction SilentlyContinue)
$maxWs = if ($gui.Count) { [math]::Round((($gui | Measure-Object WorkingSet64 -Maximum).Maximum)/1MB) } else { 0 }
Write-Output ("PROC gui=" + $gui.Count + " engine=" + $eng.Count + " chrome=" + $chr.Count + " guiMaxWS=" + $maxWs + "MB")
# :9222 up?
$cdp = 'DOWN'
try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) { $cdp = 'UP' } } catch {}
Write-Output ("CDP9222 " + $cdp)
# installed build
$base = "$env:LOCALAPPDATA\Programs\bacopy-copytrade-gui\resources"
if (-not (Test-Path $base)) { $base = "C:\BACOPYRECEIVER_user01\resources" }  # bafather layout
$em = (Get-FileHash "$base\engine\bacopy_engine.exe" -Algorithm MD5).Hash
$am = (Get-FileHash "$base\app.asar" -Algorithm MD5).Hash
Write-Output ("BUILD engine=" + $em.Substring(0,8) + " asar=" + $am.Substring(0,8))
# engine log freshness + crash signals
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
if (Test-Path $log) {
  $i = Get-Item $log
  $age = [math]::Round(((Get-Date) - $i.LastWriteTime).TotalMinutes, 1)
  $tail = Get-Content -LiteralPath $log -Tail 2000
  $econn = @($tail | Select-String 'ECONNREFUSED').Count
  $fatal = @($tail | Select-String 'run\(\) fatal').Count
  Write-Output ("ENGLOG age=" + $age + "min size=" + [math]::Round($i.Length/1MB,1) + "MB ECONNREFUSED=" + $econn + " fatal=" + $fatal)
  # last billing/balance + pnl
  $bl = ($tail | Select-String 'BILLING\] state loaded' | Select-Object -Last 1)
  if ($bl) { Write-Output ("BILLING " + ($bl.Line -replace '.*\[BILLING\] ','').Substring(0,[Math]::Min(70,(($bl.Line -replace '.*\[BILLING\] ','')).Length))) }
} else { Write-Output "ENGLOG none" }
# watchdog running? (main log)
$mlog = "$env:APPDATA\bacopy-copytrade-gui\logs\main_cli_capture.log"
if (Test-Path $mlog) {
  $wd = @(Get-Content -LiteralPath $mlog -Tail 3000 | Select-String 'cdp-watchdog\] started').Count
  Write-Output ("WATCHDOG started_logs=" + $wd)
}
