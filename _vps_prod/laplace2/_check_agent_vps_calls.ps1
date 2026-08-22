Write-Host "=== agent.log last 100 lines ==="
Get-Content C:\dev\ba\agent.log -Tail 100
Write-Host ""
Write-Host "=== Error/Warning lines from agent.log (today) ==="
Select-String -Path C:\dev\ba\agent.log -Pattern "ERROR|WARNING|Exception|Traceback|127\.0\.0\.1|api/sessions|api/decide|api/result|api/exit|SSH|tunnel" | Select-Object -Last 50 | ForEach-Object { $_.Line }
Write-Host ""
Write-Host "=== laplace_client related ==="
Select-String -Path C:\dev\ba\agent.log -Pattern "laplace_client|LAPLACE|Remote mode|API=http" | Select-Object -Last 30 | ForEach-Object { $_.Line }
