"""エッジ探索（結果以外の特徴量）: players_online / ws_silence を使う

前提:
  analytics_db.py の拡張後に収集したDB（analytics.sqlite3）でのみ有効。
  過去のDBは observed_at / players_online / ws_silence がNULLのため分析できない。

やること:
  - hands テーブルを shoe_id で辿り、直前手の players_online / ws_silence で条件分岐し
    trend/counter の期待値（profit/hand）を推定する。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

DB_PATH = "analytics.sqlite3"
BANKER_COMMISSION = 0.05


def profit_if_win(side: str) -> float:
    return 1.0 - BANKER_COMMISSION if side == "B" else 1.0


def bucket_players(n: int) -> int:
    # 0:0-4, 1:5-9, 2:10-19, 3:20-39, 4:40+
    if n < 5:
        return 0
    if n < 10:
        return 1
    if n < 20:
        return 2
    if n < 40:
        return 3
    return 4


def bucket_ws(s: float) -> int:
    # 0:<2s 1:2-5 2:5-15 3:15-60 4:60+
    if s < 2:
        return 0
    if s < 5:
        return 1
    if s < 15:
        return 2
    if s < 60:
        return 3
    return 4


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # join hands->shoes for ordering (shoe_id, hand_index)
    cur.execute(
        "SELECT h.shoe_id, h.hand_index, h.result, h.players_online, h.ws_silence, "
        "s.table_id, s.table_name, s.started_at "
        "FROM hands h JOIN shoes_analytics s ON s.id = h.shoe_id "
        "WHERE h.players_online IS NOT NULL AND h.ws_silence IS NOT NULL "
        "ORDER BY h.shoe_id, h.hand_index"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"No rows with players_online/ws_silence found in {DB_PATH}. Run data collector after DB migration.")
        return

    # cell -> mode -> (profit,n,wins)
    stats = defaultdict(lambda: {"counter":[0.0,0,0], "trend":[0.0,0,0]})

    prev = None  # (shoe_id, idx, result, players_online, ws_silence)
    for shoe_id, idx, result, players_online, ws_silence, table_id, table_name, started_at in rows:
        if result not in ("player","banker","tie"):
            continue
        if prev is None or prev[0] != shoe_id:
            prev = (shoe_id, idx, result, players_online, ws_silence)
            continue
        if prev[2] == "tie" or result == "tie":
            prev = (shoe_id, idx, result, players_online, ws_silence)
            continue

        last = "P" if prev[2] == "player" else "B"
        cur_side = "P" if result == "player" else "B"
        cell = (bucket_players(int(prev[3] or 0)), bucket_ws(float(prev[4] or 0.0)))

        # counter
        bet_c = "B" if last == "P" else "P"
        won_c = (cur_side != last)
        p_c = profit_if_win(bet_c) if won_c else -1.0
        st = stats[cell]["counter"]; st[0]+=p_c; st[1]+=1; st[2]+=1 if p_c>0 else 0

        # trend
        bet_t = last
        won_t = (cur_side == last)
        p_t = profit_if_win("B" if bet_t=="B" else "P") if won_t else -1.0
        st = stats[cell]["trend"]; st[0]+=p_t; st[1]+=1; st[2]+=1 if p_t>0 else 0

        prev = (shoe_id, idx, result, players_online, ws_silence)

    def fmt_cell(cell):
        pb, wb = cell
        ptxt = ["0-4","5-9","10-19","20-39","40+"][pb]
        wtxt = ["<2s","2-5","5-15","15-60","60+"][wb]
        return f"players={ptxt} ws={wtxt}"

    rows_out=[]
    for cell, m in stats.items():
        for mode in ("counter","trend"):
            prof,n,w = m[mode]
            if n < 2000:
                continue
            rows_out.append((prof/n, n, w/n if n else 0.0, cell, mode))
    rows_out.sort(reverse=True)
    print("Top cells by avg profit/hand (n>=2000):")
    for avg,n,wr,cell,mode in rows_out[:20]:
        print(f"- {mode:7} {fmt_cell(cell):18} n={n:7,} avgP={avg:+.4f} wr={wr*100:5.2f}%")


if __name__ == "__main__":
    main()
