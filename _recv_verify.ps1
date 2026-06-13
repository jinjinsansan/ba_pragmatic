# Verify a receiver after installer swap: engine version/MD5, asar fix, processes, engine log.
$ErrorActionPreference = 'SilentlyContinue'
Write-Output '--- INSTALLED ENGINE ---'
$roots = @("$env:LOCALAPPDATA\Programs","$env:PROGRAMFILES","${env:PROGRAMFILES(X86)}","C:\")
$eng = $null
foreach ($r in $roots) {
  $f = Get-ChildItem $r -Recurse -Filter 'bacopy_engine.exe' -ErrorAction SilentlyContinue |
       Where-Object { $_.FullName -notlike '*\bacopy\*' } |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($f) { $eng = $f; break }
}
if ($eng) {
  $h = (Get-FileHash $eng.FullName -Algorithm MD5).Hash
  Write-Output ('  PATH=' + $eng.FullName)
  Write-Output ('  SIZE=' + $eng.Length + ' MD5=' + $h + ' MTIME=' + $eng.LastWriteTime.ToString('MM-dd HH:mm'))
  Write-Output ('  EXPECT SIZE=68708941 MD5=BF69C5DCE53B0A3CA2095FF02DDBD5D4')
  if ($h -eq 'BF69C5DCE53B0A3CA2095FF02DDBD5D4') { Write-Output '  ENGINE_MATCH=YES (fixed build)' } else { Write-Output '  ENGINE_MATCH=NO' }
  $asar = Join-Path (Split-Path (Split-Path $eng.FullName)) 'app.asar'
  if (Test-Path $asar) {
    $txt = [System.IO.File]::ReadAllText($asar)
    if ($txt.Contains('followLine')) { Write-Output '  ASAR_followLine=PRESENT (old asar!)' } else { Write-Output '  ASAR_followLine=absent (new asar OK)' }
  }
} else { Write-Output '  ENGINE_NOT_FOUND' }
Write-Output '--- PROCESSES ---'
$g = Get-Process BACOPYRECEIVER -ErrorAction SilentlyContinue
Write-Output ('  GUI_COUNT=' + @($g).Count)
foreach ($q in $g) { Write-Output ('    pid=' + $q.Id + ' start=' + $q.StartTime.ToString('MM-dd HH:mm') + ' WS=' + [math]::Round($q.WorkingSet64/1MB) + 'MB') }
Write-Output ('  ENGINE_COUNT=' + @(Get-Process bacopy_engine -ErrorAction SilentlyContinue).Count)
Write-Output ('  CHROME_COUNT=' + @(Get-Process chrome -ErrorAction SilentlyContinue).Count)
Write-Output '--- ENGINE LOG ---'
$l = "$env:APPDATA\bacopy-copytrade-gui\logs\engine_cli_capture.log"
if (Test-Path $l) {
  $i = Get-Item $l
  Write-Output ('  mtime=' + $i.LastWriteTime.ToString('MM-dd HH:mm:ss') + ' age_sec=' + [math]::Round(((Get-Date)-$i.LastWriteTime).TotalSeconds) + ' size=' + [math]::Round($i.Length/1MB,1) + 'MB')
} else { Write-Output '  NO_LOG' }
