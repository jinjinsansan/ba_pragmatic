# Print the latest bet-path lines (compact) to diagnose timing/miss right now.
$ErrorActionPreference = 'SilentlyContinue'
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
$t = Get-Content -LiteralPath $log -Tail 4000
$pat = 'TRY-BET\] executing|TRY-BET\] SENDING|defer center|side recomputed|TRY-BET-DEFER|skipped_not_prepared|TRY-BET-PREPARE|NOW-BET-HOLD\] start|local bet settled|PHANTOM-GUARD|DROP stale|signal age=|BET-QUEUED\] (side=|immediate|no active|betsopen active)|FOLLOW\] WIN chase|FOLLOW\] END|request_switch|perform_switch'
$hits = $t | Select-String -Pattern $pat
foreach ($h in ($hits | Select-Object -Last 50)) {
  $ln = $h.Line
  if ($ln -match '(\d\d:\d\d:\d\d,\d\d\d)\s+\[(?:INFO|WARNING|ERROR)\]\s+(.*)$') {
    $msg = $matches[2]
    Write-Output ($matches[1] + '  ' + $msg.Substring(0,[Math]::Min(150,$msg.Length)))
  }
}
