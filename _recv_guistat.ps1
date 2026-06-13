# Diagnose receiver GUI black-screen: process count, start time, memory, instances.
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue
if (-not $p) { Write-Output 'GUI_NOT_RUNNING'; }
else {
  Write-Output ('GUI_PROC_COUNT=' + @($p).Count)
  $tot = 0
  foreach ($q in $p) {
    $st = $q.StartTime
    $ws = [math]::Round($q.WorkingSet64/1MB,0)
    $tot += $q.WorkingSet64
    Write-Output ('  pid=' + $q.Id + ' start=' + $st.ToString('MM-dd HH:mm') + ' WS=' + $ws + 'MB')
  }
  Write-Output ('GUI_TOTAL_WS_MB=' + [math]::Round($tot/1MB,0))
}
$e = Get-Process bacopy_engine -ErrorAction SilentlyContinue
Write-Output ('ENGINE_COUNT=' + @($e).Count)
$c = Get-Process chrome -ErrorAction SilentlyContinue
Write-Output ('CHROME_COUNT=' + @($c).Count)
# GPU cache size (bloat indicator)
$gpu = "$env:APPDATA\bacopy-copytrade-gui\GPUCache"
if (Test-Path $gpu) {
  $sz = (Get-ChildItem $gpu -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
  Write-Output ('GPUCACHE_MB=' + [math]::Round($sz/1MB,0))
}
