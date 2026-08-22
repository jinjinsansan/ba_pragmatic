"""3-filter simulation: 正しいGUI仕様準拠版

GUI動作:
  - sync_pause: BB→観戦, P→再開
  - フィルタ exit = テーブル退避 = 観戦と同じ扱い (BETしないだけ)
  - MaruBatsu状態は持続 (+50利確 or 破綻でのみリセット)
"""
import sys
sys.path.insert(0, '.')
from generate_equity_report import MaruBatsuSim
from regularity_monitor import compute_regularity, count_non_tie, count_pb
import sqlite3
from collections import defaultdict

DB = 'analytics_vps.sqlite3'
START = 10000
TARGET = 50

ENTRY_REG = 70
EXIT_REG = 65
ENTRY_HANDS = 35
ENTRY_P_RATIO = 0.42
EXIT_P_RATIO = 0.38
PAUSE_THRESHOLD = 2


def sim_filtered(shoes, start_capital, target=TARGET, use_reg=True, use_pb=True, use_pause=True):
    """GUI仕様準拠 — 全シューを1つの連続シーケンスとして扱う:

    - 各シューを連結 (シュー境界もスライド窓的に判定)
    - フィルタは「直近のシュー」のbead roadで評価 (シュー切替時にリセット)
    - フィルタ通らない時は観戦 (BETしない、MaruBatsu状態は保持)
    - sync_pause: BB→観戦, P→再開 (MaruBatsu状態は保持)
    - MaruBatsuは +50利確 OR 破綻でのみリセット
    """
    balance = start_capital
    sim = MaruBatsuSim(target=target, lc=10**12)
    bankrupt = False
    wins = losses = 0
    pause_events = 0
    bet_hands = 0
    skip_hands = 0
    consec_b = 0
    paused = False
    in_table = False  # フィルタ通過状態

    for seq, _ts in shoes:
        if bankrupt:
            break
        # シュー切替時 — bead road リセット
        history = []
        # シュー切替で in_table を False に戻す (新シュー先頭は判定不能なので)
        in_table = False
        consec_b = 0
        paused = False

        results = [c for c in seq if c in ('P', 'B', 'T')]

        for r in results:
            hands = count_non_tie(history)
            reg = compute_regularity(history) if (hands >= 5 and use_reg) else 100
            pc, bc = count_pb(history)
            p_ratio = pc / (pc + bc) if (pc + bc) > 0 else 0.5
            if not use_pb:
                p_ratio = 0.5

            # フィルタ判定
            if in_table:
                exit_now = False
                if hands < ENTRY_HANDS:
                    exit_now = True
                if use_reg and reg < EXIT_REG:
                    exit_now = True
                if use_pb and p_ratio < EXIT_P_RATIO:
                    exit_now = True
                if exit_now:
                    in_table = False
                    # 観戦継続 (MaruBatsu状態は保持)
                    consec_b = 0
                    paused = False
            else:
                ok = hands >= ENTRY_HANDS
                if use_reg and reg < ENTRY_REG:
                    ok = False
                if use_pb and p_ratio < ENTRY_P_RATIO:
                    ok = False
                if ok:
                    in_table = True

            if r == 'T':
                history.append(r)
                continue

            if not in_table:
                history.append(r)
                skip_hands += 1
                continue

            # paused (sync_pause観戦中)
            if paused:
                if r == 'B':
                    consec_b += 1
                elif r == 'P':
                    paused = False
                    consec_b = 0
                    pause_events += 1
                history.append(r)
                continue

            # BET (not paused, in_table)
            sim.add(r)
            bet_hands += 1
            if balance + sim.cumulative <= 0:
                losses += 1
                bankrupt = True
                balance = 0
                history.append(r)
                break
            if sim.cumulative >= target:
                balance += sim.cumulative
                wins += 1
                sim.reset()
                consec_b = 0
                paused = False

            if use_pause:
                if r == 'B':
                    consec_b += 1
                    if consec_b >= PAUSE_THRESHOLD:
                        paused = True
                elif r == 'P':
                    consec_b = 0

            history.append(r)

    return {'final': balance, 'bankrupt': bankrupt, 'wins': wins, 'losses': losses,
            'pauses': pause_events, 'bet_hands': bet_hands, 'skip': skip_hands}


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT table_name, result_sequence, started_at FROM shoes_analytics WHERE hand_count >= 50 ORDER BY started_at')
    shoes_by_table = defaultdict(list)
    for tn, seq, ts in cur.fetchall():
        shoes_by_table[tn].append((seq, ts))
    conn.close()

    sf = {tn: s for tn, s in shoes_by_table.items() if len(s) >= 30}
    print(f'Tables: {len(sf)}\n')

    TOP4 = ['Japanese Speed Baccarat A', 'Korean Speed Baccarat B',
            'Korean Speed Baccarat A', 'Korean Speed Baccarat E']

    def aggregate(tables, **kwargs):
        b = s = bal = w = pe = bh = sk = 0
        for tn in tables:
            r = sim_filtered(sf[tn], START, **kwargs)
            if r['bankrupt']:
                b += 1
            else:
                s += 1
            bal += r['final']
            w += r['wins']
            pe += r['pauses']
            bh += r['bet_hands']
            sk += r['skip']
        return b, s, bal, w, pe, bh, sk

    all_tables = list(sf.keys())
    top4 = [t for t in TOP4 if t in sf]

    cases = [
        ('Top 4 / sync_pause only', top4, dict(use_reg=False, use_pb=False, use_pause=True)),
        ('Top 4 / 3-filter (Reg+P/B+Pause)', top4, dict(use_reg=True, use_pb=True, use_pause=True)),
        ('All 62 / sync_pause only', all_tables, dict(use_reg=False, use_pb=False, use_pause=True)),
        ('All 62 / P/B + pause', all_tables, dict(use_reg=False, use_pb=True, use_pause=True)),
        ('All 62 / Reg + pause', all_tables, dict(use_reg=True, use_pb=False, use_pause=True)),
        ('All 62 / 3-filter (all)', all_tables, dict(use_reg=True, use_pb=True, use_pause=True)),
    ]

    print(f'{"Strategy":<40} {"Bankr":>6} {"Surv":>6} {"Total bal":>13} {"P&L":>13} {"Wins":>7}')
    print('=' * 100)
    for name, tables, kwargs in cases:
        b, s, bal, w, pe, bh, sk = aggregate(tables, **kwargs)
        n = len(tables)
        pl = bal - n * START
        sign = '+' if pl >= 0 else ''
        print(f'{name:<40} {b:>6} {s:>6} ${bal:>11,} {sign}${pl:>11,} {w:>7,}')

    # 全テーブル個別 (3-filter)
    print('\n=== 全62テーブル 個別 (3-filter all) ===')
    indiv = [(tn, sim_filtered(sf[tn], START)) for tn in all_tables]
    indiv.sort(key=lambda x: -x[1]['final'])
    print('Top 15:')
    for tn, r in indiv[:15]:
        flag = 'X' if r['bankrupt'] else 'O'
        name = tn.encode('ascii', errors='replace').decode('ascii')[:42]
        print(f'  [{flag}] {name:<42} ${r["final"]:>9,} W{r["wins"]:>4}')
    print('Worst 15:')
    for tn, r in indiv[-15:]:
        flag = 'X' if r['bankrupt'] else '!'
        name = tn.encode('ascii', errors='replace').decode('ascii')[:42]
        print(f'  [{flag}] {name:<42} ${r["final"]:>9,} W{r["wins"]:>4}')


if __name__ == '__main__':
    main()
