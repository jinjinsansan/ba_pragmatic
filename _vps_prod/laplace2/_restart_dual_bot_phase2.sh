#!/bin/bash
# Phase2 enable: restart dual_line bot wrapper so it re-sources .env (BACOPY_V4_PUBLISH=1)
# and relaunches the patched bot. Self-match-safe: this script's invocation cmdline
# does NOT contain the bot patterns, and all pgrep/pkill use the [x]bracket trick.
set +e

echo "=== BEFORE ==="
pgrep -af '[r]un_dual_line_bot.sh' || echo "wrapper:none"
pgrep -af '[d]ual_line_pragmatic_bot.py' || echo "bot:none"
pgrep -af '[c]amoufox_profile_dual_line' | head -3 || echo "camoufox:none"

echo "=== KILL leftovers (TERM) ==="
pgrep -f '[r]un_dual_line_bot.sh'        | xargs -r kill    2>/dev/null
pgrep -f '[d]ual_line_pragmatic_bot.py'  | xargs -r kill    2>/dev/null
pgrep -f '[c]amoufox_profile_dual_line'  | xargs -r kill    2>/dev/null
sleep 4
echo "=== ESCALATE (KILL -9 leftovers) ==="
pgrep -f '[d]ual_line_pragmatic_bot.py'  | xargs -r kill -9 2>/dev/null
pgrep -f '[c]amoufox_profile_dual_line'  | xargs -r kill -9 2>/dev/null
sleep 2

echo "=== AFTER KILL ==="
echo "wrapper=$(pgrep -fc '[r]un_dual_line_bot.sh')"
echo "bot=$(pgrep -fc '[d]ual_line_pragmatic_bot.py')"
echo "camoufox=$(pgrep -fc '[c]amoufox_profile_dual_line')"

echo "=== clear locks ==="
rm -f /tmp/dual_line_bot.lock
rm -f /opt/bacopy/data/camoufox_profile_dual_line/.parentlock
echo "locks cleared"

echo "=== RELAUNCH wrapper (detached; re-sources /opt/bacopy/.env) ==="
cd /opt/laplace2 || exit 1
setsid bash /opt/laplace2/run_dual_line_bot.sh >> /opt/laplace2/dual_line_pragmatic_bot.stdout.log 2>&1 < /dev/null &
disown 2>/dev/null
sleep 9

echo "=== AFTER RELAUNCH ==="
pgrep -af '[r]un_dual_line_bot.sh' || echo "wrapper:NONE"
pgrep -af '[d]ual_line_pragmatic_bot.py' || echo "bot:NONE(may still be launching)"
BOTPID=$(pgrep -f '[d]ual_line_pragmatic_bot.py' | head -1)
echo "BOTPID=$BOTPID"
if [ -n "$BOTPID" ]; then
  echo "=== bot env (V4/API_URL) ==="
  tr '\0' '\n' < /proc/$BOTPID/environ 2>/dev/null | grep -E 'BACOPY_V4_PUBLISH|BACOPY_API_URL'
fi
echo "=== patched code present? (_publish_v4_decision count) ==="
grep -c _publish_v4_decision /opt/laplace2/dual_line_pragmatic_bot.py
echo "=== stdout tail ==="
tail -n 18 /opt/laplace2/dual_line_pragmatic_bot.stdout.log 2>/dev/null
echo "=== DONE ==="
