# Show the engine-log lines right before the 00:10 UTC (09:10 JST) silent stop.
$ErrorActionPreference = 'SilentlyContinue'
$l = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
$hits = Select-String -LiteralPath $l -Pattern 'T00:0[6-9]:|T00:10:' -AllMatches | Select-Object -Last 20
foreach ($h in $hits) {
  $ln = $h.Line
  if ($ln -match '(\d\d:\d\d:\d\d,\d\d\d)\s+\[(\w+)\]\s+(.*)$') {
    Write-Output ($matches[1] + ' [' + $matches[2] + '] ' + $matches[3].Substring(0,[Math]::Min(95,$matches[3].Length)))
  } else {
    Write-Output $ln.Substring(0,[Math]::Min(110,$ln.Length))
  }
}
