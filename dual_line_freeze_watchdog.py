#!/usr/bin/env python3
"""dual_line 監視bot 凍結ウォッチドッグ。

botのフィード活動ログ(SIGNAL-SKIP/resolve/PREPOS/DGA-STAT)が一定時間止まったら
= dgaフィードWS死亡で凍結、と判断し、**dual_line専用のpython bot + その camoufox だけ**を
kill する(プロファイル名 camoufox_profile_dual_line で限定 → 他コレクターのcamoufoxは無傷)。
ラッパー run_dual_line_bot.sh が while ループでクリーン再起動(.parentlock除去込み)するため、
ブラウザ居残りで復旧しない失敗モード(2026-06-06 の4時間サイレント停止)を自己修復する。

env:
  DUAL_LINE_LOG               (既定 /opt/laplace2/dual_line_pragmatic_bot.log)
  DUAL_LINE_FREEZE_SEC        (既定 600 = 10分 活動ログが無ければ凍結とみなす)
  DUAL_LINE_WATCHDOG_CHECK_SEC(既定 120)
"""
import re
import os
import sys
import time
import subprocess
from datetime import datetime

LOG = os.getenv("DUAL_LINE_LOG", "/opt/laplace2/dual_line_pragmatic_bot.log")
FREEZE_SEC = int(os.getenv("DUAL_LINE_FREEZE_SEC", "600"))
CHECK_SEC = int(os.getenv("DUAL_LINE_WATCHDOG_CHECK_SEC", "120"))
KILL_PATTERN = os.getenv("DUAL_LINE_KILL_PATTERN", "camoufox_profile_dual_line")
LOCKFILE = "/tmp/dual_line_freeze_watchdog.lock"

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
ACTIVE_RE = re.compile(r"SIGNAL-SKIP|resolve |PREPOS|DGA-STAT|new hand")


def _log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [FREEZE-WATCHDOG] {msg}", flush=True)


def _singleton_or_exit() -> None:
    if os.path.exists(LOCKFILE):
        try:
            old = int(open(LOCKFILE).read().strip())
            os.kill(old, 0)
            _log(f"another instance alive (pid={old}); exiting")
            sys.exit(0)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    with open(LOCKFILE, "w") as f:
        f.write(str(os.getpid()))


def _last_active_age_sec():
    try:
        out = subprocess.run(
            ["tail", "-400", LOG], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return None
    last = None
    for line in out.splitlines():
        if ACTIVE_RE.search(line):
            m = TS_RE.match(line)
            if m:
                last = m.group(1)
    if not last:
        return None
    try:
        return (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:
        return None


def main() -> None:
    _singleton_or_exit()
    _log(f"started pid={os.getpid()} log={LOG} freeze_sec={FREEZE_SEC} check_sec={CHECK_SEC}")
    last_restart = 0.0
    while True:
        time.sleep(CHECK_SEC)
        age = _last_active_age_sec()
        if age is None:
            continue
        if age > FREEZE_SEC and (time.time() - last_restart) > FREEZE_SEC:
            _log(f"feed FROZEN: no active line for {age:.0f}s (> {FREEZE_SEC}s) -> clean restart")
            try:
                subprocess.run(f"pkill -f {KILL_PATTERN}", shell=True, timeout=15)
            except Exception as e:
                _log(f"pkill failed: {e}")
            last_restart = time.time()
            _log("killed dual_line python+camoufox; wrapper respawns clean in ~20s")


if __name__ == "__main__":
    main()
