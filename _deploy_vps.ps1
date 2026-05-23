# VPS (210.131.215.116) dual-line bot デプロイスクリプト
# 対象: dual_line_*.py (ロジック/マネー/マッチ/ボット)
# 実行後: 旧プロセスを kill → 20秒後に自動再起動

$KEY  = "C:\Users\USER\.ssh\laplace_vps"
$REMOTE = "root@210.131.215.116"
$SRC  = "E:\dev\Cusor\bacopy"
$DEST = "/opt/laplace2"
$API_DEST = "/opt/bacopy"

# ── 1. Python ファイル SCP ────────────────────────────────────────────
Write-Host "[1] SCP dual_line py files..."
$files = @(
    "dual_line_pragmatic_bot.py",
    "dual_line_match.py",
    "dual_line_money.py",
    "dual_line_logic.py"
)
foreach ($f in $files) {
    scp -i $KEY "$SRC\$f" "${REMOTE}:${DEST}/$f"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "SCP failed: $f"
        exit 1
    }
    Write-Host "  OK: $f"
}

# The API serves /api/preposition to the GUI and must use the same six-pattern logic.
Write-Host "[1b] SCP API preposition files..."
$apiFiles = @("bacopy_api.py", "dual_line_match.py", "dual_line_logic.py")
foreach ($f in $apiFiles) {
    scp -i $KEY "$SRC\$f" "${REMOTE}:${API_DEST}/$f"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "SCP failed for API: $f"
        exit 1
    }
    Write-Host "  OK API: $f"
}
Write-Host "  NOTE: restart bacopy_api.py after reviewing active GUI/BET sessions."

# ── 2. pycache クリア (古い .pyc が残ると新コードが反映されない場合がある) ──
Write-Host "[2] Clear pycache..."
ssh -i $KEY $REMOTE "rm -f ${DEST}/__pycache__/dual_line_*.pyc ${API_DEST}/__pycache__/dual_line_*.pyc 2>/dev/null; echo pycache cleared"

# ── 3. 旧ボットプロセスを kill (ループスクリプトが 20s 後に自動再起動) ──
Write-Host "[3] Kill running bot process..."
ssh -i $KEY $REMOTE @'
LOCK=/tmp/dual_line_bot.lock
if [ -f $LOCK ]; then
    OLD_PID=$(cat $LOCK)
    if kill -0 $OLD_PID 2>/dev/null; then
        CHILD=$(pgrep -P $OLD_PID python 2>/dev/null | head -1)
        if [ -n "$CHILD" ]; then
            kill $CHILD && echo "killed python pid $CHILD"
        else
            echo "no python child found under pid $OLD_PID"
        fi
    else
        echo "lock exists but process $OLD_PID is gone"
    fi
else
    pkill -f dual_line_pragmatic_bot.py && echo "killed by name" || echo "no process found"
fi
'@

# ── 4. 20秒待機して起動確認 ──────────────────────────────────────────
Write-Host "[4] Waiting 22s for restart..."
Start-Sleep -Seconds 22
Write-Host "[5] Checking log..."
ssh -i $KEY $REMOTE "tail -15 ${DEST}/dual_line_bot_service.log"

Write-Host "`n[Done] VPS deploy complete."
