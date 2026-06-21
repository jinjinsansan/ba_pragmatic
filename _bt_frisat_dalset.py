#!/usr/bin/env python3
"""Replay the real v3 (6-pattern) results of Fri 2026-06-20 + Sat 2026-06-21
through dalembertset (7-hand) vs flat vs bet123set. Reconstructs the ordered
W/L/T sequence by diffing the bot-log cumulative resolved/W/L snapshots."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))
from dual_line_money import BetManager
assert "dalembertset" in __import__("dual_line_money").ALLOWED_MODES

rows = []
for line in open("_v3_fri_sat.txt", encoding="utf-8"):
    p = line.split()
    if len(p) < 6:
        continue
    rows.append((p[0], int(p[3]), int(p[4]), int(p[5])))  # date, resolved, w, l
rows.sort(key=lambda r: r[1])

seq_by_day = {}
prev = None
for date, resolved, w, l in rows:
    if prev is None:
        prev = (resolved, w, l); continue
    pr, pw, pl = prev
    dw, dl = w - pw, l - pl
    dt = (resolved - pr) - dw - dl
    seq = seq_by_day.setdefault(date, [])
    # interleave W/L within a multi-resolution step, ties last (ties don't affect sets)
    n = max(dw, dl)
    for i in range(n):
        if i < dw: seq.append("W")
        if i < dl: seq.append("L")
    for _ in range(max(0, dt)): seq.append("T")
    prev = (resolved, w, l)

DAYS = {"2026-06-20": "金 Fri", "2026-06-21": "土 Sat"}
BANKER_FRAC = 52  # v3 bets ~52% Banker -> 5% commission on those wins


def run(mode, seq, set_size=7):
    m = BetManager(mode=mode, unit=1.0, seq_set_size=set_size)
    peak = maxdd = maxbet = 0.0
    maxlevel = 1
    win_i = 0
    for r in seq:
        amt = m.next_bet(); maxbet = max(maxbet, amt)
        if r == "T":
            m.apply_result(won=None)
        elif r == "W":
            side = "B" if (win_i % 100) < BANKER_FRAC else "P"; win_i += 1
            m.apply_result(won=True, side=side)
        else:
            m.apply_result(won=False, side="P")
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
        if mode == "dalembertset": maxlevel = max(maxlevel, m.dalembertset_level)
        elif mode == "bet123set": maxlevel = max(maxlevel, m.b123set_step)
    return m.session_pnl, maxbet, maxdd, maxlevel, m.total_wins, m.total_losses, m.total_ties


print("=== real v3 (6-pattern) results — 2026-06-20 Fri + 2026-06-21 Sat ===")
print("(per $1 unit; ~52% Banker commission applied on wins)\n")
total = {}
for date in sorted(seq_by_day):
    seq = seq_by_day[date]
    w = seq.count("W"); l = seq.count("L"); t = seq.count("T")
    wr = 100 * w / (w + l) if (w + l) else 0
    print(f"--- {date} ({DAYS.get(date,'')}) : {w}W/{l}L/{t}T  勝率 {wr:.2f}%  決着{w+l} ---")
    print(f"  {'mode':<13}{'PnL$':>9}{'maxBet$':>9}{'maxDD$':>9}{'max段/level':>12}")
    for mode in ("flat", "bet123set", "dalembertset"):
        pnl, mb, dd, ml, mw, mll, mt = run(mode, seq)
        total.setdefault(mode, [0, 0, 0]); total[mode][0] += pnl; total[mode][1] = max(total[mode][1], mb)
        print(f"  {mode:<13}{pnl:>9.2f}{mb:>9.0f}{dd:>9.2f}{ml:>12}")
    print()

print("=== 2日合計（金→土を連続運用＝資金は持ち越し前提でなく各日リセットの単純合算）===")
print(f"  {'mode':<13}{'2日PnL$':>10}{'2日中最大Bet$':>14}")
for mode in ("flat", "bet123set", "dalembertset"):
    print(f"  {mode:<13}{total[mode][0]:>10.2f}{total[mode][1]:>14.0f}")

# continuous run (carry level/state across both days, as a real session would)
print("\n=== 連続運用（金→土を1セッションとして段/状態を持ち越し）===")
allseq = seq_by_day.get("2026-06-20", []) + seq_by_day.get("2026-06-21", [])
for mode in ("flat", "bet123set", "dalembertset"):
    pnl, mb, dd, ml, mw, mll, mt = run(mode, allseq)
    print(f"  {mode:<13} PnL=${pnl:.2f}  maxBet=${mb:.0f}  maxDD=${dd:.2f}  max段/level={ml}")

print("\n=== ★7ターン vs 5ターン ダランベール（あなたの懸念=5ターンは悪日で痛手か）===")
print("  (連続運用・金→土)")
for ss in (7, 5):
    pnl, mb, dd, ml, mw, mll, mt = run("dalembertset", allseq, set_size=ss)
    print(f"  dalembertset {ss}ターン: PnL=${pnl:.2f}  maxBet=${mb:.0f}  maxDD=${dd:.2f}  max level={ml}")
