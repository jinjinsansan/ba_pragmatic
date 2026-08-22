#!/bin/bash
# Auto-restart loop wrapper for dual_line_pragmatic_bot
# ws-watchdog の exit(1) や Python SystemExit で bot が落ちても自動復帰
cd /opt/laplace2
while true; do
  echo "[$(date '+%F %T')] starting bot" | tee -a /opt/laplace2/dual_line_pragmatic_bot.stdout.log
  PYTHONPATH=/opt/laplace2 /opt/laplace/.venv/bin/python /opt/laplace2/dual_line_pragmatic_bot.py --headless --profile /opt/laplace2/monitor/pragmatic_profile 2>&1 | tee -a /opt/laplace2/dual_line_pragmatic_bot.stdout.log
  EXIT_CODE=$?
  echo "[$(date '+%F %T')] bot exited (code=$EXIT_CODE), cleanup + sleep 10s..." | tee -a /opt/laplace2/dual_line_pragmatic_bot.stdout.log
  pkill -9 -f "camoufox-bin.*pragmatic" 2>/dev/null
  rm -f /opt/laplace2/monitor/pragmatic_profile/.parentlock 2>/dev/null
  sleep 10
done
