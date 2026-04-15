"""Evolution Baccarat Data Collector

全Evolutionバカラテーブル (92) の全シュー結果を analytics DB に記録する。
テレグラム通知なし。AI学習用のデータ蓄積が目的。

Usage:
    cd E:\\dev\\Cusor\\ba\\monitor
    python run_data_collector.py
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
_cfg.AUTH_STATE_DIR = _cfg.Path(_monitor_dir) / "auth_state_collector"
_cfg.AUTH_STATE_DIR.mkdir(exist_ok=True)
_cfg.PROFILE_NAME = "collector"
_cfg.STAKE_USERNAME = os.getenv("STAKE_USERNAME", "")
_cfg.STAKE_PASSWORD = os.getenv("STAKE_PASSWORD", "")

import io
import time
import signal
import logging
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

_console = logging.StreamHandler(
    stream=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

_log_path = os.path.join(_monitor_dir, "data_collector.log")
_file = logging.FileHandler(_log_path, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logging.getLogger("baccarat.scraper").handlers = [_file]
logging.getLogger("baccarat.scraper").propagate = False
logging.getLogger("baccarat.game_ws").handlers = [_file]
logging.getLogger("baccarat.game_ws").propagate = False

logger = logging.getLogger("baccarat.data_collector")

from scraper import BaccaratScraper
from shoe import ShoeTracker
from analytics_db import init_db, save_shoe, count_shoes, count_hands

WS_SILENCE_THRESHOLD = _ini.getint("monitor", "ws_silence_threshold", fallback=180)
MIN_HANDS_FOR_SAVE = 10  # 最低10ハンド溜まったシューのみ保存 (途中開始のシューを除外)


def main():
    init_db()
    start_shoes = count_shoes()
    start_hands = count_hands()
    logger.info(f"Data collector starting. DB: {start_shoes} shoes, {start_hands} hands")

    scraper = BaccaratScraper()
    scraper.table_name = "all"  # 全テーブル監視

    # table_id -> ShoeTracker
    shoe_trackers: dict[str, ShoeTracker] = {}
    # table_id -> started_at
    shoe_started_at: dict[str, datetime] = {}

    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("Stop signal received")
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        scraper.start()
    except Exception as e:
        logger.error(f"Browser launch failed: {e}")
        scraper.stop()
        return

    scraper.setup_ws_intercept()

    logger.info("Waiting for table IDs...")
    for _ in range(30):
        if scraper._target_table_ids:
            break
        time.sleep(1)

    table_count = len(scraper._target_table_ids)
    logger.info(f"Tracking {table_count} baccarat tables")

    # 既存履歴を読み込んでShoeTrackerを初期化 (ライブ分から収集開始)
    for tid, name in scraper._target_table_names.items():
        tracker = ShoeTracker(table_name=name)
        tracker.shoe_number = 1
        hist = scraper._evo_table_histories.get(tid, [])
        for entry in hist:
            if isinstance(entry, dict):
                color = entry.get("c", "")
                r = {"B": "player", "R": "banker"}.get(color)
                if r:
                    tracker.add_result(r)
                for _ in range(entry.get("ties", 0)):
                    tracker.add_result("tie")
        shoe_trackers[tid] = tracker
        shoe_started_at[tid] = datetime.now(JST)

    logger.info(f"Initial histories loaded for {len(shoe_trackers)} tables")

    last_stats_time = time.time()
    stats_interval = 300  # 5分ごとに統計ログ

    while running:
        try:
            # 新シュー信号
            shoe_signals = scraper.get_new_shoe_signals()
            for tid, has_signal in shoe_signals.items():
                if not has_signal:
                    continue
                tracker = shoe_trackers.get(tid)
                if tracker is None:
                    continue

                # 完了したシューを保存
                if tracker.hand_count >= MIN_HANDS_FOR_SAVE:
                    try:
                        analysis = tracker.analyze()
                        results_list = list(tracker.results)
                        row_id = save_shoe(
                            table_id=tid,
                            table_name=tracker.table_name,
                            started_at=shoe_started_at.get(tid),
                            ended_at=datetime.now(JST),
                            results=results_list,
                            analysis=analysis,
                        )
                        if row_id:
                            logger.info(
                                f"SAVED shoe id={row_id} table={tracker.table_name[:30]} "
                                f"hands={tracker.hand_count} reg={analysis.get('regularity', '')} "
                                f"score={analysis.get('regularity_score', 0):.1f}"
                            )
                    except Exception as e:
                        logger.error(f"Save failed for {tracker.table_name}: {e}")

                # リセット (次のシュー開始)
                tracker.reset()
                tracker.table_name = scraper._target_table_names.get(tid, tracker.table_name)
                shoe_started_at[tid] = datetime.now(JST)

            # WS結果取得
            ws_results = scraper.get_ws_results()
            if ws_results:
                scraper.process_results(ws_results)
                for r in ws_results:
                    result = r.get("result")
                    tid = r.get("table_id", "")

                    if tid not in shoe_trackers:
                        # 新しいテーブル発見
                        name = scraper._target_table_names.get(tid, tid)
                        shoe_trackers[tid] = ShoeTracker(table_name=name)
                        shoe_trackers[tid].shoe_number = 1
                        shoe_started_at[tid] = datetime.now(JST)

                    if result in ("player", "banker", "tie"):
                        shoe_trackers[tid].add_result(result)

            # WS沈黙チェック
            ws_silent = scraper.seconds_since_last_ws_message()
            if ws_silent > WS_SILENCE_THRESHOLD:
                logger.warning(f"WS silent {int(ws_silent)}s — reloading lobby")
                scraper.reload_lobby()
                time.sleep(5)

            # 定期統計ログ
            now = time.time()
            if now - last_stats_time > stats_interval:
                last_stats_time = now
                total_active = sum(1 for t in shoe_trackers.values() if t.hand_count > 0)
                total_hands_in_mem = sum(t.hand_count for t in shoe_trackers.values())
                db_shoes = count_shoes()
                db_hands = count_hands()
                gained_shoes = db_shoes - start_shoes
                gained_hands = db_hands - start_hands
                logger.info(
                    f"[STATS] Active tables: {total_active}/{len(shoe_trackers)} | "
                    f"In-mem hands: {total_hands_in_mem} | "
                    f"DB: {db_shoes} shoes ({gained_shoes:+d}), {db_hands} hands ({gained_hands:+d})"
                )

            time.sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(10)

    logger.info("Shutting down...")

    # 最終統計
    end_shoes = count_shoes()
    end_hands = count_hands()
    logger.info(
        f"Session end. "
        f"Shoes: {start_shoes} -> {end_shoes} (+{end_shoes - start_shoes}), "
        f"Hands: {start_hands} -> {end_hands} (+{end_hands - start_hands})"
    )

    try:
        scraper.stop()
    except Exception:
        pass
    logger.info("Done")


if __name__ == "__main__":
    main()
