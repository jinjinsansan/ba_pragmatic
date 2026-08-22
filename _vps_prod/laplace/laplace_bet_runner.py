"""
VPS-side BET runner for LAPLACE.

Spawned by laplace-api.service via bot_manager. Reads config from
$LAPLACE_BOT_CONFIG (JSON file) and launches a headless Camoufox BET
session that talks to the same machine's LAPLACE API over loopback.

Flow:
  1. Parse config JSON (user_id, target_table_name, chip_base, ...)
  2. Force headless + set env vars that agent_api.run_bet_session reads
  3. Monkey-patch agent_api.send_msg to redirect GUI-bound messages into
     the logger (since there is no GUI attached here — the log file is
     served back to the GUI via /api/bot/log)
  4. Hook SIGTERM/SIGINT to set the stop_event for graceful shutdown
  5. Call agent_api.run_bet_session(session_config, stop_event)

This avoids duplicating the 300+ line BET loop logic from agent_api.py.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env for STAKE_USERNAME / STAKE_PASSWORD / LAPLACE_API_KEY / etc.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("laplace.bet_runner")


def _load_config() -> dict:
    path = os.getenv("LAPLACE_BOT_CONFIG", "").strip()
    if not path:
        logger.error("LAPLACE_BOT_CONFIG env var is empty")
        sys.exit(2)
    if not os.path.exists(path):
        logger.error(f"config not found: {path}")
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    run_id = os.getenv("LAPLACE_BOT_RUN_ID", "unknown")
    config = _load_config()

    logger.info("=" * 60)
    logger.info(f"LAPLACE bet runner START run_id={run_id}")
    logger.info(f"config: {json.dumps(config, ensure_ascii=False)}")
    logger.info("=" * 60)

    # === Env setup for agent_api.run_bet_session ===
    os.environ["LAPLACE_HEADLESS"] = "1"
    os.environ["LAPLACE_USE_REMOTE"] = "1"
    os.environ.setdefault("LAPLACE_API_URL", "http://127.0.0.1:8000")
    os.environ["LAPLACE_MODE"] = "verification"
    os.environ["LAPLACE_FIXED_TABLE"] = config.get(
        "target_table_name", "Japanese Speed Baccarat A"
    )
    os.environ["LAPLACE_USER"] = config.get("user_id", "default")
    os.environ["LAPLACE_PROFILE_NAME"] = f"bet-{config.get('user_id', 'default')}"

    chip_base = float(config.get("chip_base", 1.0))
    # Config comes from BotStartRequest with profit_stop/loss_cut as CHIP amounts,
    # but agent_api.run_bet_session converts profit_target/loss_cut as DOLLAR amounts.
    # Convert chips → dollars for the session config.
    profit_stop_chips = int(config.get("profit_stop", 50))
    loss_cut_chips = int(config.get("loss_cut", 200))

    session_config = {
        "chip_base": chip_base,
        "profit_target": profit_stop_chips * chip_base,
        "loss_cut": loss_cut_chips * chip_base,
        "dry_run": bool(config.get("dry_run", True)),
        "resume": bool(config.get("resume_session", True)),
        "verification_mode": True,
    }
    logger.info(f"session_config: {json.dumps(session_config, ensure_ascii=False)}")

    # === Monkey-patch agent_api send_msg to redirect GUI JSON → log ===
    import agent_api

    def _log_send_msg(msg: dict):
        try:
            line = json.dumps(msg, ensure_ascii=False)
        except Exception:
            line = repr(msg)
        logger.info(f"MSG {line}")

    agent_api.send_msg = _log_send_msg  # type: ignore[assignment]

    # === Graceful stop hook ===
    stop_event = threading.Event()

    def handle_sig(signum, frame):
        name = signal.Signals(signum).name
        logger.info(f"signal {name} received — requesting stop")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    try:
        signal.signal(signal.SIGHUP, handle_sig)  # type: ignore[attr-defined]
    except Exception:
        pass  # not available on non-POSIX

    # === Run the BET session ===
    try:
        agent_api.run_bet_session(session_config, stop_event, skip_event=None)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — stopping")
    except SystemExit:
        raise
    except Exception:
        logger.exception("bet runner crashed")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"LAPLACE bet runner STOP run_id={run_id}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
