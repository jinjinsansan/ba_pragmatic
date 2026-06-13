# Identify which Electron process is bloated (renderer vs main) and the engine parent.
$ErrorActionPreference = 'SilentlyContinue'
Write-Output "--- BACOPYRECEIVER processes (type + parent + WS) ---"
Get-CimInstance Win32_Process -Filter "Name='BACOPYRECEIVER.exe'" |
  ForEach-Object {
    $cl = $_.CommandLine
    $type = if ($cl -match '--type=([a-zA-Z-]+)') { $matches[1] } else { 'MAIN' }
    $ws = [math]::Round((Get-Process -Id $_.ProcessId).WorkingSet64/1MB)
    Write-Output ("  pid=" + $_.ProcessId + " ppid=" + $_.ParentProcessId + " type=" + $type + " WS=" + $ws + "MB")
  }
Write-Output "--- engine parent (is it child of MAIN, independent of renderer?) ---"
Get-CimInstance Win32_Process -Filter "Name='bacopy_engine.exe'" |
  ForEach-Object { Write-Output ("  engine pid=" + $_.ProcessId + " ppid=" + $_.ParentProcessId) }
