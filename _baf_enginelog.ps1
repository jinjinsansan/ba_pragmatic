# Locate the newest engine_cli_capture.log and print health + bundle markers.
$ErrorActionPreference = 'SilentlyContinue'
$roots = @("$env:APPDATA", "$env:LOCALAPPDATA", "C:\BACOPYRECEIVER_user01")
$log = $null
foreach ($r in $roots) {
  $f = Get-ChildItem $r -Recurse -Filter 'engine_cli_capture.log' -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($f) { $log = $f; break }
}
if (-not $log) { Write-Output 'LOG_NOT_FOUND'; exit 0 }
$i = Get-Item $log.FullName
Write-Output ("LOG=" + $i.FullName)
Write-Output ("SIZE=" + [math]::Round($i.Length/1MB,2) + "MB MTIME=" + $i.LastWriteTime.ToString('HH:mm:ss'))
Write-Output '--- TAIL 40 ---'
Get-Content -LiteralPath $i.FullName -Tail 40
Write-Output '--- BUNDLE MARKERS (last 2000 lines) ---'
$recent = Get-Content -LiteralPath $i.FullName -Tail 2000
($recent | Select-String -Pattern 'PHANTOM-GUARD','FOLLOW] side recomputed','BET-QUEUED','LIVE] bet sent OK','SETTLED','ERROR','Traceback' |
  Select-Object -Last 20) | ForEach-Object { Write-Output ("  " + $_.Line) }
