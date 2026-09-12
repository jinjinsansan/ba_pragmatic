# ============================================================================
# ★★ 2026-09-12 実行停止ガード ★★
#
# このスクリプトは **リポジトリ直下の dual_line_*.py を VPS 本番へ上書き** する。
# だが直下版は「VPS本番でもengineビルド元でもない回帰版」である (DUAL_LINE_VARIANTS.md)。
#   dual_line_pragmatic_bot.py : 直下 334,502 B  vs  VPS本番 160,368 B
# そのまま流すとシグナル生成が変わる。過去に実際にこれで挙動が変わっている。
#
# さらに下の $REMOTE / $SRC は既に無効:
#   - 210.131.215.116 は 2026-08 に解約済み (Xserver が別契約者へ再割当する)
#   - E:\dev\Cusoracopy はもう存在しない (現在は V:\dev\Cusoracopy)
#
# 使うなら: 転送元を _vps_prod/laplace2/ に変え、$REMOTE を新VPSに変え、
#           下の 1 行を消してから。
# ============================================================================
Write-Error @"
_deploy_vps.ps1 は停止中です (2026-09-12)。

理由: 転送元がリポジトリ直下 = 回帰版であり、VPS本番 (_vps_prod/laplace2/) とは別物です。
      接続先 210.131.215.116 も解約済みです。

詳細と正しい転送元: DUAL_LINE_VARIANTS.md
"@
exit 1

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
