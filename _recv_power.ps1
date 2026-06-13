# Was the 8h stop a reboot / sleep / manual close? Check boot time + power events.
$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
Write-Output ("LastBootUpTime = " + $os.LastBootUpTime)
Write-Output ("Now            = " + (Get-Date))
Write-Output "--- Power/sleep/boot events (System log, last 12h) ---"
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddHours(-12)} -ErrorAction SilentlyContinue |
  Where-Object { $_.Id -in 1,12,13,42,107,1074,6005,6006,6008,41,109 } |
  Sort-Object TimeCreated |
  ForEach-Object {
    $msg = ($_.Message -split "`n")[0]
    Write-Output ("  " + $_.TimeCreated.ToString('MM-dd HH:mm:ss') + " id=" + $_.Id + " : " + $msg.Substring(0,[Math]::Min(90,$msg.Length)))
  }
Write-Output "--- Kernel-Power / sleep-wake (Id 42=sleep,107=wake,1=wake) ---"
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; StartTime=(Get-Date).AddHours(-12)} -ErrorAction SilentlyContinue |
  Sort-Object TimeCreated |
  ForEach-Object { Write-Output ("  " + $_.TimeCreated.ToString('MM-dd HH:mm:ss') + " id=" + $_.Id) } | Select-Object -First 20
