#!/usr/bin/env python3
"""Is loss_cut=300x unit the right balance for dalembertset? loss_cut also cuts
recovery, so sweep it at both a 1-day and a 1-week session horizon (v3 edge).
Per $1 unit. Bankroll reference $1000 -> loss_cut as % of bankroll shown."""
import sys, os, random, statistics
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))
from dual_line_money import BetManager

P_TIE, P_WIN_DEC, BANKER_FRAC = 0.095, 0.509, 52
N_SESS, SEED, BANKROLL = 8000, 71, 1000


def sim(hands, loss_cut, rng):
    m = BetManager(mode="dalembertset", unit=1.0, loss_cut=loss_cut, on_limit="stop", seq_set_size=7)
    peak = maxdd = 0.0; cut = False; win_i = 0
    for _ in range(hands):
        if m.limit_reached:
            cut = True; break
        m.next_bet()
        r = rng.random()
        won = None if r < P_TIE else (rng.random() < P_WIN_DEC)
        side = "B" if (won and (win_i % 100) < BANKER_FRAC) else "P"
        if won: win_i += 1
        m.apply_result(won=won, side=side)
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
    return m.session_pnl, maxdd, (cut or m.limit_reached)


def run(hands, loss_cut):
    rng = random.Random(SEED + int(loss_cut))
    pnls, dds, cuts = [], [], 0
    for _ in range(N_SESS):
        pnl, dd, cut = sim(hands, loss_cut, rng)
        pnls.append(pnl); dds.append(dd); cuts += 1 if cut else 0
    pnls.sort(); dds.sort(); n = len(pnls)
    return {"mean": statistics.mean(pnls), "median": pnls[n//2], "p05": pnls[int(n*0.05)],
            "worst": pnls[0], "worstDD": dds[-1], "cutpct": 100.0*cuts/n}


for HANDS, htxt in ((250, "1日 ~250手"), (1250, "1週 ~1250手")):
    print(f"\n===== dalembertset $1 — horizon {htxt} ({N_SESS} sessions, v3 ~50.9%) =====")
    print(f"  {'loss_cut':>9}{'(%元本)':>8}{'mean':>8}{'median':>8}{'worst':>9}{'worstDD':>9}{'cut%':>7}")
    base = None
    for lc in (0, 200, 300, 500, 800, 1200):
        r = run(HANDS, lc)
        if lc == 0: base = r
        pct = "" if lc == 0 else f"{100*lc/BANKROLL:.0f}%"
        tag = ""
        if lc != 0 and base:
            dmean = r['mean'] - base['mean']
            tag = f"  (mean {dmean:+.0f} vs none)"
        print(f"  {('none' if lc==0 else '$'+str(lc)):>9}{pct:>8}{r['mean']:>8.1f}{r['median']:>8.1f}"
              f"{r['worst']:>9.0f}{r['worstDD']:>9.0f}{r['cutpct']:>7.1f}{tag}")
