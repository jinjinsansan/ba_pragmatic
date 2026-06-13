$ErrorActionPreference = 'SilentlyContinue'
$cap = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $cap -Tail 12000
Write-Output ('engine=' + (Get-Item 'C:\BACOPYRECEIVER_user01\resources\engine\bacopy_engine.exe').Length)
# TICK-SLOW breakdown (the freeze culprit)
Write-Output '--- TICK-SLOW (freeze culprit) breakdown ---'
$ts = $tail | Select-String -Pattern 'TICK-SLOW\] (\S+) ([0-9.]+)s'
if (@($ts).Count -eq 0) { Write-Output '  (none)' } else {
  $ts | ForEach-Object { if ($_.Line -match 'TICK-SLOW\] (\S+) ([0-9.]+)s') { $matches[1] } } |
    Group-Object | Sort-Object Count -Descending | ForEach-Object { Write-Output ('  ' + $_.Count + '  ' + $_.Name) }
  $mx = ($ts | ForEach-Object { if ($_.Line -match 'TICK-SLOW\] \S+ ([0-9.]+)s') { [double]$matches[1] } } | Measure-Object -Maximum).Maximum
  Write-Output ('  max=' + $mx + 's')
}
# NOW / bet / settle / follow outcomes
Write-Output '--- bet/settle/follow activity (last 12) ---'
$tail | Select-String -Pattern 'NOW-LOCK\] start|SEND-BET\] called|WS-BET-SEND|local bet settled|FOLLOW\] (place|END)|BET予約を破棄|skipped_not_prepared|window未検出' |
  Where-Object { $_.Line -notmatch 'now_probe' } | Select-Object -Last 12 | ForEach-Object {
    if ($_.Line -match ' (\d\d:\d\d:\d\d),') { $ts2=$matches[1] } else { $ts2='?' }
    $b = ($_.Line -replace '^.*\] \[','[') -replace "bet_id=.*",'' -replace "did=.*",''
    Write-Output ("  $ts2  " + $b.Substring(0,[Math]::Min(62,$b.Length)))
  }
# missed bets (discarded / not prepared)
$missed = @($tail | Select-String -Pattern 'BET予約を破棄|skipped_not_prepared|window未検出|FOLLOW\] END reason=(timeout|miss)').Count
Write-Output ('--- missed/discarded bets in window: ' + $missed)
