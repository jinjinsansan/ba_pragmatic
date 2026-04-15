"""バカラモニター + 自動BETシステム — メインエントリポイント

Stake.com経由でEvolutionライブバカラのテーブルを24時間監視し、
全ラウンドの結果（Player/Banker/Tie）をSQLiteに記録する。
1シューごとにTelegramに統計・出目・バンカー連続数を通知する。

BETモード (--bet / --dry-bet) では戦略に基づいて自動BETを実行。

Usage:
    python main.py                  # 通常起動 (監視のみ)
    python main.py --stats          # 統計表示のみ
    python main.py --dry            # Telegram通知なし
    python main.py --table "Speed Baccarat A"  # テーブル指定
    python main.py --bet            # 自動BETモード
    python main.py --dry-bet        # デモBETモード (実BETなし)
"""
import os
import sys
import time
import random
import signal
import logging
import argparse
import threading

import config
from db import (
    init_db, insert_shoe, get_stats, get_streak, get_recent_results,
    insert_bet, update_bet_result, start_session, end_session, get_bet_stats,
)
from scraper import BaccaratScraper
from notify import TelegramNotifier
from shoe import ShoeTracker
from strategy import BetStrategy
from humanizer import Humanizer
from executor import BetExecutor

import io

# コンソール (PowerShell) — UTF-8強制
_console = logging.StreamHandler(
    stream=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

# ファイル — 全ログ
_file = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "baccarat.log"),
    encoding="utf-8",
)
_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])

# scraper内部の詳細ログ (Round #N, 履歴ロード等) はファイルのみ
logging.getLogger("baccarat.scraper").handlers = [_file]
logging.getLogger("baccarat.scraper").propagate = False
logging.getLogger("baccarat.game_ws").handlers = [_file]
logging.getLogger("baccarat.game_ws").propagate = False

logger = logging.getLogger("baccarat")


def show_stats():
    """統計を表示して終了"""
    init_db()
    stats = get_stats(hours=24)
    if stats["total"] == 0:
        print("データがありません。")
        return

    streak = get_streak()
    recent = get_recent_results(limit=20)

    print("━━━ バカラモニター 24時間統計 ━━━")
    print(f"\n📊 合計 {stats['total']} ラウンド")
    print(f"  🔵 Player: {stats['player']:>4} ({stats['player_pct']}%)")
    print(f"  🔴 Banker: {stats['banker']:>4} ({stats['banker_pct']}%)")
    print(f"  🟢 Tie:    {stats['tie']:>4} ({stats['tie_pct']}%)")
    print(f"  ペア: Player={stats['player_pair']} Banker={stats['banker_pair']}")
    print(f"\n現在の連続: {streak['current']} × {streak['count']}")

    print("\n直近20ラウンド:")
    symbols = {"player": "🔵", "banker": "🔴", "tie": "🟢"}
    row = ""
    for r in reversed(recent):
        row += symbols.get(r["result"], "?")
    print(f"  {row}")


def _run_table_session(
    executor, strategy, shoe, tid, tname,
    notifier, config_mod, stats, dry_bet, running_flag,
):
    """テーブル内でP2連続待ち → 3落ち目狙い → 1-2-3打法を実行。

    フロー:
      1. P2連続 (2落ち) が来るまでラウンドを観戦 (結果をDOMで追う)
      2. P2連続を確認 → 次のBETフェーズで3落ち目をPLAYER BET
      3. 的中 → ドラゴン追い (そのままPLAYER BET)
      4. ハズレ → 次のP2連続を待つ (最大MAX_WAIT_ROUNDS)
      5. P2連続が来なければテーブル退出
    """
    MAX_BET_ROUNDS = 10
    MAX_WAIT_ROUNDS = 5  # P2連続を待つ最大ラウンド数

    bet_count = 0
    wait_count = 0

    for total_round in range(1, MAX_BET_ROUNDS + MAX_WAIT_ROUNDS + 1):
        if not running_flag():
            break

        # エラーダイアログチェック (TRY AGAIN / BACK TO LOBBY)
        if not executor.check_and_dismiss_error():
            logger.warning("エラーダイアログ → テーブル退出")
            break

        # BETフェーズを待つ
        is_first = (total_round == 1)
        if not executor.wait_for_betting_phase(
            timeout=180 if is_first else 120, skip_round=is_first
        ):
            # タイムアウト → エラーダイアログが出ている可能性
            if not executor.check_and_dismiss_error():
                logger.warning("エラーダイアログ → テーブル退出")
                break
            logger.warning("BETフェーズ待ちタイムアウト → テーブル退出")
            break

        # BETフェーズ中にビーズロードを読んでP1落ちを確認
        # P1落ち → 2落ち目をPLAYER BET
        # 当たり(P) → ドラゴン追い
        # ハズレ(B) → ルックに戻る (次のP1落ちを待つ)
        # ※1-2-3打法の途中でも、ビーズ末尾がPでなければBETしない
        bead_road = executor.read_bead_road()
        streaks = executor.get_last_streaks_from_bead()
        should_bet = False
        if streaks:
            last = streaks[-1]
            if last["type"] == "player" and last["len"] >= 1:
                should_bet = True
                logger.info(f"P{last['len']}落ち確認 → {last['len']+1}落ち目狙い (ビーズ: ...{bead_road[-10:]})")

        if not should_bet:
            wait_count += 1
            if wait_count > MAX_WAIT_ROUNDS:
                logger.info(f"P1落ちなし ({MAX_WAIT_ROUNDS}ラウンド待機) ビーズ=...{bead_road[-10:]} → テーブル退出")
                break
            logger.info(f"P1落ちなし ビーズ=...{bead_road[-10:]} (待機 {wait_count}/{MAX_WAIT_ROUNDS})")
            executor.wait_for_result(timeout=90, bet_amount=0)
            continue

        # BET実行 (既にBETフェーズ中)
        wait_count = 0
        bet_count += 1
        bet_amount = strategy.current_bet_amount
        bet_amount = max(config_mod.BET_MIN, min(config_mod.BET_MAX, bet_amount))
        side = "player"
        status = strategy.get_status()
        level = status["bet_level"]

        if not executor.place_bet(side, bet_amount):
            logger.warning("BET失敗 → テーブル退出")
            break

        bet_id = insert_bet(
            table_name=tname, table_id=tid,
            shoe_number=shoe.shoe_number, hand_number=shoe.hand_count,
            bet_side=side, bet_amount=bet_amount,
            strategy_name="player_3dan",
            strategy_reason=f"1-2-3打法 {level}回目",
            regularity_score=0,
        )
        stats["total_bets"] += 1

        if dry_bet or executor.demo_mode:
            logger.info("[DEMO] BET記録完了")
            break

        result_info = executor.wait_for_result(timeout=90, bet_amount=bet_amount)
        if not result_info or not result_info.get("result"):
            logger.warning("結果取得失敗 → テーブル退出")
            break

        result = result_info["result"]
        balance = result_info.get("balance", 0)
        shoe.add_result(result)

        if result == "tie":
            outcome = "tie"
            profit = 0.0
        elif result == side:
            outcome = "win"
            profit = bet_amount
        else:
            outcome = "lose"
            profit = -bet_amount

        if bet_id:
            update_bet_result(bet_id, outcome, profit)
        stats["total_profit"] += profit
        if outcome == "win":
            stats["wins"] += 1
        elif outcome == "lose":
            stats["losses"] += 1

        outcome_jp = {"win": "勝利!", "lose": "負け", "tie": "TIE (引き分け)"}[outcome]
        logger.info(f"結果: {outcome_jp} 収支: ${profit:+.0f} 残高: ${balance:.2f}")

        if config_mod.NOTIFY_EVERY_BET:
            notifier.notify_bet_result({
                "result": outcome, "profit": profit,
                "table_name": tname, "cumulative_profit": stats["total_profit"],
            })

        if outcome == "tie":
            logger.info("TIE → 同額で再BET")
            continue
        elif outcome == "win":
            strategy.record_result(True)
        else:
            strategy.record_result(False)

        # 1-2-3打法の途中 → 即続行
        status = strategy.get_status()
        if status["bet_level"] > 1:
            logger.info(f"次: ${strategy.current_bet_amount:.0f} BET (1-2-3: {status['bet_level']}回目)")
            continue

        # リセット → P2連続を再確認 (has_bet_signal は次ループで判定)
        logger.info("1-2-3リセット → P2連続を再確認")

    logger.info(
        f"テーブルセッション終了: {stats['total_bets']}BET "
        f"累計${stats['total_profit']:+.2f}"
    )


def _handle_shoe_complete(shoe: ShoeTracker, notifier: TelegramNotifier):
    """シュー完了時の処理: DB保存 + Telegram通知"""
    if shoe.hand_count == 0:
        return

    summary = shoe.get_summary()

    # DB保存
    insert_shoe(summary)

    # Telegram通知
    notifier.notify_shoe_complete(summary)

    logging.getLogger("baccarat.scraper").info(
        f"シュー #{summary['shoe_number']} 完了: {summary['table_name']} "
        f"{summary['hand_count']}手 P={summary['player_count']} B={summary['banker_count']} T={summary['tie_count']}"
    )


def run_monitor(table: str = "", dry: bool = False, bet_mode: bool = False, dry_bet: bool = False):
    """メイン監視ループ (BETモード対応)"""
    init_db()

    # BET関連の初期化
    is_betting = bet_mode or dry_bet

    # Telegram (BETモードでは通知無効)
    if is_betting or dry:
        notifier = TelegramNotifier("", "")
    else:
        notifier = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    scraper = BaccaratScraper()
    scraper.table_name = table if table else config.TARGET_TABLE
    strategy = None
    humanizer = None
    executor = None
    session_id = None
    session_start_time = time.time()
    bet_session_stats = {"total_bets": 0, "wins": 0, "losses": 0, "total_profit": 0.0}
    daily_profit = 0.0

    if is_betting:
        strategy = BetStrategy(config.STRATEGY_CONFIG)
        humanizer = Humanizer(config.HUMANIZE_CONFIG)

        executor_config = dict(config.EXECUTOR_CONFIG)
        if dry_bet:
            executor_config["demo_mode"] = True
        elif bet_mode:
            executor_config["demo_mode"] = False

        logger.info(f"BETモード: {'DEMO' if executor_config.get('demo_mode') else 'LIVE'}")
        logger.info(f"戦略: {config.BET_STRATEGY}, P2連続以上でBET")

    running = True
    _shutdown_count = 0

    def shutdown(signum, frame):
        nonlocal running, _shutdown_count
        _shutdown_count += 1
        import traceback
        logger.info(f"停止シグナル受信 (signum={signum}, count={_shutdown_count})")
        logger.info(f"  呼び出し元: {''.join(traceback.format_stack(frame, limit=3))}")
        running = False
        if _shutdown_count >= 2:
            logger.info("強制終了します")
            import os
            os._exit(1)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 起動
    try:
        scraper.start()
    except Exception as e:
        logger.error(f"起動失敗: {e}")
        scraper.take_screenshot("startup_error")
        scraper.stop()
        return

    # BET executor はスクレイパー起動後に初期化 (pageが必要)
    if is_betting:
        executor_config = dict(config.EXECUTOR_CONFIG)
        if dry_bet:
            executor_config["demo_mode"] = True
        elif bet_mode:
            executor_config["demo_mode"] = False
        executor = BetExecutor(scraper.page, scraper.game_ws, executor_config)
        session_id = start_session(0.0)

    # WebSocket傍受を設定
    scraper.setup_ws_intercept()

    # EvolutionロビーWSからテーブル設定が来るのを待つ
    logger.info("EvolutionテーブルID解決を待機中...")
    for _ in range(30):
        if scraper._target_table_ids:
            break
        time.sleep(1)
    else:
        logger.warning("テーブルID解決タイムアウト — 続行します")

    table_count = len(scraper._target_table_ids)
    mode_str = " [BET: DEMO]" if dry_bet else " [BET: LIVE]" if bet_mode else ""
    notifier.notify_startup(
        f"全バカラ ({table_count}テーブル){mode_str}"
    )
    logger.info(f"監視開始: {table_count}テーブル{mode_str} [account={config.STAKE_USERNAME.split('@')[0]}]")

    # マルチテーブル シュー追跡: table_id → ShoeTracker
    shoes: dict[str, ShoeTracker] = {}
    for tid in scraper._target_table_ids:
        tname = scraper._target_table_names.get(tid, tid)
        shoes[tid] = ShoeTracker(table_name=tname)
        shoes[tid].shoe_number = 1
        # 履歴データをshoeに投入
        hist = scraper._evo_table_histories.get(tid, [])
        for entry in hist:
            if isinstance(entry, dict):
                color = entry.get("c", "")
                r = {"B": "player", "R": "banker"}.get(color)
                if r:
                    shoes[tid].add_result(r)
                for _ in range(entry.get("ties", 0)):
                    shoes[tid].add_result("tie")
            elif isinstance(entry, str):
                r = {"player": "player", "banker": "banker", "tie": "tie"}.get(entry.lower())
                if r:
                    shoes[tid].add_result(r)
        logging.getLogger("baccarat.scraper").info(f"シュー #1 開始: {tname} (履歴{shoes[tid].hand_count}手)")

    last_report = time.time()
    last_result_time = time.time()
    last_result_per_table: dict[str, float] = {tid: time.time() for tid in shoes}
    no_result_warning = False
    retry_count = 0
    _recently_bet_tables: dict[str, float] = {}  # table_id → last_bet_time
    _entry_fail_count = 0

    while running:
        try:
            # 0. 新シュー信号をテーブルごとにチェック
            shoe_signals = scraper.get_new_shoe_signals()
            for tid, sig in shoe_signals.items():
                if sig and tid in shoes:
                    _handle_shoe_complete(shoes[tid], notifier)
                    shoes[tid].reset()
                    shoes[tid].table_name = scraper._target_table_names.get(tid, tid)
                    if is_betting and executor and executor.current_table_id == tid:
                        logger.info(f"BET中テーブルのシューリセット検出: {shoes[tid].table_name}")

            # 1. WebSocket結果をチェック
            ws_results = scraper.get_ws_results()
            if ws_results:
                new = scraper.process_results(ws_results)
                if new > 0:
                    last_result_time = time.time()
                    no_result_warning = False

                    # テーブルごとにシューへ結果追加
                    added_to_shoes = 0
                    missed = 0
                    for r in ws_results:
                        result = r.get("result")
                        tid = r.get("table_id", "")
                        if result in ("player", "banker", "tie"):
                            if tid in shoes:
                                shoes[tid].add_result(result)
                                last_result_per_table[tid] = time.time()
                                added_to_shoes += 1
                            else:
                                missed += 1
                    if added_to_shoes > 0 or missed > 0:
                        logging.getLogger("baccarat.scraper").info(f"結果→シュー追加: {added_to_shoes}件, 未登録{missed}件")

            # 1.5. WSキープアライブ (BET中はリロードしない)
            ws_silent = scraper.seconds_since_last_ws_message()
            if ws_silent > config.WS_SILENCE_THRESHOLD and not (executor and executor.in_table):
                logger.warning(f"WSメッセージ{int(ws_silent)}秒沈黙 — ロビーリロード")
                if scraper.reload_lobby():
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

            # === BET判断フェーズ ===
            if is_betting and strategy and executor and not executor.in_table:
                # ブラウザ生存チェック — 死んでいたら即再起動
                if not scraper.is_alive():
                    logger.error("ブラウザセッション切れ — 再起動します")
                    try:
                        # ロビーリロードを試みる
                        scraper.page.goto(
                            "https://stake.com/casino/games/evolution-baccarat",
                            timeout=30000
                        )
                        time.sleep(5)
                        if scraper.is_alive():
                            logger.info("ロビーリロードで復帰成功")
                        else:
                            raise Exception("WS復帰失敗")
                    except Exception as e:
                        logger.error(f"ロビーリロード失敗: {e} — ブラウザ再起動")
                        try:
                            scraper.stop()
                        except Exception:
                            pass
                        time.sleep(3)
                        scraper = BaccaratScraper()
                        scraper.table_name = table if table else "all"
                        scraper.start()
                        scraper.setup_ws_intercept()
                        executor = BetExecutor(scraper.page, scraper.game_ws, executor_config)
                        logger.info("ブラウザ再起動完了")
                    continue

                # 日次損失上限チェック
                if daily_profit <= -config.DAILY_LOSS_LIMIT:
                    if running:
                        logger.warning(f"日次損失上限到達: ${daily_profit:.2f}")
                        notifier.notify_daily_limit("loss", daily_profit)
                        running = False
                        break

                # 日次利益目標チェック
                if daily_profit >= config.DAILY_PROFIT_TARGET:
                    if running:
                        logger.info(f"日次利益目標達成: ${daily_profit:.2f}")
                        notifier.notify_daily_limit("profit", daily_profit)
                        running = False
                        break

                # テーブル巡回: BET条件一致テーブルを探す
                found_target = False
                candidates = []
                now_ts = time.time()
                for tid, shoe in shoes.items():
                    # 直近60秒以内にBETしたテーブルはスキップ
                    if tid in _recently_bet_tables and now_ts - _recently_bet_tables[tid] < 120:
                        continue
                    # テーブル選定条件のみ (①シュー30-50% ③P>B)
                    # BETタイミング(P2連続)はテーブル入場後にDOMで確認する
                    if not strategy.is_table_eligible(shoe):
                        continue
                    tname = scraper._target_table_names.get(tid, tid)
                    candidates.append((tid, shoe, tname, None))

                if candidates:
                    # P優勢度が高いテーブルを優先
                    candidates.sort(key=lambda x: strategy.table_score(x[1]), reverse=True)
                    tid, shoe, tname, _ = candidates[0]
                    found_target = True
                    p = shoe.player_count
                    b = shoe.banker_count
                    logger.info(
                        f">>> テーブル入場: {tname} ({shoe.hand_count}手 P{p}/B{b}) "
                        f"出目={shoe.result_sequence[-8:]}"
                    )
                    if len(candidates) > 1:
                        others = ", ".join(c[2].replace("Japanese ","J.")[:20] for c in candidates[1:3])
                        logger.info(f"    他の候補: {others}")

                    entered = executor.enter_table(tid, tname)
                    if not entered:
                        _entry_fail_count += 1
                        # 失敗テーブルを300秒ブラックリスト
                        _recently_bet_tables[tid] = time.time() + 240
                        logger.warning(f"テーブル入場失敗: {tname} (連続{_entry_fail_count}回)")
                        if _entry_fail_count >= 3:
                            logger.error("連続3回入場失敗 → ブラウザ再起動")
                            _entry_fail_count = 0
                            raise Exception("entry_fail_restart")
                        time.sleep(10)
                        continue
                    _entry_fail_count = 0

                    # テーブル入場後: shoeデータをリセット
                    # テーブル内で観戦した結果だけでBET判断するため
                    shoe.results.clear()
                    logger.info(f"shoeリセット — テーブル内観戦で出目を確認します")

                    # テーブル内で1-2-3打法実行
                    _run_table_session(
                        executor, strategy, shoe, tid, tname,
                        notifier, config,
                        bet_session_stats, dry_bet,
                        lambda: running,
                    )
                    daily_profit = bet_session_stats["total_profit"]

                    executor.exit_table()
                    strategy.reset_losses()  # 1-2-3打法リセット (テーブル間持ち越し防止)
                    _recently_bet_tables[tid] = time.time()
                    logger.info("監視を再開します — 次のポーリングまで待機")
                    last_result_time = time.time()
                    last_result_per_table[tid] = time.time()
                    time.sleep(config.POLL_INTERVAL)

                if not found_target:
                    total_tables = len(shoes)
                    with_data = sum(1 for s in shoes.values() if s.hand_count >= 6)
                    in_range = sum(1 for s in shoes.values() if 6 <= s.hand_count <= 35)
                    p_gt_b = sum(1 for s in shoes.values() if 6 <= s.hand_count <= 35 and s.player_count >= s.banker_count)
                    logger.info(f"候補なし (全{total_tables} データあり{with_data} 範囲内{in_range} P>=B:{p_gt_b})")

            # 3. シュー完了チェック（テーブル別タイムアウト）
            now_ts = time.time()
            for tid, shoe in shoes.items():
                # テーブル別: 5分間結果なし + 十分なハンド数 → シュー完了
                table_last = last_result_per_table.get(tid, now_ts)
                table_silent = now_ts - table_last
                if table_silent > 300 and shoe.hand_count >= 30:
                    logger.info(f"テーブル {shoe.table_name}: {int(table_silent)}秒結果なし + {shoe.hand_count}ハンド → シュー完了")
                    _handle_shoe_complete(shoe, notifier)
                    shoe.reset()
                    shoe.table_name = scraper._target_table_names.get(tid, tid)
                    last_result_per_table[tid] = now_ts

                # シューのハンド数上限チェック
                if shoe.is_shoe_complete():
                    _handle_shoe_complete(shoe, notifier)
                    shoe.reset()
                    shoe.table_name = scraper._target_table_names.get(tid, tid)
                    last_result_per_table[tid] = now_ts

            # 4. 長時間結果なしの警告（全テーブル横断）
            elapsed = time.time() - last_result_time
            if elapsed > 600 and not no_result_warning:
                logger.warning("10分間全テーブルで結果なし — セッション切れの可能性")
                scraper.take_screenshot("no_results")
                no_result_warning = True

            # 5. セッション生存チェック
            if elapsed > 900:
                if not scraper.is_alive():
                    logger.error("セッション切れ — 再接続中...")

                    for tid, shoe in shoes.items():
                        if shoe.hand_count > 0:
                            _handle_shoe_complete(shoe, notifier)
                            shoe.reset()

                    retry_count += 1
                    if retry_count > config.MAX_RETRIES:
                        logger.error("最大リトライ回数超過 — 停止")
                        notifier.notify_shutdown("最大リトライ超過")
                        break

                    scraper.stop()
                    time.sleep(config.RETRY_DELAY)
                    scraper = BaccaratScraper()
                    scraper.table_name = table if table else "all"
                    scraper.start()
                    scraper.setup_ws_intercept()

                    if is_betting:
                        executor_config = dict(config.EXECUTOR_CONFIG)
                        if dry_bet:
                            executor_config["demo_mode"] = True
                        elif bet_mode:
                            executor_config["demo_mode"] = False
                        executor = BetExecutor(scraper.page, scraper.game_ws, executor_config)

                    shoes.clear()
                    for tid in scraper._target_table_ids:
                        tname = scraper._target_table_names.get(tid, tid)
                        shoes[tid] = ShoeTracker(table_name=tname)
                        shoes[tid].shoe_number = 1
                    last_result_time = time.time()
                    last_result_per_table = {tid: time.time() for tid in shoes}
                    no_result_warning = False
                    logger.info(f"再接続成功 (retry {retry_count}/{config.MAX_RETRIES})")

            # 6. 定期レポート
            if time.time() - last_report >= config.REPORT_INTERVAL:
                stats = get_stats(hours=24)
                streak = get_streak()
                notifier.notify_report(f"全テーブル ({table_count})", stats, streak)
                last_report = time.time()

            time.sleep(config.POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            if "entry_fail_restart" in str(e):
                # 連続入場失敗 → ブラウザ再起動
                try:
                    scraper.stop()
                except Exception:
                    pass
                time.sleep(3)
                scraper = BaccaratScraper()
                scraper.table_name = table if table else "all"
                scraper.start()
                scraper.setup_ws_intercept()
                executor_config = dict(config.EXECUTOR_CONFIG)
                if dry_bet:
                    executor_config["demo_mode"] = True
                else:
                    executor_config["demo_mode"] = False
                executor = BetExecutor(scraper.page, scraper.game_ws, executor_config)
                logger.info("ブラウザ再起動完了 (入場失敗)")
                continue
            logger.error(f"メインループエラー: {e}", exc_info=True)
            time.sleep(10)

    # 停止 — 残りのシューデータを保存 (ログ抑制)
    logger.info("監視停止中...")
    saved = 0
    for tid, shoe in shoes.items():
        if shoe.hand_count > 0:
            summary = shoe.get_summary()
            insert_shoe(summary)
            saved += 1
    if saved:
        logger.info(f"シューデータ保存: {saved}件")

    # BETセッション終了
    if is_betting and session_id:
        wins = bet_session_stats["wins"]
        losses = bet_session_stats["losses"]
        win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
        end_session(
            session_id,
            total_bets=bet_session_stats["total_bets"],
            wins=wins,
            losses=losses,
            total_profit=bet_session_stats["total_profit"],
        )
        if config.NOTIFY_SESSION_SUMMARY:
            notifier.notify_session_summary({
                "total_bets": bet_session_stats["total_bets"],
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "total_profit": bet_session_stats["total_profit"],
                "strategy": config.BET_STRATEGY,
                "starting_balance": 0,
                "ending_balance": bet_session_stats["total_profit"],
            })
        logger.info(
            f"BETセッション終了: {bet_session_stats['total_bets']}BET "
            f"W:{wins} L:{losses} 収支:${bet_session_stats['total_profit']:+.2f}"
        )

    stats = get_stats(hours=24)
    notifier.notify_shutdown(f"合計{stats['total']}ラウンド記録")
    scraper.stop()
    logger.info("完了")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="バカラモニター + 自動BETシステム")
    parser.add_argument("--stats", action="store_true", help="統計表示のみ")
    parser.add_argument("--dry", action="store_true", help="Telegram通知なし")
    parser.add_argument("--table", default="", help="テーブル名指定")
    parser.add_argument("--bet", action="store_true", help="自動BETモード (実BET)")
    parser.add_argument("--dry-bet", action="store_true", help="デモBETモード (BETなし)")
    parser.add_argument("--backtest", action="store_true", help="バックテスト実行")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    elif args.backtest:
        from backtest import main as backtest_main
        backtest_main()
    else:
        run_monitor(
            table=args.table,
            dry=args.dry,
            bet_mode=args.bet,
            dry_bet=args.dry_bet,
        )
