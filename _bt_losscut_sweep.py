#!/usr/bin/env python3
"""Find an appropriate loss_cut for dalembertset (7-hand). Two views, per $1 unit:
 1) the REAL bad Fri+Sat sequence: does loss_cut let it recover, or cut the recoverable dip?
 2) Monte-Carlo (realistic ~50.9% and stress 49.0%): mean PnL / worst loss / how often cut."""
import sys, os, random, statistics
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))
from dual_line_money import BetManager

# --- reconstruct real Fri+Sat sequence (same as _bt_frisat_dalset) ---
rows = []
for line in open("_v3_fri_sat.txt", encoding="utf-8"):
    p = line.split()
    if len(p) >= 6:
        rows.append((p[0], int(p[3]), int(p[4]), int(p[5])))
rows.sort(key=lambda r: r[1])
realseq, prev = [], None
for _d, resolved, w, l in rows:
    if prev is None:
        prev = (resolved, w, l); continue
    pr, pw, pl = prev
    dw, dl = w - pw, l - pl
    dt = (resolved - pr) - dw - dl
    n = max(dw, dl)
    for i in range(n):
        if i < dw: realseq.append("W")
        if i < dl: realseq.append("L")
    for _ in range(max(0, dt)): realseq.append("T")
    prev = (resolved, w, l)


def play(seq, loss_cut, unit=1.0):
    m = BetManager(mode="dalembertset", unit=unit, loss_cut=loss_cut, on_limit="stop", seq_set_size=7)
    peak = maxdd = 0.0
    cut = False
    for r in seq:
        if m.limit_reached:
            cut = True; break
        m.next_bet()
        m.apply_result(won=(None if r == "T" else (r == "W")), side=("B" if r == "W" else "P"))
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
    return m.session_pnl, maxdd, (cut or m.limit_reached)


print("=== 1) REAL bad Fri+Sat (per $1 unit) — does loss_cut preserve the recovery? ===")
print(f"  {'loss_cut':>9}{'finalPnL$':>11}{'maxDD$':>9}{'cut?':>6}")
for lc in (0, 75, 100, 134, 150, 200, 300):
    pnl, dd, cut = play(realseq, lc)
    print(f"  {('none' if lc==0 else '$'+str(lc)):>9}{pnl:>11.2f}{dd:>9.2f}{('CUT' if cut else '-'):>6}")
print("  (loss_cut=0 = none. note the real dip was ~$134 before it recovered to +$21.)\n")

# --- Monte-Carlo ---
def mc(p_win_dec, loss_cut, n=4000, hands=500):
    rng = random.Random(99 + int(loss_cut))
    pnls, cuts = [], 0
    for _ in range(n):
        seq = []
        for _h in range(hands):
            if rng.random() < 0.095: seq.append("T")
            else: seq.append("W" if rng.random() < p_win_dec else "L")
        pnl, dd, cut = play(seq, loss_cut)
        pnls.append(pnl); cuts += 1 if cut else 0
    pnls.sort()
    return (statistics.mean(pnls), pnls[len(pnls)//2], pnls[int(len(pnls)*0.05)],
            pnls[0], 100.0*cuts/n)

for label, pw in (("realistic ~50.9%", 0.509), ("stress 49.0% (bad day)", 0.490)):
    print(f"=== 2) Monte-Carlo {label} — 4000x500 hands, per $1 unit ===")
    print(f"  {'loss_cut':>9}{'mean$':>8}{'median$':>9}{'p05$':>8}{'worst$':>9}{'cut%':>7}")
    for lc in (0, 100, 150, 200, 300, 500):
        mean, med, p05, worst, cutpct = mc(pw, lc)
        print(f"  {('none' if lc==0 else '$'+str(lc)):>9}{mean:>8.1f}{med:>9.1f}{p05:>8.1f}{worst:>9.1f}{cutpct:>7.1f}")
    print()
