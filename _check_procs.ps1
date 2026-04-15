Get-Process | Where-Object { $_.Name -match 'python|electron|node' } | Select Name,Id,StartTime | Format-Table -AutoSize
Write-Host "---cmdline of pythons---"
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|node|electron' } | Select ProcessId,Name,CommandLine | Format-List
