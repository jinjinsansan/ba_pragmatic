from __future__ import annotations

import argparse
import atexit
import os
import shutil
import sys
import tempfile
from typing import Optional

_PROCESS_LOCK_HANDLE = None


def _acquire_single_process_lock(name: str) -> bool:
    """Best-effort cross-process lock for packaged GUI commands."""
    global _PROCESS_LOCK_HANDLE
    lock_dir = os.getenv("BACOPY_LOCK_DIR") or tempfile.gettempdir()
    lock_path = os.path.join(lock_dir, f"{name}.lockdir")

    def _pid_is_same_engine(pid_text: str) -> bool:
        try:
            pid = int(str(pid_text or "").strip())
        except Exception:
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        if os.name != "nt":
            return True
        try:
            import subprocess

            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$p=Get-CimInstance Win32_Process -Filter "
                    f"\"ProcessId={pid}\" -ErrorAction SilentlyContinue; "
                    "if($p){$p.Name + ' ' + $p.CommandLine}"
                ),
            ]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2)
            low = str(out or "").lower()
            return "bacopy_engine" in low and "dual-line" in low
        except Exception:
            return True

    def _pid_alive(pid_text: str) -> bool:
        try:
            pid = int(str(pid_text or "").strip())
        except Exception:
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel32.WaitForSingleObject.restype = wintypes.DWORD
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if not handle:
                    return False
                try:
                    return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return True
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    for _ in range(2):
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            os.mkdir(lock_path)
            pid_file = os.path.join(lock_path, "pid")
            with open(pid_file, "w", encoding="ascii") as f:
                f.write(str(os.getpid()))

            def _cleanup_lock() -> None:
                try:
                    shutil.rmtree(lock_path)
                except Exception:
                    pass

            atexit.register(_cleanup_lock)
            _PROCESS_LOCK_HANDLE = ("lockdir", lock_path)
            return True
        except FileExistsError:
            pid_text = ""
            try:
                with open(os.path.join(lock_path, "pid"), "r", encoding="ascii") as f:
                    pid_text = f.read().strip()
            except Exception:
                pid_text = ""
            if _pid_alive(pid_text) and _pid_is_same_engine(pid_text):
                return False
            try:
                shutil.rmtree(lock_path)
            except Exception:
                return False
            continue
        except Exception:
            break

    if os.name == "nt" and os.getenv("BACOPY_ENABLE_MUTEX_LOCK", "0").strip() == "1":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
            last_error = ctypes.get_last_error()
            if handle and last_error == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            if handle:
                _PROCESS_LOCK_HANDLE = ("mutex", handle)
                return True
        except Exception:
            pass

    path = os.path.join(lock_dir, f"{name}.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb") as init:
            init.write(b"\0")
    fh = open(path, "r+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            if not fh.read(1):
                fh.seek(0)
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        return False
    _PROCESS_LOCK_HANDLE = fh
    return True


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="bacopy_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("executor-pragmatic", help="Pragmatic live WS executor")
    sub.add_parser("watch-pragmatic", help="Pragmatic watcher (snapshot)")
    sub.add_parser("watch-evolution", help="Evolution watcher (snapshot)")
    sub.add_parser("dual-line", help="Dual-line match prediction bot (DRY RUN / LIVE)")

    ns, rest = ap.parse_known_args(argv)

    if ns.cmd == "executor-pragmatic":
        from bacopy_executor_pragmatic_ws_live import main as _m
        try:
            return int(_m(rest) or 0)
        except Exception as e:
            msg = str(e or "")
            if "BrowserContext.close" in msg and "Connection closed while reading from the driver" in msg:
                print(
                    "[executor-live] swallowed shutdown exception from Camoufox driver disconnect: "
                    + msg,
                    file=sys.stderr,
                    flush=True,
                )
                return 0
            raise

    if ns.cmd == "watch-pragmatic":
        from bacopy_watch_pragmatic import main as _m

        return int(_m(rest) or 0)

    if ns.cmd == "watch-evolution":
        from bacopy_watch_evolution import main as _m

        return int(_m(rest) or 0)

    if ns.cmd == "dual-line":
        rest_text = " ".join(str(x or "") for x in rest)
        is_live_dual_line = "--live" in rest or " --live " in f" {rest_text} "
        if is_live_dual_line and not _acquire_single_process_lock("bacopy_dual_line_live"):
            print(
                "[dual-line] another live engine is already running; exiting duplicate process",
                file=sys.stderr,
                flush=True,
            )
            return 97
        from dual_line_pragmatic_bot import main as _m
        try:
            return int(_m(rest) or 0)
        except Exception as e:
            msg = str(e or "")
            if "BrowserContext.close" in msg and "Connection closed while reading from the driver" in msg:
                print(
                    "[dual-line] swallowed shutdown exception from Camoufox driver disconnect: "
                    + msg,
                    file=sys.stderr,
                    flush=True,
                )
                return 0
            raise

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
