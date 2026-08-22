Write-Host "=== Port 8000 listeners ==="
Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue | Select LocalAddress,OwningProcess | Format-Table
Write-Host "=== laplace_api / uvicorn processes ==="
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'laplace_api|uvicorn' } | Select ProcessId,CommandLine | Format-List
Write-Host "=== SSH tunnels ==="
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ssh.*8000' } | Select ProcessId,CommandLine | Format-List
Write-Host "=== agent.log tail ==="
if (Test-Path C:\dev\ba\agent.log) {
    Get-Content C:\dev\ba\agent.log -Tail 20
}
