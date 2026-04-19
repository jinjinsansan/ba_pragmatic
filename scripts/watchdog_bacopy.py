"""bacopy 受け子 GUI 用 watchdog (LAPLACE watchdog_gui.py ベース).

外部 Python プロセスとして GUI の外側から監視し、以下のケースで自動復旧する:
  1. electron.exe プロセスダウン → GUI 再起動
  2. executor が生きているのにハートビート/ログが stale → camoufox + executor 再起動
  3. log に「iframe 不健全」「Browser closed」「inactivity modal observed but dismiss failed」など
     致命的メッセージが WINDOW 内で連発 → camoufox + executor 再起動
  4. 「recover attempts exhausted」の長期継続 → 最終手段として GUI 丸ごと再起動

実行方法 (PowerShell / cmd):
  python scripts\\watchdog_bacopy.py

環境変数でチューニング:
  BACOPY_WATCHDOG_CHECK_INTERVAL_SEC (default 30)
  BACOPY_WATCHDOG_STALE_SEC          (default 180)
  BACOPY_WATCHDOG_LOG_PATH           (default copytrade_gui/engine.log, if exists)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
GUI_DIR = BASE_DIR / "copytrade_gui"
LOG_PATH = Path(os.environ.get("BACOPY_WATCHDOG_LOG_PATH", str(GUI_DIR / "engine.log")))
PROCESS_NAME = "electron.exe"
EXECUTOR_PROCESS_CANDIDATES = ("python3.12.exe", "python.exe", "bacopy_engine.exe")
CAMOUFOX_PROCESS_CANDIDATES = ("camoufox.exe", "firefox.exe")

CHECK_INTERVAL = int(os.environ.get("BACOPY_WATCHDOG_CHECK_INTERVAL_SEC", "30"))
STALE_SECONDS = int(os.environ.get("BACOPY_WATCHDOG_STALE_SEC", "180"))
RESTART_COOLDOWN = int(os.environ.get("BACOPY_WATCHDOG_RESTART_COOLDOWN_SEC", "60"))
NOFRAME_WINDOW = 5 * 60
NOFRAME_LIMIT = 3
BROWSER_CLOSED_LIMIT = 3
INACTIVITY_FAIL_LIMIT = 3  # 「dismiss failed」が 5 分以内に 3 回で hard restart


def _tasklist(image: str) -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {image}"],
            text=True,
            errors="ignore",
        )
        return image.lower() in out.lower()
    except Exception:
        return False


def gui_running() -> bool:
    return _tasklist(PROCESS_NAME)


def any_process_running(candidates: tuple[str, ...]) -> bool:
    return any(_tasklist(n) for n in candidates)


def log_stale() -> bool:
    try:
        if not LOG_PATH.exists():
            return False
        mtime = LOG_PATH.stat().st_mtime
        return (time.time() - mtime) > STALE_SECONDS
    except Exception:
        return False


def kill(image: str) -> None:
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/IM", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def stop_camoufox() -> None:
    for img in CAMOUFOX_PROCESS_CANDIDATES:
        kill(img)


def stop_executor() -> None:
    # Python executor プロセスだけ狙いたいが、他の Python プロセスを巻き込まないよう
    # copytrade_gui の spawn args に一致するものを WMIC で特定して kill.
    try:
        out = subprocess.check_output(
            [
                "wmic", "process", "where",
                "CommandLine like '%bacopy_executor_pragmatic_ws_live.py%'",
                "get", "ProcessId",
            ],
            text=True,
            errors="ignore",
        )
        for line in out.splitlines():
            line = line.strip()
            if not line or "ProcessId" in line:
                continue
            try:
                pid = int(line)
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                continue
    except Exception:
        pass


def stop_gui() -> None:
    kill(PROCESS_NAME)


def start_gui() -> None:
    # GUI を npm start で起動 (開発モード). packaged 配布では別途起動スクリプト要.
    try:
        subprocess.Popen(
            ["npm", "start"],
            cwd=str(GUI_DIR),
            shell=True,
        )
    except Exception as e:
        print(f"[watchdog] start_gui failed: {e}", flush=True)


def read_new_log_lines(state: dict) -> list[str]:
    try:
        if not LOG_PATH.exists():
            return []
        size = LOG_PATH.stat().st_size
        if size < state["pos"]:
            state["pos"] = 0
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(state["pos"])
            data = f.read()
            state["pos"] = f.tell()
        return data.splitlines()
    except Exception:
        return []


def main() -> int:
    print(f"[watchdog] started - log={LOG_PATH}", flush=True)
    last_restart = 0.0
    last_window_reset = time.time()
    no_frame_hits = 0
    browser_closed_hits = 0
    inactivity_fail_hits = 0
    recover_exhausted_seen_at = 0.0
    log_state = {"pos": 0}

    while True:
        try:
            running_gui = gui_running()
            running_exec = any_process_running(EXECUTOR_PROCESS_CANDIDATES)
            stale = log_stale() if running_exec else False  # ユーザー STOP 中は無視
            now = time.time()

            if now - last_window_reset > NOFRAME_WINDOW:
                no_frame_hits = 0
                browser_closed_hits = 0
                inactivity_fail_hits = 0
                last_window_reset = now

            for line in read_new_log_lines(log_state):
                l = line.lower()
                if "no frames" in l or "iframe 不健全" in line or "target page, context or browser has been closed" in l:
                    browser_closed_hits += 1
                elif "browser closed" in l:
                    browser_closed_hits += 1
                if "iframe" in l and ("dead" in l or "detached" in l or "不健全" in line):
                    no_frame_hits += 1
                if "inactivity" in l and ("dismiss failed" in l or "fail" in l):
                    inactivity_fail_hits += 1
                if "recover attempts exhausted" in l:
                    recover_exhausted_seen_at = now

            hard_restart = (
                no_frame_hits >= NOFRAME_LIMIT
                or browser_closed_hits >= BROWSER_CLOSED_LIMIT
                or inactivity_fail_hits >= INACTIVITY_FAIL_LIMIT
            )
            # recover_exhausted が 3 分以上続いていれば GUI 丸ごと再起動.
            full_restart = bool(recover_exhausted_seen_at and (now - recover_exhausted_seen_at) > 180)

            if full_restart and now - last_restart >= RESTART_COOLDOWN:
                print("[watchdog] recover_exhausted persistent - full GUI restart", flush=True)
                stop_camoufox()
                stop_executor()
                stop_gui()
                time.sleep(3)
                start_gui()
                last_restart = now
                recover_exhausted_seen_at = 0.0
            elif (not running_gui) and now - last_restart >= RESTART_COOLDOWN:
                print("[watchdog] GUI down - restarting", flush=True)
                stop_camoufox()
                stop_executor()
                start_gui()
                last_restart = now
            elif stale and now - last_restart >= RESTART_COOLDOWN:
                print("[watchdog] log stale - restarting executor + camoufox", flush=True)
                stop_camoufox()
                stop_executor()
                last_restart = now
            elif hard_restart and running_exec and now - last_restart >= RESTART_COOLDOWN:
                print(
                    f"[watchdog] hard restart: no_frame={no_frame_hits} "
                    f"browser_closed={browser_closed_hits} inactivity_fail={inactivity_fail_hits}",
                    flush=True,
                )
                stop_camoufox()
                stop_executor()
                last_restart = now
                no_frame_hits = 0
                browser_closed_hits = 0
                inactivity_fail_hits = 0
                last_window_reset = now
        except Exception as e:
            print(f"[watchdog] loop error: {e}", flush=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[watchdog] interrupted", flush=True)
        sys.exit(0)
