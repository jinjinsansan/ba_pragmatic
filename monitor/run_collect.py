"""全Evolutionバカラテーブル データ収集スクリプト

全テーブル(Salon Prive等除く)のハンド結果をDBに蓄積する。
BETなし、シュー分析なし、最小限の処理でデータ収集に特化。

Usage:
    cd E:\\dev\\Cusor\\ba\\monitor
    python run_collect.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

import configparser
_monitor_dir = os.path.dirname(os.path.abspath(__file__))
_ini = configparser.ConfigParser()
_ini.read(os.path.join(_monitor_dir, "config.ini"), encoding="utf-8")

import config as _cfg
_cfg.HEADLESS = _ini.getboolean("monitor", "headless", fallback=True)
_cfg.AUTH_STATE_DIR = _cfg.Path(_monitor_dir) / "auth_state"
_cfg.AUTH_STATE_DIR.mkdir(exist_ok=True)
_cfg.PROFILE_NAME = "monitor"
_cfg.STAKE_USERNAME = os.getenv("STAKE_USERNAME", "")
_cfg.STAKE_PASSWORD = os.getenv("STAKE_PASSWORD", "")

import io
import time
import signal
import logging
from datetime import datetime, timezone, timedelta

_console = logging.StreamHandler(
    stream=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

_log_path = os.path.join(_monitor_dir, "collect.log")
_file = logging.FileHandler(_log_path, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])

logging.getLogger("baccarat.scraper").handlers = [_file]
logging.getLogger("baccarat.scraper").propagate = False
logging.getLogger("baccarat.game_ws").handlers = [_file]
logging.getLogger("baccarat.game_ws").propagate = False

logger = logging.getLogger("baccarat.collect")

from db import init_db, get_connection
from scraper import BaccaratScraper
from notify import TelegramNotifier

JST = timezone(timedelta(hours=9))
WS_SILENCE_THRESHOLD = _ini.getint("monitor", "ws_silence_threshold", fallback=180)
REPORT_INTERVAL = 3600  # 1時間ごとにレポート


def get_db_count() -> int:
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) as cnt FROM rounds").fetchone()
    conn.close()
    return row["cnt"]


def main():
    init_db()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    notifier = TelegramNotifier(bot_token, chat_id)

    initial_count = get_db_count()
    logger.info(f"DB既存レコード: {initial_count:,}")

    scraper = BaccaratScraper()
    scraper.table_name = "all"  # 全バカラテーブル

    running = True
    def shutdown(signum, frame):
        nonlocal running
        logger.info("停止シグナル受信...")
        running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        scraper.start()
    except Exception as e:
        logger.error(f"起動失敗: {e}")
        scraper.stop()
        return

    scraper.setup_ws_intercept()

    logger.info("テーブルID解決を待機中...")
    for _ in range(30):
        if scraper._target_table_ids:
            break
        time.sleep(1)

    table_count = len(scraper._target_table_ids)
    logger.info(f"監視開始: {table_count}テーブル (全Evolutionバカラ)")

    notifier.send(
        f"📊 データ収集開始\n"
        f"テーブル数: {table_count}\n"
        f"DB既存: {initial_count:,}レコード\n"
        f"目標: 200,000レコード"
    )

    session_rounds = 0
    last_report = time.time()
    last_result_time = time.time()

    while running:
        try:
            # WS結果をチェック
            ws_results = scraper.get_ws_results()
            if ws_results:
                new = scraper.process_results(ws_results)
                if new > 0:
                    session_rounds += new
                    last_result_time = time.time()

            # WS沈黙チェック
            ws_silent = scraper.seconds_since_last_ws_message()
            if ws_silent > WS_SILENCE_THRESHOLD:
                logger.warning(f"WS {int(ws_silent)}秒沈黙 — リロード")
                scraper.reload_lobby()
                for _ in range(15):
                    if scraper._target_table_ids:
                        break
                    time.sleep(1)
                new_count = len(scraper._target_table_ids)
                if new_count != table_count:
                    table_count = new_count
                    logger.info(f"テーブル数更新: {table_count}")

            # ページ生存チェック
            elapsed = time.time() - last_result_time
            if elapsed > 600 and not scraper.is_alive():
                logger.error("セッション切れ — ブラウザ再起動")
                try:
                    scraper.stop()
                except Exception:
                    pass
                time.sleep(3)
                scraper = BaccaratScraper()
                scraper.table_name = "all"
                scraper.start()
                scraper.setup_ws_intercept()
                for _ in range(30):
                    if scraper._target_table_ids:
                        break
                    time.sleep(1)
                table_count = len(scraper._target_table_ids)
                last_result_time = time.time()

            # 定期レポート
            now = time.time()
            if now - last_report >= REPORT_INTERVAL:
                total = get_db_count()
                added = total - initial_count
                rate = session_rounds / max((now - last_report), 1) * 3600
                remaining = max(0, 200000 - total)
                eta_hours = remaining / max(rate, 1)

                msg = (
                    f"📊 データ収集レポート\n"
                    f"テーブル数: {table_count}\n"
                    f"DB合計: {total:,}レコード\n"
                    f"今回追加: +{session_rounds:,}\n"
                    f"収集速度: {rate:,.0f}/時間\n"
                    f"残り: {remaining:,} → 約{eta_hours:.1f}時間"
                )
                notifier.send(msg)
                logger.info(
                    f"レポート: DB={total:,} +{session_rounds:,} "
                    f"速度={rate:,.0f}/h テーブル={table_count}"
                )
                session_rounds = 0
                last_report = now

            time.sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"エラー: {e}", exc_info=True)
            time.sleep(10)

    # 停止
    total = get_db_count()
    added = total - initial_count
    logger.info(f"停止: DB={total:,} (今回+{added:,})")
    notifier.send(
        f"⛔ データ収集停止\n"
        f"DB合計: {total:,}レコード\n"
        f"今回追加: +{added:,}"
    )
    scraper.stop()


if __name__ == "__main__":
    main()
