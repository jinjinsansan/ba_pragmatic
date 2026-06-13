# Show SENDING side vs settled side (and latency) for recent bets, no switch noise.
$ErrorActionPreference = 'SilentlyContinue'
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
$t = Get-Content -LiteralPath $log -Tail 8000
$pat = 'TRY-BET\] executing|TRY-BET\] SENDING|side recomputed|local bet settled|PHANTOM-GUARD|FOLLOW\] END'
$hits = $t | Select-String -Pattern $pat
foreach ($h in ($hits | Select-Object -Last 40)) {
  $ln = $h.Line
  if ($ln -match '(\d\d:\d\d:\d\d,\d\d\d)\s+\[(?:INFO|WARNING|ERROR)\]\s+(.*)$') {
    $msg = $matches[2]
    Write-Output ($matches[1] + '  ' + $msg.Substring(0,[Math]::Min(140,$msg.Length)))
  }
}
