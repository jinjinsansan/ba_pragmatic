import os
import subprocess
import time

BASE_DIR = r"C:\dev\ba"
GUI_DIR = os.path.join(BASE_DIR, "gui")
LOG_PATH = os.path.join(BASE_DIR, "agent.log")
RUN_BAT = os.path.join(BASE_DIR, "cloud_scripts", "run.bat")
PROCESS_NAME = "electron.exe"
CHECK_INTERVAL = 30
STALE_SECONDS = 3 * 60
RESTART_COOLDOWN = 60
NOFRAME_WINDOW = 5 * 60
NOFRAME_LIMIT = 3
BROWSER_CLOSED_LIMIT = 3


def is_process_running() -> bool:
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}"],
            text=True,
            errors="ignore",
        )
        return PROCESS_NAME.lower() in output.lower()
    except Exception:
        return False


def log_stale() -> bool:
    try:
        mtime = os.path.getmtime(LOG_PATH)
        return (time.time() - mtime) > STALE_SECONDS
    except Exception:
        return False


def find_agent_pids() -> list[int]:
    try:
        output = subprocess.check_output(
            [
                "wmic",
                "process",
                "where",
                "CommandLine like '%agent_api.py%'",
                "get",
                "ProcessId,CommandLine",
            ],
            text=True,
            errors="ignore",
        )
        pids = []
        for line in output.splitlines():
            line = line.strip()
            if not line or "ProcessId" in line:
                continue
            parts = line.split()
            try:
                pids.append(int(parts[-1]))
            except Exception:
                continue
        return pids
    except Exception:
        return []


def stop_agent():
    for pid in find_agent_pids():
        subprocess.call(
            ["taskkill", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

def stop_camoufox():
    # Kill browser processes too; agent-only restart is insufficient when the browser is zombied.
    for img in ("camoufox.exe", "firefox.exe"):
        subprocess.call(
            ["taskkill", "/F", "/T", "/IM", img],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def stop_gui():
    subprocess.call(
        ["taskkill", "/F", "/IM", PROCESS_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_gui():
    if os.path.exists(RUN_BAT):
        subprocess.Popen(["cmd", "/c", RUN_BAT], cwd=BASE_DIR)
    else:
        subprocess.Popen(["cmd", "/c", "npm run dev"], cwd=GUI_DIR)


def read_new_log_lines(state: dict) -> list[str]:
    try:
        if not os.path.exists(LOG_PATH):
            return []
        size = os.path.getsize(LOG_PATH)
        if size < state["pos"]:
            state["pos"] = 0
        with open(LOG_PATH, "r", errors="ignore") as f:
            f.seek(state["pos"])
            data = f.read()
            state["pos"] = f.tell()
        return data.splitlines()
    except Exception:
        return []


def main():
    last_restart = 0.0
    last_no_frame_reset = time.time()
    no_frame_hits = 0
    browser_closed_hits = 0
    log_state = {"pos": 0}
    while True:
        running = is_process_running()
        agent_pids = find_agent_pids()
        # agent が動いていない (=ユーザーがSTOP中/未起動) 状態では
        # agent.log が更新されないのは正常。ここで GUI を再起動してしまうと、
        # 30秒ごとの監視で「勝手にGUIが落ちて起動し直す」ように見え、
        # localStorage の永続化も不安定になる。
        stale = log_stale() if agent_pids else False
        now = time.time()
        if now - last_no_frame_reset > NOFRAME_WINDOW:
            no_frame_hits = 0
            browser_closed_hits = 0
            last_no_frame_reset = now

        for line in read_new_log_lines(log_state):
            if "no frames" in line or "iframe 不健全" in line:
                no_frame_hits += 1
            if "Browser closed" in line or "Target page, context or browser has been closed" in line:
                browser_closed_hits += 1

        hard_restart = (
            no_frame_hits >= NOFRAME_LIMIT or browser_closed_hits >= BROWSER_CLOSED_LIMIT
        )

        if (not running) or stale:
            if now - last_restart >= RESTART_COOLDOWN:
                if not running:
                    print("[watchdog] gui down - restarting gui")
                    stop_camoufox()
                    stop_agent()
                    start_gui()
                elif stale:
                    # stale は agent_pids がある場合のみ True になる
                    print("[watchdog] log stale - restarting agent")
                    stop_camoufox()
                    stop_agent()
                last_restart = now
        elif hard_restart and agent_pids and now - last_restart >= RESTART_COOLDOWN:
            print("[watchdog] recovery loop/browser closed - restarting agent")
            stop_camoufox()
            stop_agent()
            no_frame_hits = 0
            browser_closed_hits = 0
            last_no_frame_reset = now
            last_restart = now
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
