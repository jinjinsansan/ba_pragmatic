#!/bin/bash
cd /opt/laplace2
set -a
source /opt/bacopy/.env 2>/dev/null || true
set +a
PROFILE_DIR=/opt/bacopy/data/camoufox_profile_dual_line
LOCK=/tmp/dual_line_bot.lock

# 既存プロセスのチェック
if [ -f "$LOCK" ]; then
    OLD_PID=$(cat "$LOCK")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Already running (PID $OLD_PID), exiting."
        exit 0
    fi
fi

echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# 凍結ウォッチドッグ起動(singleton lockで重複回避)
nohup /opt/laplace/.venv/bin/python /opt/laplace2/dual_line_freeze_watchdog.py >> /opt/laplace2/dual_line_freeze_watchdog.log 2>&1 &

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting dual_line_pragmatic_bot (dual_line profile)..."
    rm -f "$PROFILE_DIR/.parentlock" 2>/dev/null || true
    /opt/laplace/.venv/bin/python dual_line_pragmatic_bot.py         --headless         --profile "$PROFILE_DIR"         --cookies /opt/laplace2/monitor/auth_state_pragmatic_collector/stake_cookies.json        
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot exited, restarting in 20s..."
    sleep 20
done
