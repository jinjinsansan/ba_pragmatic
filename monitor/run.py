"""hakudasama用 — Japanese Speed Baccarat 9テーブル監視 + Telegram通知

Usage:
    cd E:\\dev\\Cusor\\ba\\monitor
    python run.py
"""
import os
import sys

# 親ディレクトリのモジュールを使用
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# このディレクトリの .env を優先読み込み
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

# config.ini もこのディレクトリのものを使用
import configparser
_monitor_dir = os.path.dirname(os.path.abspath(__file__))
_ini = configparser.ConfigParser()
_ini.read(os.path.join(_monitor_dir, "config.ini"), encoding="utf-8")

# monitor用設定を親configに反映
import config as _cfg
_cfg.HEADLESS = _ini.getboolean("monitor", "headless", fallback=True)
_cfg.AUTH_STATE_DIR = _cfg.Path(_monitor_dir) / "auth_state"
_cfg.AUTH_STATE_DIR.mkdir(exist_ok=True)
_cfg.PROFILE_NAME = "monitor"
# monitor用の.envから再設定 (親config.pyのload_dotenvで上書きされている可能性対策)
_cfg.STAKE_USERNAME = os.getenv("STAKE_USERNAME", "")
_cfg.STAKE_PASSWORD = os.getenv("STAKE_PASSWORD", "")

import io
import time
import signal
import logging

# ログ設定
_console = logging.StreamHandler(
    stream=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

_log_path = os.path.join(_monitor_dir, "monitor.log")
_file = logging.FileHandler(_log_path, encoding="utf-8")
_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])

# scraper詳細ログはファイルのみ
logging.getLogger("baccarat.scraper").handlers = [_file]
logging.getLogger("baccarat.scraper").propagate = False
logging.getLogger("baccarat.game_ws").handlers = [_file]
logging.getLogger("baccarat.game_ws").propagate = False

logger = logging.getLogger("baccarat.monitor")

from db import init_db, insert_shoe, get_stats, get_streak
from scraper import BaccaratScraper
from notify import TelegramNotifier
from shoe import ShoeTracker

# Evolution WSの historyUpdated バッチ間隔は2〜3分。
# リロードはその合間を壊すため、十分長い閾値を設定する。
WS_SILENCE_THRESHOLD = _ini.getint("monitor", "ws_silence_threshold", fallback=180)


def _handle_shoe_complete(shoe: ShoeTracker, notifier: TelegramNotifier):
    if shoe.hand_count == 0:
        return
    summary = shoe.get_summary()
    insert_shoe(summary)
    notifier.notify_shoe_complete(summary)
    logger.info(
        f"シュー完了: {summary['table_name']} {summary['hand_count']}手 "
        f"P={summary['player_count']} B={summary['banker_count']} T={summary['tie_count']}"
    )


def _restart_browser(scraper, shoes, notifier):
    """ブラウザ再起動。既存シューを保存して新しいscraperを返す"""
    for tid, shoe in shoes.items():
        if shoe.hand_count > 0:
            _handle_shoe_complete(shoe, notifier)
            shoe.reset()
    try:
        scraper.stop()
    except Exception:
        pass
    time.sleep(3)
    new_scraper = BaccaratScraper()
    new_scraper.table_name = "Japanese Baccarat"
    new_scraper.start()
    new_scraper.setup_ws_intercept()
    for _ in range(30):
        if new_scraper._target_table_ids:
            break
        time.sleep(1)
    # 新しいテーブルをshoesに追加
    for tid in new_scraper._target_table_ids:
        if tid not in shoes:
            tname = new_scraper._target_table_names.get(tid, tid)
            shoes[tid] = ShoeTracker(table_name=tname)
            shoes[tid].shoe_number = 1
    logger.info(f"ブラウザ再起動完了: {len(new_scraper._target_table_ids)}テーブル")
    return new_scraper


def main():
    init_db()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    logger.info(f"Telegram設定: token={'***'+bot_token[-6:] if bot_token else 'EMPTY'}, chat_id={chat_id}")
    notifier = TelegramNotifier(bot_token, chat_id)

    scraper = BaccaratScraper()
    scraper.table_name = "Japanese Baccarat"  # Japanese 9テーブルのみ

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

    logger.info("EvolutionテーブルID解決を待機中...")
    for _ in range(30):
        if scraper._target_table_ids:
            break
        time.sleep(1)

    table_count = len(scraper._target_table_ids)
    logger.info(f"監視開始: {table_count}テーブル (Japanese Speed Baccarat)")
    notifier.notify_startup(f"Japanese Speed Baccarat ({table_count}テーブル)")

    shoes: dict[str, ShoeTracker] = {}
    now = time.time()
    for tid in scraper._target_table_ids:
        tname = scraper._target_table_names.get(tid, tid)
        shoes[tid] = ShoeTracker(table_name=tname)
        shoes[tid].shoe_number = 1
        hist = scraper._evo_table_histories.get(tid, [])
        for entry in hist:
            if isinstance(entry, dict):
                color = entry.get("c", "")
                r = {"B": "player", "R": "banker"}.get(color)
                if r:
                    shoes[tid].add_result(r)
                for _ in range(entry.get("ties", 0)):
                    shoes[tid].add_result("tie")

    last_result_time = now
    last_result_per_table: dict[str, float] = {tid: now for tid in shoes}
    last_report = now
    no_result_warning = False

    while running:
        try:
            # 1. 新シュー信号をチェック
            shoe_signals = scraper.get_new_shoe_signals()
            for tid, sig in shoe_signals.items():
                if sig and tid in shoes:
                    _handle_shoe_complete(shoes[tid], notifier)
                    shoes[tid].reset()
                    shoes[tid].table_name = scraper._target_table_names.get(tid, tid)
                    last_result_per_table[tid] = time.time()

            # 2. WS結果をチェック
            ws_results = scraper.get_ws_results()
            if ws_results:
                new = scraper.process_results(ws_results)
                if new > 0:
                    last_result_time = time.time()
                    no_result_warning = False
                    for r in ws_results:
                        result = r.get("result")
                        tid = r.get("table_id", "")
                        if result in ("player", "banker", "tie") and tid in shoes:
                            shoes[tid].add_result(result)
                            last_result_per_table[tid] = time.time()

            # 3. WS沈黙チェック — Evolution WSは2〜3分間隔でバッチ送信。
            #    十分待ってからリロードする (不要なリロードを防止)。
            ws_silent = scraper.seconds_since_last_ws_message()
            if ws_silent > WS_SILENCE_THRESHOLD:
                logger.warning(f"WSメッセージ{int(ws_silent)}秒沈黙 — ロビーリロード")
                if scraper.reload_lobby():
                    # リロード成功 — テーブルID再解決を待つ
                    for _ in range(15):
                        if scraper._target_table_ids:
                            break
                        time.sleep(1)
                    for tid in scraper._target_table_ids:
                        if tid not in shoes:
                            tname = scraper._target_table_names.get(tid, tid)
                            shoes[tid] = ShoeTracker(table_name=tname)
                            shoes[tid].shoe_number = 1
                            last_result_per_table[tid] = time.time()
                else:
                    # リロード失敗が続く場合は scraper.reload_lobby 内で
                    # _full_navigate_lobby にエスカレートされる
                    logger.warning("リロード失敗")

            # 4. ページ生存チェック (10分間全テーブルで結果なし)
            elapsed = time.time() - last_result_time
            if elapsed > 600:
                if not no_result_warning:
                    logger.warning("10分間全テーブルで結果なし — セッション確認")
                    scraper.take_screenshot("no_results")
                    no_result_warning = True

                if not scraper.is_alive():
                    logger.error("セッション切れ — ブラウザ再起動")
                    scraper = _restart_browser(scraper, shoes, notifier)
                    last_result_time = time.time()
                    last_result_per_table = {tid: time.time() for tid in shoes}
                    no_result_warning = False
                    table_count = len(scraper._target_table_ids)

            # 5. テーブル別シュー完了チェック
            now_ts = time.time()
            for tid, shoe in list(shoes.items()):
                table_last = last_result_per_table.get(tid, now_ts)
                table_silent = now_ts - table_last

                # テーブル別: 5分間結果なし + 十分なハンド数 → シュー完了
                if table_silent > 300 and shoe.hand_count >= 30:
                    logger.info(
                        f"テーブル {shoe.table_name}: {int(table_silent)}秒結果なし "
                        f"+ {shoe.hand_count}ハンド → シュー完了"
                    )
                    _handle_shoe_complete(shoe, notifier)
                    shoe.reset()
                    shoe.table_name = scraper._target_table_names.get(tid, tid)
                    last_result_per_table[tid] = now_ts

                # ハンド数上限チェック
                if shoe.is_shoe_complete():
                    _handle_shoe_complete(shoe, notifier)
                    shoe.reset()
                    shoe.table_name = scraper._target_table_names.get(tid, tid)
                    last_result_per_table[tid] = now_ts

            # 6. 定期レポート (1時間ごと)
            if time.time() - last_report >= _cfg.REPORT_INTERVAL:
                monitored_names = list(scraper._target_table_names.values())
                stats = get_stats(hours=24, table_names=monitored_names)
                streak = get_streak(table_names=monitored_names)
                notifier.notify_report(f"Japanese Speed Baccarat ({table_count}テーブル)", stats, streak)
                last_report = time.time()

            time.sleep(5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"エラー: {e}", exc_info=True)
            time.sleep(10)

    logger.info("監視停止中...")
    saved = 0
    for tid, shoe in shoes.items():
        if shoe.hand_count > 0:
            summary = shoe.get_summary()
            insert_shoe(summary)
            saved += 1
    if saved:
        logger.info(f"シューデータ保存: {saved}件")

    notifier.notify_shutdown()
    scraper.stop()
    logger.info("完了")


if __name__ == "__main__":
    main()
