#!/usr/bin/env python3
"""Probability of a bad ordering with dalembertset $1 (v3 ~50.9%).
Distinguishes 'dipped to -X at some point' (maxDD) vs 'ENDED at <= -X' (final).
Per $1 unit. No loss_cut (raw risk)."""
import sys, os, random, statistics
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))
from dual_line_money import BetManager

P_TIE, P_WIN_DEC, BANKER_FRAC = 0.095, 0.509, 52
N, SEED = 30000, 909


def sim(hands, rng):
    m = BetManager(mode="dalembertset", unit=1.0, seq_set_size=7)
    peak = maxdd = 0.0; win_i = 0
    for _ in range(hands):
        m.next_bet()
        r = rng.random()
        won = None if r < P_TIE else (rng.random() < P_WIN_DEC)
        side = "B" if (won and (win_i % 100) < BANKER_FRAC) else "P"
        if won: win_i += 1
        m.apply_result(won=won, side=side)
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
    return m.session_pnl, maxdd


THRESH = [100, 200, 300, 500, 800, 1200, 2000]
for HANDS, htxt in ((250, "1日 ~250手"), (1250, "1週 ~1250手")):
    rng = random.Random(SEED + HANDS)
    finals, dds = [], []
    for _ in range(N):
        f, d = sim(HANDS, rng)
        finals.append(f); dds.append(d)
    end_neg = 100.0 * sum(1 for f in finals if f < 0) / N
    print(f"\n===== {htxt}  ({N} sessions) =====")
    print(f"  中央PnL={statistics.median(finals):+.0f}  平均PnL={statistics.mean(finals):+.0f}  終了マイナス率={end_neg:.0f}%")
    print(f"  {'しきい値-$X':>12}{'途中で-Xに沈む確率':>22}{'最終が-X以下で終わる確率':>26}")
    for x in THRESH:
        p_dip = 100.0 * sum(1 for d in dds if d >= x) / N
        p_end = 100.0 * sum(1 for f in finals if f <= -x) / N
        onein_dip = f"(1/{int(100/p_dip)})" if p_dip > 0 else "(<1/{})".format(N)
        onein_end = f"(1/{int(100/p_end)})" if p_end > 0 else f"(<1/{N})"
        print(f"  {('-$'+str(x)):>12}{(f'{p_dip:5.2f}%  {onein_dip}'):>22}{(f'{p_end:5.2f}%  {onein_end}'):>26}")
