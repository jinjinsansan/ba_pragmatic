# Batch-build NSIS installers for user07-10 with the bc-fix engine (2026-06-15)
# Engine staged at build_staging/engine/bacopy_engine.exe must be fd694c36 (bc env-capable).
Set-Location E:\dev\Cusor\bacopy\copytrade_gui
$users = @('07','08','09','10')
foreach($n in $users){
  Write-Output ("==== user" + $n + " ====")
  node scripts/provision-user-build.js ("user" + $n + "@beta.bacopy.local") 2>&1 | Where-Object { $_ -match 'allocated|reused|reach|port' }
  if($LASTEXITCODE -ne 0){ Write-Output ("PROVISION_FAILED user" + $n); break }
  $before = (Get-Date).AddSeconds(-2)
  npx electron-builder --win nsis --config.win.signAndEditExecutable=false 2>&1 | Select-Object -Last 3
  $inst = Get-ChildItem dist -Filter '*Setup*.exe' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $before } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if($inst){
    $outdir = Join-Path '..\dist_per_user' ("user" + $n)
    New-Item -ItemType Directory -Force -Path $outdir | Out-Null
    $dst = Join-Path $outdir ("BACOPYRECEIVER_user" + $n + "_Setup.exe")
    Move-Item -LiteralPath $inst.FullName -Destination $dst -Force
    Write-Output ("INSTALLER_DONE user" + $n + " size=" + [math]::Round((Get-Item $dst).Length/1MB,1) + "MB path=" + $dst)
  } else { Write-Output ("NO_INSTALLER user" + $n); break }
}
Write-Output "BATCH_0710_COMPLETE"
