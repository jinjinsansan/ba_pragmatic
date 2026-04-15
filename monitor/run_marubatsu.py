"""〇❌ロジック検証モニター — Japanese Baccarat A 専用

Japanese Baccarat A の1テーブルのみを監視し、
Player=〇, Banker=✕ として7ハンド=1セットの〇❌ロジックを
リアルタイムでシミュレーション。セット確定ごとにTelegram通知。

Usage:
    cd E:\\dev\\Cusor\\ba\\monitor
    python run_marubatsu.py
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

_console = logging.StreamHandler(
    stream=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

_log_path = os.path.join(_monitor_dir, "marubatsu.log")
_file = logging.FileHandler(_log_path, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])

logging.getLogger("baccarat.scraper").handlers = [_file]
logging.getLogger("baccarat.scraper").propagate = False
logging.getLogger("baccarat.game_ws").handlers = [_file]
logging.getLogger("baccarat.game_ws").propagate = False

logger = logging.getLogger("baccarat.marubatsu_monitor")

from scraper import BaccaratScraper
from notify import PublicNotifier
from shoe import ShoeTracker
from marubatsu_strategy import MaruBatsuTracker, SEQ

TARGET_TABLE_NAME = "Japanese Speed Baccarat A"
CHIP_BASE = float(os.getenv("MARUBATSU_CHIP_BASE", "1"))
PROFIT_STOP = int(os.getenv("MARUBATSU_PROFIT_STOP", "50"))
WS_SILENCE_THRESHOLD = _ini.getint("monitor", "ws_silence_threshold", fallback=180)


def find_target_table_id(scraper: BaccaratScraper) -> str | None:
    """Japanese Baccarat A のテーブルIDを特定"""
    for tid, name in scraper._target_table_names.items():
        if name.strip().lower() == TARGET_TABLE_NAME.lower():
            return tid
    # 部分一致フォールバック
    for tid, name in scraper._target_table_names.items():
        if "japanese baccarat a" in name.lower():
            return tid
    return None


def main():
    # 宣伝用公開チャンネル向け: PUBLIC_BOT_TOKEN / PUBLIC_CHANNEL_ID を使用
    # (GUI の TELEGRAM_BOT_TOKEN/USER とは別ボット・別用途)
    notifier = PublicNotifier()
    if not notifier.enabled:
        logger.error("PublicNotifier disabled — PUBLIC_BOT_TOKEN / PUBLIC_CHANNEL_ID が未設定")
        return

    tracker = MaruBatsuTracker(chip_base=CHIP_BASE)

    scraper = BaccaratScraper()
    scraper.table_name = "Japanese Baccarat"

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

    target_tid = find_target_table_id(scraper)
    if not target_tid:
        logger.error(f"テーブル '{TARGET_TABLE_NAME}' が見つかりません")
        logger.info(f"利用可能なテーブル: {list(scraper._target_table_names.values())}")
        scraper.stop()
        return

    target_name = scraper._target_table_names.get(target_tid, TARGET_TABLE_NAME)
    logger.info(f"監視開始: {target_name} (ID: {target_tid})")

    startup_msg = (
        f"🎯 〇❌シミュレーション開始\n"
        f"📍 テーブル: {target_name}\n"
        f"💰 1chip = {CHIP_BASE}円\n"
        f"📋 Player=〇, Banker=✕, Tie=スキップ\n"
        f"7ハンド=1セット\n"
        f"━━━━━━━━━━━━━━━"
    )
    notifier.send(startup_msg)

    shoe = ShoeTracker(table_name=target_name)
    shoe.shoe_number = 1

    # 既存履歴をシューに読み込み (〇❌トラッカーには入れない — ライブ分のみ)
    hist = scraper._evo_table_histories.get(target_tid, [])
    for entry in hist:
        if isinstance(entry, dict):
            color = entry.get("c", "")
            r = {"B": "player", "R": "banker"}.get(color)
            if r:
                shoe.add_result(r)
            for _ in range(entry.get("ties", 0)):
                shoe.add_result("tie")
    logger.info(f"既存履歴読み込み: {shoe.hand_count}ハンド (〇❌はライブ分から開始)")

    last_ws_time = time.time()
    last_result_time = time.time()

    while running:
        try:
            # 新シュー信号チェック
            shoe_signals = scraper.get_new_shoe_signals()
            if target_tid in shoe_signals and shoe_signals[target_tid]:
                logger.info("新シュー検出 — シューリセット")
                shoe.reset()
                shoe.table_name = target_name

                # 〇❌トラッカーの途中ターンがあれば通知
                if tracker.current_turns:
                    partial = "".join(
                        "〇" if t == "O" else "✕" for t in tracker.current_turns
                    )
                    notifier.send(
                        f"⚠️ シューリセット — 途中ターン破棄\n"
                        f"破棄: {partial} ({len(tracker.current_turns)}/7)\n"
                        f"累計損益: {tracker.cumulative_profit:+d} chip"
                    )
                    tracker.current_turns.clear()

            # WS結果チェック
            ws_results = scraper.get_ws_results()
            if ws_results:
                new = scraper.process_results(ws_results)
                if new > 0:
                    last_ws_time = time.time()

                for r in ws_results:
                    result = r.get("result")
                    tid = r.get("table_id", "")

                    if tid != target_tid:
                        continue
                    if result not in ("player", "banker", "tie"):
                        continue

                    shoe.add_result(result)
                    last_result_time = time.time()

                    # 〇❌トラッカーに投入
                    completed_set = tracker.add_result(result)

                    if result == "tie":
                        logger.info(f"Tie — スキップ (〇❌に影響なし)")
                        continue

                    mark = "〇" if result == "player" else "✕"
                    turn_status = tracker.format_telegram_turn_update()
                    logger.info(f"{mark} {turn_status}")

                    if completed_set:
                        # セット確定 → Telegram通知
                        msg = tracker.format_telegram_set_complete(completed_set)
                        notifier.send(msg)
                        logger.info(
                            f"Set #{completed_set.set_index} 確定: "
                            f"{completed_set.results} "
                            f"{completed_set.wins}/{completed_set.losses} "
                            f"P/L:{completed_set.cumulative_profit:+d}"
                        )

                        # 累計損益が目標に達したら停止
                        if completed_set.cumulative_profit >= PROFIT_STOP:
                            logger.info(f"累計損益 {completed_set.cumulative_profit:+d} >= {PROFIT_STOP} — 目標達成停止")
                            notifier.send(
                                f"🎉 目標達成! 累計損益 {completed_set.cumulative_profit:+d} chip "
                                f"({completed_set.cumulative_profit * tracker.chip_base:+.0f}円)\n"
                                f"自動停止します"
                            )
                            running = False
                            break

            # WS沈黙チェック
            ws_silent = scraper.seconds_since_last_ws_message()
            if ws_silent > WS_SILENCE_THRESHOLD:
                logger.warning(f"WSメッセージ{int(ws_silent)}秒沈黙 — ロビーリロード")
                scraper.reload_lobby()
                for _ in range(15):
                    if scraper._target_table_ids:
                        break
                    time.sleep(1)
                new_tid = find_target_table_id(scraper)
                if new_tid:
                    target_tid = new_tid

            # ページ生存チェック (10分間結果なし)
            elapsed = time.time() - last_result_time
            if elapsed > 600 and not scraper.is_alive():
                logger.error("セッション切れ — ブラウザ再起動試行")
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
                new_tid = find_target_table_id(scraper)
                if new_tid:
                    target_tid = new_tid
                    logger.info(f"再接続完了: {target_name}")
                last_result_time = time.time()

            time.sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"エラー: {e}", exc_info=True)
            time.sleep(10)

    # 停止処理
    logger.info("停止中...")
    status = tracker.get_status()
    shutdown_msg = (
        f"⛔ 〇❌シミュレーション停止\n"
        f"📍 {target_name}\n"
        f"📊 完了セット: {status['set_count']}\n"
        f"💰 累計損益: {status['cumulative_profit']:+d} chip "
        f"({status['cumulative_money']:+.0f}円)\n"
        f"〇{status['total_o']} / ✕{status['total_x']}"
    )
    notifier.send(shutdown_msg)
    scraper.stop()
    logger.info("完了")


if __name__ == "__main__":
    main()
