"""〇❌ロジック × 自動BET 起動スクリプト

Japanese Speed Baccarat A に入場し、〇❌ロジックでPlayer BETを実行。
監視(run_marubatsu.py)とは別プロセスで起動する。

Usage:
    cd E:\\dev\\Cusor\\ba
    python -X utf8 run_marubatsu_bet.py --dry-run     # テスト (BETしない)
    python -X utf8 run_marubatsu_bet.py                # 本番
    python -X utf8 run_marubatsu_bet.py --chip-base 2  # $2/chip
"""
import argparse
import io
import os
import signal
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

_console = logging.StreamHandler(
    stream=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

_file = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "marubatsu_bet.log"),
    encoding="utf-8",
)
_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logger = logging.getLogger("baccarat.marubatsu_bet_main")

import config as cfg
from scraper import BaccaratScraper
from executor import BetExecutor
from game_ws import GameWSMonitor
from humanizer import Humanizer
from notify import TelegramNotifier
from marubatsu_bet import MaruBatsuBetSession, PROFIT_STOP
from marubatsu_strategy import SEQ

TARGET_TABLE_NAME = "Japanese Speed Baccarat A"
MAX_ROUNDS_PER_TABLE = 200


def find_target_table_id(scraper: BaccaratScraper) -> str | None:
    for tid, name in scraper._target_table_names.items():
        if name.strip().lower() == TARGET_TABLE_NAME.lower():
            return tid
    for tid, name in scraper._target_table_names.items():
        if "japanese speed baccarat a" in name.lower():
            return tid
    return None


def main():
    parser = argparse.ArgumentParser(description="〇❌ Auto BET Bot")
    parser.add_argument("--dry-run", action="store_true", help="BETしない (DEMO)")
    parser.add_argument("--chip-base", type=float, default=1.0, help="1chip = ?USD")
    parser.add_argument("--loss-cut", type=int, default=200, help="損切りchip数")
    args = parser.parse_args()

    mode = "DRY RUN" if args.dry_run else "LIVE"
    logger.info(f"[{mode}] 〇❌ Auto BET Bot 起動")
    logger.info(f"  chip_base=${args.chip_base}, 利確+{PROFIT_STOP}, 損切-{args.loss_cut}")

    notifier = TelegramNotifier(
        os.getenv("TELEGRAM_BOT_TOKEN", ""),
        os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    # ブラウザ起動 (BET用プロファイル)
    cfg.HEADLESS = False
    cfg.PROFILE_NAME = "bet"
    scraper = BaccaratScraper()
    scraper.table_name = "Japanese Baccarat"

    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("停止シグナル受信")
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

    # テーブルID解決
    logger.info("テーブルID解決を待機中...")
    for _ in range(30):
        if scraper._target_table_ids:
            break
        time.sleep(1)

    target_tid = find_target_table_id(scraper)
    if not target_tid:
        logger.error(f"テーブル '{TARGET_TABLE_NAME}' が見つかりません")
        scraper.stop()
        return

    target_name = scraper._target_table_names.get(target_tid, TARGET_TABLE_NAME)
    logger.info(f"テーブル発見: {target_name} (ID: {target_tid})")

    # Executor & Session
    humanizer = Humanizer(cfg.HUMANIZE_CONFIG)
    executor_config = {"demo_mode": args.dry_run}
    executor = BetExecutor(scraper.page, scraper.game_ws, executor_config, humanizer=humanizer)

    session = MaruBatsuBetSession(
        executor=executor,
        notifier=notifier,
        chip_base=args.chip_base,
        loss_cut=args.loss_cut,
        dry_run=args.dry_run,
    )

    # テーブル入場
    logger.info(f"テーブルに入場: {target_name}")
    if not executor.enter_table(target_tid, target_name):
        logger.error("テーブル入場失敗")
        scraper.stop()
        return

    balance = executor.get_balance()
    notifier.send(
        f"🟢 〇❌ Auto BET 起動 [{mode}]\n"
        f"━━━━━━━━━━━━━━━\n"
        f"テーブル: {target_name}\n"
        f"1chip = ${args.chip_base}\n"
        f"利確: +{PROFIT_STOP} chip (=${PROFIT_STOP * args.chip_base:.0f}$)\n"
        f"損切: -{args.loss_cut} chip (=${args.loss_cut * args.chip_base:.0f}$)\n"
        f"残高: ${balance:.2f}\n"
        f"現在: SEQ[{session.tracker.current_unit_idx}]={SEQ[session.tracker.current_unit_idx]} chip\n"
        f"累計: {session.tracker.cumulative_profit:+d} chip\n"
        f"━━━━━━━━━━━━━━━\n"
        f"BET開始..."
    )

    # メインBETループ
    round_count = 0
    entry_fail_count = 0

    while running and round_count < MAX_ROUNDS_PER_TABLE:
        # シュー交換チェック
        shoe_signals = scraper.get_new_shoe_signals()
        if target_tid in shoe_signals and shoe_signals[target_tid]:
            logger.info("シュー交換検出")
            session.handle_shoe_change()

        # 1ラウンド実行
        result = session.run_round(lambda: running)

        if result["action"] == "exit":
            # テーブルから退出→再入場を試行
            logger.info("セッション中断 → 再入場試行")
            executor.exit_table()
            time.sleep(5)

            if not running:
                break

            if executor.enter_table(target_tid, target_name):
                entry_fail_count = 0
                continue
            else:
                entry_fail_count += 1
                if entry_fail_count >= 3:
                    logger.error("連続3回入場失敗 → ブラウザ再起動")
                    try:
                        scraper.stop()
                    except Exception:
                        pass
                    time.sleep(3)
                    scraper = BaccaratScraper()
                    scraper.table_name = "Japanese Baccarat"
                    scraper.start()
                    scraper.setup_ws_intercept()
                    for _ in range(30):
                        if scraper._target_table_ids:
                            break
                        time.sleep(1)
                    executor = BetExecutor(scraper.page, scraper.game_ws, executor_config, humanizer=humanizer)
                    session.executor = executor
                    target_tid = find_target_table_id(scraper) or target_tid
                    entry_fail_count = 0
                    if not executor.enter_table(target_tid, target_name):
                        logger.error("再起動後も入場失敗 → 終了")
                        break
                else:
                    time.sleep(10)
                    continue

        round_count += 1

        # 利確/損切りリセット
        if result.get("should_reset"):
            reason = "利確" if session.tracker.cumulative_profit >= PROFIT_STOP else "損切り"
            session.reset_session(reason)

    # 停止処理
    logger.info("停止中...")
    summary = session.get_summary()
    balance = executor.get_balance() if not args.dry_run else 0
    notifier.send(
        f"🔴 〇❌ Auto BET 停止\n"
        f"━━━━━━━━━━━━━━━\n"
        f"BET回数: {summary['total_bets']}回\n"
        f"成績: {summary['total_wins']}勝 {summary['total_losses']}敗 {summary['total_ties']}Tie\n"
        f"累計損益: {summary['cumulative_profit']:+d} chip (${summary['cumulative_money']:+.2f})\n"
        f"セッション: {summary['session_count']}回\n"
        f"残高: ${balance:.2f}\n"
        f"━━━━━━━━━━━━━━━"
    )

    executor.exit_table()
    scraper.stop()
    logger.info("完了")


if __name__ == "__main__":
    main()
