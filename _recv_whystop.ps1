# Why did the GUI/engine stop? Process state + last log activity + crash markers.
$ErrorActionPreference = 'SilentlyContinue'
Write-Output ('GUI=' + @(Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue).Count +
              ' ENGINE=' + @(Get-Process bacopy_engine -ErrorAction SilentlyContinue).Count +
              ' CHROME=' + @(Get-Process chrome -ErrorAction SilentlyContinue).Count)
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
if (Test-Path $log) {
  $i = Get-Item $log
  $age = [math]::Round(((Get-Date) - $i.LastWriteTime).TotalMinutes, 1)
  Write-Output ("ENGINE_LOG last=" + $i.LastWriteTime.ToString('MM-dd HH:mm:ss') + " (" + $age + " min ago) size=" + [math]::Round($i.Length/1MB,1) + "MB")
  Write-Output "--- LAST 25 LINES (stop reason) ---"
  Get-Content -LiteralPath $log -Tail 25
  Write-Output "--- crash/fatal markers in last 3000 lines ---"
  Get-Content -LiteralPath $log -Tail 3000 | Select-String -Pattern 'FATAL|Traceback|CRASH|ECONNREFUSED|9222|connect_over_cdp|exiting|SystemExit|MemoryError|killed|session.*expired|customer support|長時間' | Select-Object -Last 12 | ForEach-Object { Write-Output ("  " + $_.Line.Substring(0,[Math]::Min(160,$_.Line.Length))) }
}
# GUI main process log (electron)
$mlog = Get-ChildItem "$env:APPDATA\bacopy-copytrade-gui\logs" -Filter "main*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($mlog) {
  Write-Output ("--- MAIN LOG " + $mlog.Name + " last=" + $mlog.LastWriteTime.ToString('MM-dd HH:mm:ss') + " ---")
  Get-Content -LiteralPath $mlog.FullName -Tail 12
}
# Windows app error events (crash)
Write-Output "--- recent Application errors (WER/.NET/app crash) ---"
Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2; StartTime=(Get-Date).AddHours(-12)} -MaxEvents 8 -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'BACOPY|bacopy|electron|chrome|engine' } |
  ForEach-Object { Write-Output ("  " + $_.TimeCreated.ToString('MM-dd HH:mm') + " " + ($_.Message -split "`n")[0].Substring(0,[Math]::Min(140,(($_.Message -split "`n")[0]).Length))) }
