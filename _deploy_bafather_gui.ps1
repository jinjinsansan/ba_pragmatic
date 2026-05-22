# bafather GUI (app.asar) デプロイスクリプト
# 前提: copytrade_gui/src/renderer/app.js または index.html を変更した後に実行
# ※ GUI を停止してから実行すること (app.asar がロックされている場合は上書き不可)

$KEY    = "C:\Users\USER\.ssh\laplace_vps"
$BFHOST = "Administrator@162.43.83.54"
$GUI_SRC = "E:\dev\Cusor\bacopy\copytrade_gui"

# ── 1. ローカルで Electron ビルド ─────────────────────────────────────
Write-Host "[1] Building Electron app locally..."
Push-Location $GUI_SRC
npx electron-builder --dir --config.win.signAndEditExecutable=false 2>&1 | Select-Object -Last 5
Pop-Location

$ASAR = "$GUI_SRC\dist\win-unpacked\resources\app.asar"
if (-not (Test-Path $ASAR)) {
    Write-Error "app.asar not found: $ASAR"
    exit 1
}
Write-Host "  Built: $ASAR"

# ── 2. bafather に SCP ────────────────────────────────────────────────
Write-Host "[2] SCP app.asar to bafather..."
$DEST_ASAR = "C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\app.asar"
scp -i $KEY $ASAR "${BFHOST}:${DEST_ASAR}"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Direct copy failed (GUI may be running). Copying to temp path instead..."
    scp -i $KEY $ASAR "${BFHOST}:C:\bacopy\app.asar.new"
    Write-Host ""
    Write-Host "*** GUI を停止してから以下を実行してください: ***"
    Write-Host "  Copy-Item -Force 'C:\bacopy\app.asar.new' '$DEST_ASAR'"
} else {
    Write-Host "  OK: app.asar deployed"
    Write-Host "[Done] GUI deploy complete. Restart GUI on bafather."
}
