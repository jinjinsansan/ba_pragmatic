# Bounded watcher measuring decision->send latency per bet after the fast-send fix.
param([int]$Window = 420)
$ErrorActionPreference = 'SilentlyContinue'
$log = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
if (-not (Test-Path -LiteralPath $log)) { Write-Output 'LOG_NOT_FOUND'; exit 0 }
$pos0 = (Get-Item -LiteralPath $log).Length
Write-Output ("WATCH_START win=" + $Window + "s")
Start-Sleep -Seconds $Window
$fs = [System.IO.File]::Open($log,'Open','Read','ReadWrite')
$fs.Seek($pos0,'Begin') | Out-Null
$sr = New-Object System.IO.StreamReader($fs)
$new = $sr.ReadToEnd(); $sr.Close(); $fs.Close()
$lines = $new -split "`n"
$pat = 'TRY-BET\] executing|TRY-BET\] SENDING|defer center \(ws fast-send\)|side recomputed at send|FOLLOW\] side recomputed|NOW-BET-HOLD\] start|local bet settled|PHANTOM-GUARD'
$hits = $lines | Select-String -Pattern $pat
if (-not $hits) { Write-Output 'NO_BETS_IN_WINDOW'; exit 0 }
Write-Output ("MARKER_COUNT=" + @($hits).Count)
foreach ($h in ($hits | Select-Object -Last 60)) {
  $ln = $h.Line
  # keep only HH:mm:ss,fff + the tag of interest to stay compact
  if ($ln -match '(\d\d:\d\d:\d\d,\d\d\d).*?(\[(TRY-BET|FOLLOW|NOW-BET-HOLD|DECISION|PHANTOM-GUARD)\].*)$') {
    Write-Output ('  ' + $matches[1] + ' ' + $matches[2].Substring(0,[Math]::Min(160,$matches[2].Length)))
  } else {
    Write-Output ('  ' + $ln.Substring(0,[Math]::Min(180,$ln.Length)))
  }
}
