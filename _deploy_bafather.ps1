# bafather (162.43.83.54) EXE + GUI デプロイスクリプト
# EXE: dual_line_live_executor.py / dual_line_pragmatic_bot.py など → PyInstaller ビルド → EXE コピー
# GUI: app.js 変更時は別途 _deploy_bafather_gui.ps1 を使う

$KEY    = "C:\Users\USER\.ssh\laplace_vps"
$BFHOST = "Administrator@162.43.83.54"
$SRC    = "E:\dev\Cusor\bacopy"
$DEST   = "C:\bacopy"

# ── 1. SCP Python ファイル ────────────────────────────────────────────
Write-Host "[1] SCP py files to bafather..."
$files = @(
    "dual_line_live_executor.py",
    "dual_line_pragmatic_bot.py",
    "dual_line_match.py",
    "dual_line_money.py",
    "dual_line_logic.py",
    "bacopy_executor_pragmatic_ws_live.py"
)
foreach ($f in $files) {
    scp -i $KEY "$SRC\$f" "${BFHOST}:${DEST}\$f"
    if ($LASTEXITCODE -ne 0) { Write-Error "SCP failed: $f"; exit 1 }
    Write-Host "  OK: $f"
}

# ── 2. エンジン停止 ───────────────────────────────────────────────────
Write-Host "[2] Kill engine..."
ssh -i $KEY $BFHOST "cmd /c taskkill /f /im bacopy_engine.exe"
Start-Sleep -Seconds 3

# ── 3. PyInstaller ビルド ─────────────────────────────────────────────
Write-Host "[3] Build EXE on bafather..."
$BUILD = "Set-Location C:\bacopy; python -m PyInstaller --noconfirm build\bacopy_engine.spec 2>&1 | Select-String 'Build complete|ERROR' | Select-Object -Last 5"
ssh -i $KEY $BFHOST "powershell -Command `"$BUILD`""
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
    Write-Error "Build may have failed (exit=$LASTEXITCODE). Check log on bafather."
    exit 1
}

# ── 4. EXE デプロイ ───────────────────────────────────────────────────
Write-Host "[4] Deploy EXE..."
$DEPLOY = "Copy-Item -Force 'C:\bacopy\dist\bacopy_engine.exe' 'C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\engine\bacopy_engine.exe'; Write-Host 'EXE OK'"
ssh -i $KEY $BFHOST "powershell -Command `"$DEPLOY`""

Write-Host "`n[Done] bafather EXE deploy complete. Restart GUI manually."
