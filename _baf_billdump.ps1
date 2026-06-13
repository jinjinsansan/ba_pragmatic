# Dump billing/session state JSON(s) — show account + balance + daily_open + pnl.
$ErrorActionPreference = 'SilentlyContinue'
$files = Get-ChildItem "$env:APPDATA\bacopy-copytrade-gui\profiles" -Recurse -Filter 'dual_line_billing_state.json'
foreach ($f in $files) {
  Write-Output ('=== ' + $f.FullName + ' ===')
  Get-Content -LiteralPath $f.FullName -Raw
  Write-Output ''
}
# also the daily_total / session-state cache if present
$ss = Get-ChildItem "$env:APPDATA\bacopy-copytrade-gui" -Recurse -Filter '*session*state*.json' -ErrorAction SilentlyContinue
foreach ($f in $ss) { Write-Output ('=== ' + $f.Name + ' ==='); Get-Content -LiteralPath $f.FullName -Raw; Write-Output '' }
