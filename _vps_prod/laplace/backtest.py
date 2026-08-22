"""バックテストシミュレーション

蓄積データで「もしBETしていたら」をシミュレーションする。
SQLiteのroundsテーブルからシュー単位でデータを取得し、
各戦略の勝率・収支を計算する。

Usage:
    python backtest.py                          # 全戦略でバックテスト
    python backtest.py --strategy yokonagare    # 横流れのみ
    python backtest.py --table "Japanese Speed Baccarat A"  # テーブル指定
    python backtest.py --min-shoes 100          # 最低シュー数
"""
import json
import argparse
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import DB_PATH
from shoe import ShoeTracker
from strategy import BetStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("baccarat.backtest")

JST = timezone(timedelta(hours=9))

STRATEGIES = ["yokonagare", "tereko", "nikoniko", "dragon", "regularity"]

# バンカー勝利時の手数料 (5%)
BANKER_COMMISSION = 0.05


def get_shoes_from_db(table_name: str = "") -> list[list[dict]]:
    """DBからシュー単位のラウンドデータを取得"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    where = ""
    params = []
    if table_name:
        where = "WHERE table_name LIKE ?"
        params = [f"%{table_name}%"]

    shoes_rows = conn.execute(
        f"SELECT * FROM shoes {where} ORDER BY id ASC", params
    ).fetchall()

    all_shoes = []

    for shoe_row in shoes_rows:
        seq = shoe_row["result_sequence"]
        if not seq or len(seq) < 10:
            continue

        results = []
        for ch in seq:
            if ch == "P":
                results.append("player")
            elif ch == "B":
                results.append("banker")
            elif ch == "T":
                results.append("tie")

        if len(results) >= 10:
            all_shoes.append({
                "results": results,
                "table_name": shoe_row["table_name"],
                "shoe_number": shoe_row["shoe_number"],
                "hand_count": shoe_row["hand_count"],
            })

    conn.close()
    logger.info(f"DB から {len(all_shoes)} シューを取得")
    return all_shoes


def simulate_shoe(
    shoe_data: dict,
    strategy_name: str,
    bet_amount: float = 1.0,
    min_regularity: int = 60,
) -> dict:
    """1シューをシミュレーション"""
    results_list = shoe_data["results"]

    tracker = ShoeTracker(table_name=shoe_data.get("table_name", ""))
    tracker.shoe_number = shoe_data.get("shoe_number", 1)

    strategy = BetStrategy({
        "strategy": strategy_name,
        "min_regularity_score": min_regularity,
        "max_consecutive_loss": 99,  # バックテストでは制限しない
    })

    bets = []
    total_profit = 0.0

    for i, result in enumerate(results_list):
        tracker.add_result(result)

        if i < 9:
            continue

        bet_info = strategy.evaluate(tracker)
        if not bet_info:
            continue

        # 次のハンドの結果でBET判定
        if i + 1 < len(results_list):
            next_result = results_list[i + 1]

            if next_result == "tie":
                outcome = "tie_push"
                profit = 0.0
            elif next_result == bet_info["side"]:
                outcome = "win"
                if bet_info["side"] == "banker":
                    profit = bet_amount * (1 - BANKER_COMMISSION)
                else:
                    profit = bet_amount
            else:
                outcome = "lose"
                profit = -bet_amount

            total_profit += profit
            bets.append({
                "hand": i + 1,
                "side": bet_info["side"],
                "reason": bet_info["reason"],
                "next_result": next_result,
                "outcome": outcome,
                "profit": profit,
                "cumulative": total_profit,
            })

    wins = sum(1 for b in bets if b["outcome"] == "win")
    losses = sum(1 for b in bets if b["outcome"] == "lose")
    ties = sum(1 for b in bets if b["outcome"] == "tie_push")

    return {
        "table_name": shoe_data.get("table_name", ""),
        "shoe_number": shoe_data.get("shoe_number", 0),
        "total_hands": len(results_list),
        "total_bets": len(bets),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
        "total_profit": round(total_profit, 2),
        "bets": bets,
    }


def run_backtest(
    strategy_name: str,
    table_name: str = "",
    bet_amount: float = 1.0,
    min_regularity: int = 60,
) -> dict:
    """全シューでバックテスト実行"""
    shoes = get_shoes_from_db(table_name)
    if not shoes:
        logger.warning("シューデータがありません")
        return {"strategy": strategy_name, "shoes": 0, "bets": 0}

    total_bets = 0
    total_wins = 0
    total_losses = 0
    total_ties = 0
    total_profit = 0.0
    bet_shoes = 0  # BETが発生したシュー数
    shoe_results = []

    for shoe_data in shoes:
        result = simulate_shoe(shoe_data, strategy_name, bet_amount, min_regularity)
        shoe_results.append(result)

        if result["total_bets"] > 0:
            bet_shoes += 1

        total_bets += result["total_bets"]
        total_wins += result["wins"]
        total_losses += result["losses"]
        total_ties += result["ties"]
        total_profit += result["total_profit"]

    win_rate = round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) > 0 else 0

    summary = {
        "strategy": strategy_name,
        "shoes": len(shoes),
        "bet_shoes": bet_shoes,
        "total_bets": total_bets,
        "wins": total_wins,
        "losses": total_losses,
        "ties": total_ties,
        "win_rate": win_rate,
        "total_profit": round(total_profit, 2),
        "avg_profit_per_shoe": round(total_profit / len(shoes), 2) if shoes else 0,
        "avg_bets_per_shoe": round(total_bets / bet_shoes, 1) if bet_shoes else 0,
        "bet_amount": bet_amount,
        "min_regularity": min_regularity,
    }

    return summary


def print_summary(summary: dict):
    """バックテスト結果を表示"""
    print(f"\n{'━' * 50}")
    print(f"  戦略: {summary['strategy']}")
    print(f"{'━' * 50}")
    print(f"  シュー数:         {summary['shoes']}")
    print(f"  BET対象シュー:    {summary['bet_shoes']}")
    print(f"  合計BET回数:      {summary['total_bets']}")
    print(f"  勝ち:             {summary['wins']}")
    print(f"  負け:             {summary['losses']}")
    print(f"  タイ(プッシュ):   {summary['ties']}")
    print(f"  勝率:             {summary['win_rate']}%")
    print(f"  総収支:           ${summary['total_profit']:+.2f}")
    print(f"  シューあたり平均: ${summary['avg_profit_per_shoe']:+.2f}")
    print(f"  BETシューあたりBET: {summary['avg_bets_per_shoe']}回")
    print(f"  BET単位:          ${summary['bet_amount']:.2f}")
    print(f"  最低規則性:       {summary['min_regularity']}")
    print(f"{'━' * 50}")


def main():
    parser = argparse.ArgumentParser(description="バカラ バックテスト")
    parser.add_argument("--strategy", default="", help="戦略名 (yokonagare/tereko/nikoniko/dragon/regularity)")
    parser.add_argument("--table", default="", help="テーブル名フィルタ")
    parser.add_argument("--bet", type=float, default=1.0, help="BET単位額")
    parser.add_argument("--min-regularity", type=int, default=60, help="最低規則性スコア")
    parser.add_argument("--min-shoes", type=int, default=0, help="最低シュー数 (未満なら警告)")
    args = parser.parse_args()

    from db import init_db
    init_db()

    strategies = [args.strategy] if args.strategy else STRATEGIES

    print("\n🎰 バカラ バックテスト")
    print(f"   テーブル: {args.table or '全テーブル'}")
    print(f"   BET額: ${args.bet:.2f}")
    print(f"   最低規則性: {args.min_regularity}")

    all_summaries = []

    for strat in strategies:
        summary = run_backtest(
            strategy_name=strat,
            table_name=args.table,
            bet_amount=args.bet,
            min_regularity=args.min_regularity,
        )
        all_summaries.append(summary)
        print_summary(summary)

        if args.min_shoes > 0 and summary["shoes"] < args.min_shoes:
            print(f"  ⚠️  シュー数不足: {summary['shoes']} < {args.min_shoes}")
            print(f"  ⚠️  最低{args.min_shoes}シューのデータ蓄積を推奨")

    # 戦略比較
    if len(all_summaries) > 1:
        print(f"\n{'━' * 50}")
        print("  📊 戦略比較")
        print(f"{'━' * 50}")
        print(f"  {'戦略':<15} {'勝率':>6} {'BET数':>6} {'収支':>10}")
        print(f"  {'-' * 40}")
        for s in sorted(all_summaries, key=lambda x: x["total_profit"], reverse=True):
            print(f"  {s['strategy']:<15} {s['win_rate']:>5.1f}% {s['total_bets']:>6} ${s['total_profit']:>+9.2f}")
        print(f"{'━' * 50}")


if __name__ == "__main__":
    main()
