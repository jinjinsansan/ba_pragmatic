# 決済時NOW-LOCK即解除(reason=settled)の発火確認用
$log = 'C:\Users\Administrator\AppData\Roaming\bacopy-copytrade-gui\logs\engine_cli_capture.log'
$tail = Get-Content $log -Tail 30000
$rel = $tail | Select-String -SimpleMatch '[NOW-LOCK] release'
$settled = @($rel | Where-Object { $_.Line -match 'reason=settled' })
$other = @($rel | Where-Object { $_.Line -notmatch 'reason=settled' })
Write-Output ("release reason=settled count: " + $settled.Count)
Write-Output ("release other reasons count: " + $other.Count)
Write-Output "--- last 5 settled releases ---"
$settled | Select-Object -Last 5 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
Write-Output "--- last 5 local-bet settles ---"
$tail | Select-String -SimpleMatch 'local bet settled' | Select-Object -Last 5 | ForEach-Object { $_.Line.Substring(0, [Math]::Min(160, $_.Line.Length)) }
