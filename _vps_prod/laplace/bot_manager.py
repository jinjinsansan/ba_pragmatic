"""
Bot process manager for LAPLACE API (VPS-side).

Manages lifecycle of laplace_bet_runner.py as a child subprocess:
  - start(config)    : spawn runner with JSON config
  - stop(timeout)    : graceful SIGTERM → SIGKILL fallback
  - status()         : pid / uptime / config / running flag
  - log_tail(lines)  : return last N lines from the run log

Only one bot runs at a time per API instance.
Logs: /opt/laplace/bot_logs/{run_id}.log
Configs: /opt/laplace/bot_configs/{run_id}.json
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("laplace.bot_manager")

BOT_LOGS_DIR = Path(os.getenv("LAPLACE_BOT_LOGS_DIR", "/opt/laplace/bot_logs"))
BOT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
BOT_CONFIG_DIR = Path(os.getenv("LAPLACE_BOT_CONFIG_DIR", "/opt/laplace/bot_configs"))
BOT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

BOT_RUNNER_SCRIPT = os.getenv(
    "LAPLACE_BOT_RUNNER",
    "/opt/laplace/laplace_bet_runner.py",
)
BOT_PYTHON = os.getenv(
    "LAPLACE_BOT_PYTHON",
    "/opt/laplace/.venv/bin/python",
)
BOT_CWD = os.getenv("LAPLACE_BOT_CWD", "/opt/laplace")


class BotManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._run_id: Optional[str] = None
        self._log_path: Optional[Path] = None
        self._config_path: Optional[Path] = None
        self._started_at: Optional[float] = None
        self._config: Optional[dict] = None
        self._last_exit: Optional[dict] = None

    # --- lifecycle ---

    def is_running(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            return self._proc.poll() is None

    def start(self, config: dict) -> dict:
        with self._lock:
            if self.is_running():
                raise RuntimeError(f"bot already running (pid={self._proc.pid})")

            if not Path(BOT_RUNNER_SCRIPT).exists():
                raise RuntimeError(f"runner script not found: {BOT_RUNNER_SCRIPT}")
            if not Path(BOT_PYTHON).exists():
                raise RuntimeError(f"python not found: {BOT_PYTHON}")

            run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            log_path = BOT_LOGS_DIR / f"{run_id}.log"
            config_path = BOT_CONFIG_DIR / f"{run_id}.json"

            config_with_id = {**config, "run_id": run_id}
            config_path.write_text(
                json.dumps(config_with_id, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            log_file = open(log_path, "w", encoding="utf-8")

            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            env["LAPLACE_BOT_RUN_ID"] = run_id
            env["LAPLACE_BOT_CONFIG"] = str(config_path)

            cmd = [BOT_PYTHON, "-u", BOT_RUNNER_SCRIPT]

            logger.info(f"spawn bot run_id={run_id} cmd={' '.join(cmd)}")
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=BOT_CWD,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
            except Exception as e:
                log_file.close()
                raise RuntimeError(f"failed to spawn bot: {e}")

            self._run_id = run_id
            self._log_path = log_path
            self._config_path = config_path
            self._started_at = time.time()
            self._config = config_with_id
            self._last_exit = None

            return {
                "run_id": run_id,
                "pid": self._proc.pid,
                "log_path": str(log_path),
                "config": config_with_id,
            }

    def stop(self, timeout: float = 15.0) -> dict:
        with self._lock:
            if not self.is_running():
                return {"was_running": False, "last_exit": self._last_exit}

            pid = self._proc.pid
            run_id = self._run_id

            try:
                pgid = os.getpgid(pid)
                logger.info(f"stop bot run_id={run_id} pid={pid} pgid={pgid}")
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning(f"SIGTERM failed: {e}")

            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("bot SIGTERM timeout, sending SIGKILL")
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("bot SIGKILL timeout")

            exit_code = self._proc.returncode
            self._last_exit = {
                "run_id": run_id,
                "pid": pid,
                "exit_code": exit_code,
                "stopped_at": time.time(),
            }
            self._proc = None

            return {"was_running": True, **self._last_exit}

    # --- introspection ---

    def status(self) -> dict:
        with self._lock:
            running = self.is_running()
            return {
                "running": running,
                "run_id": self._run_id if running else None,
                "pid": self._proc.pid if running and self._proc else None,
                "started_at": self._started_at if running else None,
                "uptime_seconds": (
                    (time.time() - self._started_at)
                    if self._started_at and running
                    else None
                ),
                "log_path": str(self._log_path) if self._log_path else None,
                "config": self._config if running else None,
                "last_exit": self._last_exit,
            }

    def log_tail(self, lines: int = 100) -> list[str]:
        with self._lock:
            if self._log_path is None or not self._log_path.exists():
                return []
            try:
                size = self._log_path.stat().st_size
                with open(self._log_path, "rb") as f:
                    if size > 65536:
                        f.seek(-65536, 2)
                    data = f.read().decode("utf-8", errors="replace")
                return data.splitlines()[-lines:]
            except Exception as e:
                logger.warning(f"log_tail error: {e}")
                return []


_BOT = BotManager()


def get_bot_manager() -> BotManager:
    return _BOT
