$ErrorActionPreference = "Stop"

# Build bacopy_engine.exe with PyInstaller for Electron extraResources.
#
# Output:
#   copytrade_gui/build_staging/engine/bacopy_engine.exe

$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OUTDIR = Join-Path $ROOT "copytrade_gui/build_staging/engine"
New-Item -ItemType Directory -Force -Path $OUTDIR | Out-Null

Push-Location $ROOT
try {
  python -m pip install --upgrade pip | Out-Null
  python -m pip install -r requirements.txt | Out-Null
  # Ensure camoufox Firefox binary is downloaded (required at runtime).
  python -m camoufox fetch | Out-Null

  $dist = Join-Path $ROOT "dist"
  $build = Join-Path $ROOT "build"
  if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
  if (Test-Path $build) { Remove-Item -Recurse -Force $build }

  python -m PyInstaller --noconfirm --clean --onefile `
    --name bacopy_engine `
    --collect-all camoufox `
    --collect-all browserforge `
    --collect-all apify_fingerprint_datapoints `
    --collect-all language_tags `
    --collect-submodules playwright `
    --hidden-import tzdata `
    --hidden-import dual_line_pragmatic_bot `
    --hidden-import dual_line_match `
    --hidden-import dual_line_live_executor `
    --hidden-import collector_pragmatic `
    --distpath (Join-Path $ROOT "copytrade_gui/build_staging/engine") `
    --workpath $build `
    --specpath $build `
    (Join-Path $ROOT "bacopy_engine.py")

  Write-Host "[ok] built: $OUTDIR\bacopy_engine.exe"
} finally {
  Pop-Location
}
