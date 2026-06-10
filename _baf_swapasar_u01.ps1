# Swap app.asar for the C:\BACOPYRECEIVER_user01 install (bafather current layout)
$ErrorActionPreference = 'Stop'
$asar = "C:\BACOPYRECEIVER_user01\resources\app.asar"
$new  = "C:\bacopy\app.asar.new"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (-not (Test-Path -LiteralPath $new)) { Write-Output "ABORT: .new missing"; exit 1 }
$expected = (Get-Item -LiteralPath $new).Length
Write-Output ("NEW size=" + $expected)
Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Get-Process bacopy_engine -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
$gc = @(Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue).Count
Write-Output ("AFTER_STOP gui=" + $gc)
if ($gc -gt 0) { Write-Output "ABORT: GUI still running"; exit 1 }
$bak = $asar + ".bak_" + $stamp
Copy-Item -LiteralPath $asar -Destination $bak -Force
Write-Output ("BACKUP=" + $bak + " " + (Get-Item -LiteralPath $bak).Length)
Copy-Item -LiteralPath $new -Destination $asar -Force
$i = Get-Item -LiteralPath $asar
Write-Output ("DEPLOYED=" + $i.Length)
if ($i.Length -ne $expected) { Write-Output "WARN size mismatch"; exit 1 }
Write-Output "ASAR_SWAP_OK"
