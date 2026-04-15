Write-Host "=== Today (2026-04-15) laplace_client entries ==="
Select-String -Path C:\dev\ba\agent.log -Pattern "^2026-04-15.*laplace_client" | Select-Object -First 30 | ForEach-Object { $_.Line }
Write-Host ""
Write-Host "=== Today total laplace_client count ==="
$count = (Select-String -Path C:\dev\ba\agent.log -Pattern "^2026-04-15.*laplace_client" | Measure-Object).Count
Write-Host "Count: $count"
Write-Host ""
Write-Host "=== Today Remote mode / LAPLACE Engine ==="
Select-String -Path C:\dev\ba\agent.log -Pattern "^2026-04-15.*(Remote mode|LAPLACE Engine|LAPLACE API health|api/sessions|api/decide|api/result|api/exit-check|SSH tunnel)" | Select-Object -First 30 | ForEach-Object { $_.Line }
Write-Host ""
Write-Host "=== Today ConnectionError or Connection refused ==="
Select-String -Path C:\dev\ba\agent.log -Pattern "^2026-04-15.*(ConnectionError|refused|timeout|Connection|urllib|HTTPError|requests)" | Select-Object -First 20 | ForEach-Object { $_.Line }
