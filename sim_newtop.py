"""新Top10候補での再シミュレーション。
3-filter シミュで生存上位だったテーブルを新推奨リストにして比較。
"""
import sys
sys.path.insert(0, '.')
from sim_3filter import sim_filtered, START
import sqlite3
from collections import defaultdict

DB = 'analytics_vps.sqlite3'

# 現状のTop推奨 (sync_pause デフォルト)
CURRENT_TOP = [
    'Japanese Speed Baccarat A',
    'Korean Speed Baccarat H',
    'Korean Speed Baccarat B',
    'Korean Speed Baccarat A',
    'Korean Speed Baccarat E',
]

# 3-filter シミュで生存上位だった新候補 Top 10
NEW_TOP_10 = [
    'Korean Speed Baccarat A',
    'Speed Baccarat W',
    'Korean Speed Baccarat D',
    'Speed Baccarat X',
    'Japanese Speed Baccarat A',
    'Lotus Speed Baccarat A',
    'Thai Speed Baccarat B',
    'Lotus Speed Baccarat B',
    'Baccarat B',
    'Speed Baccarat T',
]

NEW_TOP_5 = NEW_TOP_10[:5]
NEW_TOP_15 = NEW_TOP_10 + [
    'Stake Exclusive Speed Baccarat 1',
    'Dynasty Speed Baccarat 1',
    'Dynasty Speed Baccarat 8',
    'Korean Speed Baccarat E',
    'Japanese Speed Baccarat C',
]


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT table_name, result_sequence, started_at FROM shoes_analytics WHERE hand_count >= 50 ORDER BY started_at')
    shoes_by_table = defaultdict(list)
    for tn, seq, ts in cur.fetchall():
        shoes_by_table[tn].append((seq, ts))
    conn.close()

    sf = {tn: s for tn, s in shoes_by_table.items() if len(s) >= 30}
    print(f'Total tables in DB: {len(sf)}\n')

    def aggregate(tables, **kwargs):
        b = s = bal = w = 0
        results_per_table = []
        for tn in tables:
            if tn not in sf:
                continue
            r = sim_filtered(sf[tn], START, **kwargs)
            results_per_table.append((tn, r))
            if r['bankrupt']:
                b += 1
            else:
                s += 1
            bal += r['final']
            w += r['wins']
        n_eval = b + s
        return b, s, bal, w, n_eval, results_per_table

    cases = [
        ('CURRENT Top 5 / sync_pause', CURRENT_TOP, dict(use_reg=False, use_pb=False, use_pause=True)),
        ('CURRENT Top 5 / 3-filter', CURRENT_TOP, dict(use_reg=True, use_pb=True, use_pause=True)),
        ('NEW Top 5 / sync_pause', NEW_TOP_5, dict(use_reg=False, use_pb=False, use_pause=True)),
        ('NEW Top 5 / 3-filter', NEW_TOP_5, dict(use_reg=True, use_pb=True, use_pause=True)),
        ('NEW Top 10 / sync_pause', NEW_TOP_10, dict(use_reg=False, use_pb=False, use_pause=True)),
        ('NEW Top 10 / 3-filter', NEW_TOP_10, dict(use_reg=True, use_pb=True, use_pause=True)),
        ('NEW Top 15 / sync_pause', NEW_TOP_15, dict(use_reg=False, use_pb=False, use_pause=True)),
        ('NEW Top 15 / 3-filter', NEW_TOP_15, dict(use_reg=True, use_pb=True, use_pause=True)),
    ]

    print(f'{"Strategy":<35} {"Bnkrt":>6} {"Surv":>5} {"#":>4} {"Total bal":>13} {"P&L":>13} {"Wins":>6}')
    print('=' * 100)
    for name, tables, kwargs in cases:
        b, s, bal, w, n, _ = aggregate(tables, **kwargs)
        pl = bal - n * START
        sign = '+' if pl >= 0 else ''
        print(f'{name:<35} {b:>6} {s:>5} {n:>4} ${bal:>11,} {sign}${pl:>11,} {w:>6,}')

    # 詳細: NEW Top 10 / sync_pause の個別結果
    print('\n=== NEW Top 10 / sync_pause 個別結果 ===')
    _, _, _, _, _, results = aggregate(NEW_TOP_10, use_reg=False, use_pb=False, use_pause=True)
    for tn, r in results:
        flag = 'X' if r['bankrupt'] else 'O'
        name = tn.encode('ascii', errors='replace').decode('ascii')
        pl = r['final'] - START
        sign = '+' if pl >= 0 else ''
        print(f'  [{flag}] {name:<42} ${r["final"]:>9,}  {sign}${pl:>7,}  W{r["wins"]:>4}')

    # 詳細: NEW Top 10 / 3-filter の個別結果
    print('\n=== NEW Top 10 / 3-filter 個別結果 ===')
    _, _, _, _, _, results = aggregate(NEW_TOP_10, use_reg=True, use_pb=True, use_pause=True)
    for tn, r in results:
        flag = 'X' if r['bankrupt'] else 'O'
        name = tn.encode('ascii', errors='replace').decode('ascii')
        pl = r['final'] - START
        sign = '+' if pl >= 0 else ''
        print(f'  [{flag}] {name:<42} ${r["final"]:>9,}  {sign}${pl:>7,}  W{r["wins"]:>4}')


if __name__ == '__main__':
    main()
