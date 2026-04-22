param(
  [int]$Count = 5,
  [int]$Start = 1,
  [string]$EmailDomain = 'beta.bacopy.local',
  [string]$Version = '0.1.0'
)

$ErrorActionPreference = 'Stop'
$ROOT = 'C:\bacopy'
$GUI  = Join-Path $ROOT 'copytrade_gui'
$OUT  = Join-Path $ROOT 'dist_per_user'

if (-not (Test-Path (Join-Path $ROOT 'support_keys\client_key'))) {
  Write-Error "support_keys\client_key not found in $ROOT. Transfer support_keys/ from local first."
}

New-Item -ItemType Directory -Force -Path $OUT | Out-Null

$report = New-Object System.Collections.Generic.List[object]

$sw = [System.Diagnostics.Stopwatch]::StartNew()

for ($i = $Start; $i -lt ($Start + $Count); $i++) {
  $slot  = "user{0:D2}" -f $i
  $email = "$slot@$EmailDomain"

  Write-Host ""
  Write-Host "=========================================="
  Write-Host "  Building [$slot] email=$email"
  Write-Host "=========================================="
  $stepSw = [System.Diagnostics.Stopwatch]::StartNew()

  # 1. Provision user-specific support_key + .env
  Push-Location $GUI
  try {
    node scripts/provision-user-build.js $email
  } finally {
    Pop-Location
  }

  # Read assigned port from build_meta.json
  $metaPath = Join-Path $GUI 'build_staging\build_meta.json'
  $meta = Get-Content $metaPath -Raw | ConvertFrom-Json
  $port = $meta.port
  Write-Host "  [$slot] assigned port: $port"

  # 2. Clean previous dist and build
  $distDir = Join-Path $GUI 'dist'
  if (Test-Path $distDir) {
    Remove-Item -Recurse -Force $distDir
  }

  Push-Location $GUI
  try {
    npm run build:installer
    if ($LASTEXITCODE -ne 0) { throw "npm run build:installer failed for $slot" }
  } finally {
    Pop-Location
  }

  # 3. Move + rename the installer into per-user folder
  $srcName  = "BACOPYRECEIVER Setup $Version.exe"
  $src      = Join-Path $distDir $srcName
  if (-not (Test-Path $src)) { throw "installer not found: $src" }

  $dstDir   = Join-Path $OUT $slot
  New-Item -ItemType Directory -Force -Path $dstDir | Out-Null

  $newName  = "BACOPYRECEIVER_${slot}_Setup_${Version}.exe"
  $dst      = Join-Path $dstDir $newName
  Move-Item -Force $src $dst

  # Copy blockmap if present
  $blk = "$src.blockmap"
  if (Test-Path $blk) {
    Copy-Item $blk (Join-Path $dstDir "$newName.blockmap") -Force
  }

  $size = [math]::Round((Get-Item $dst).Length / 1MB, 1)
  $stepSw.Stop()

  Write-Host "  [$slot] built $newName ($size MB) in $($stepSw.Elapsed.TotalSeconds.ToString('F1'))s"

  $report.Add([PSCustomObject]@{
    Slot    = $slot
    Email   = $email
    Port    = $port
    File    = $dst
    SizeMB  = $size
    BuiltAt = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  })
}

$sw.Stop()

Write-Host ""
Write-Host "=========================================="
Write-Host "  ALL BUILDS COMPLETE"
Write-Host "  total time: $($sw.Elapsed.TotalMinutes.ToString('F1')) min"
Write-Host "=========================================="

$reportCsv = Join-Path $OUT 'build_report.csv'
$report | Export-Csv -Path $reportCsv -Encoding UTF8 -NoTypeInformation
$report | Format-Table -AutoSize

Write-Host ""
Write-Host "report CSV: $reportCsv"
Write-Host "per-user folders:"
Get-ChildItem $OUT -Directory | Select-Object Name | Format-Table -AutoSize
