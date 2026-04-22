$ErrorActionPreference = "Stop"

# Copy the camoufox Firefox cache from %LOCALAPPDATA%\camoufox into the Electron
# build_staging directory so it can be shipped inside the NSIS installer as
# extraResources/camoufox_firefox. On first launch Electron restores this tree
# into each end-user's %LOCALAPPDATA%\camoufox so the bot runs offline.

$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SRC  = Join-Path $env:LOCALAPPDATA "camoufox"
$DEST = Join-Path $ROOT "copytrade_gui\build_staging\camoufox_firefox"

if (-not (Test-Path $SRC)) {
  Write-Error "[copy_camoufox_assets] source not found: $SRC -- run 'python -m camoufox fetch' first"
}

if (Test-Path $DEST) {
  Write-Host "[copy_camoufox_assets] cleaning old dest: $DEST"
  Remove-Item -Recurse -Force $DEST
}

New-Item -ItemType Directory -Force -Path $DEST | Out-Null
Write-Host "[copy_camoufox_assets] copying $SRC -> $DEST (this may take a minute)"
Copy-Item -Path (Join-Path $SRC "*") -Destination $DEST -Recurse -Force

$size = (Get-ChildItem $DEST -Recurse -File | Measure-Object -Property Length -Sum).Sum
$mb   = [math]::Round($size / 1MB, 1)
Write-Host "[copy_camoufox_assets] done. $mb MB staged under $DEST"
