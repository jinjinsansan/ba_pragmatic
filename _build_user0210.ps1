# Build NSIS installers for user02-10. 2026-06-21 rebuild #2: engine = context-death
# self-recovery (CDP reconnect) + 2 NEW money modes (bet123set = 1-2-3 per 7-hand set,
# dalembertset = D'Alembert per 7-hand set) + Kelly/winrate. GUI asar = current src
# (winrate RANGE chart + the 2 new money-mode pulldowns/panels; SAFETY MODE commented out).
# provision bakes bc per user (user06=10/11). DISTRIBUTION_BUILD_RUNBOOK.md 3-A. Log=_build_user0210.log.
$ErrorActionPreference = 'Continue'
Set-Location 'E:\dev\Cusor\bacopy\copytrade_gui'
$log = 'E:\dev\Cusor\bacopy\_build_user0210.log'
function W($m){ "$((Get-Date).ToString('HH:mm:ss')) $m" | Out-File -FilePath $log -Append -Encoding utf8 }
"" | Out-File -FilePath $log -Encoding utf8
W "BUILD START (2026-06-25 #11: STABLE base + REVERSE-BET + ENGINE-HEARTBEAT badge REDESIGNED (相乗り install+全フレームping/60s+THRESH180s) + centering env cap + ★賭けChrome膨張バッジ (GUI右下にRAM色付き表示 緑<2GB/黄2-3GB/赤≥3GB再起動推奨・kill せず読むだけ決済非接触・BACOPY_CHROME_BLOAT_MONITOR=0 で無効). engine=bafather-verified 03AE23AE; GUI src has reverse toggle/banner + bloat badge)"
$eng = Get-Item 'build_staging\engine\bacopy_engine.exe'
W ("engine size=" + $eng.Length + " (expect 68733831 = STABLE+reverse+heartbeat-redesigned, bafather-verified MD5 03AE23AE452F5A96022A1BC455939BB4)")

$users = @('02','03','04','05','06','07','08','09','10')
foreach($n in $users){
  W "==== user$n : provision ===="
  node scripts/provision-user-build.js ("user$n@beta.bacopy.local") *>&1 | Out-File -FilePath $log -Append -Encoding utf8
  if($LASTEXITCODE -ne 0){ W "PROVISION_FAILED user$n exit=$LASTEXITCODE"; continue }

  $before = (Get-Date).AddSeconds(-2)
  W "==== user$n : electron-builder ===="
  npx electron-builder --win nsis --config.win.signAndEditExecutable=false *>&1 | Out-File -FilePath $log -Append -Encoding utf8

  $inst = Get-ChildItem dist -Filter '*Setup*.exe' -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -ge $before } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if($inst){
    $dst = Join-Path 'dist' ("BACOPYRECEIVER_user$n" + "_Setup.exe")
    Move-Item -LiteralPath $inst.FullName -Destination $dst -Force
    W ("INSTALLER_DONE user$n size=" + [math]::Round((Get-Item $dst).Length/1MB,1) + "MB name=" + (Split-Path $dst -Leaf))
  } else {
    W "NO_INSTALLER user$n"
  }
}
W "BATCH_NSIS_COMPLETE"
