#!/usr/bin/env python3
"""Validate + backtest the new bet123set money mode (1-2-3 per N-hand set).

1) deterministic state-machine check against the user's example
2) Monte-Carlo risk profile vs flat / bet123 / small1 SEQ at the measured v3 edge.
"""
import sys, os, random, statistics
sys.path.insert(0, os.path.dirname(__file__))  # marubatsu_strategy at repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))  # FIRST: edited money
from dual_line_money import BetManager
assert "bet123set" in __import__("dual_line_money").ALLOWED_MODES, "wrong dual_line_money imported"

# v3 measured: ~50.9% win of decided, ~9.5% tie. Banker ~60% of wins (x0.95 commission).
P_TIE = 0.095
P_WIN_DECIDED = 0.509
SEED = 12345


def deterministic_check():
    print("=== state-machine check (user example: set_size=7) ===")
    m = BetManager(mode="bet123set", unit=1.0, seq_set_size=7)
    # Set1: 3 wins 4 losses -> losing set -> step 1->2
    seq1 = [True, False, True, False, False, True, False]  # 3W 4L
    for w in seq1:
        m.next_bet(); m.apply_result(won=w, side="P")
    print(f"after set1 (3W4L, losing): step={m.b123set_step} (expect 2)")
    # Set2: 4 wins 3 losses -> winning set -> step ->1
    seq2 = [True, True, True, True, False, False, False]  # 4W 3L
    for w in seq2:
        m.next_bet(); m.apply_result(won=w, side="P")
    print(f"after set2 (4W3L, winning): step={m.b123set_step} (expect 1)")
    # 3 losing sets in a row from step1 -> 1->2->3->1 (cap reset)
    m2 = BetManager(mode="bet123set", unit=1.0, seq_set_size=7)
    losing = [False, False, False, False, True, True, True]  # 3W4L losing
    for s in range(3):
        for w in losing:
            m2.next_bet(); m2.apply_result(won=w, side="P")
        print(f"  losing set {s+1}: step={m2.b123set_step}")
    print(f"after 3 losing sets: step={m2.b123set_step} (expect 1 = cycle reset, never blows up)")
    print(f"bet at each step: unit*step -> max bet = unit*3 (bounded)\n")

    print("=== dalembertset state-machine check (set_size=7) ===")
    d = BetManager(mode="dalembertset", unit=1.0, seq_set_size=7)
    losing = [False, False, False, False, True, True, True]  # 3W4L losing set
    winning = [True, True, True, True, False, False, False]  # 4W3L winning set
    for s, seq in enumerate([losing, losing, losing, winning, winning]):
        for w in seq:
            d.next_bet(); d.apply_result(won=w, side="P")
        print(f"  set{s+1} ({'負け越し' if seq is losing else '勝ち越し'}): level={d.dalembertset_level}")
    print("  expect: 2,3,4,3,2 (+1 on losing set, -1 on winning, floor 1, NO cap)\n")


def one_hand(rng):
    if rng.random() < P_TIE:
        return None  # tie
    return rng.random() < P_WIN_DECIDED


def sim_session(mode, hands, unit, rng, **kw):
    m = BetManager(mode=mode, unit=unit, seq_set_size=7, **kw)
    pnl_curve = []
    peak = 0.0
    max_dd = 0.0
    max_bet = 0.0
    for _ in range(hands):
        amt = m.next_bet()
        max_bet = max(max_bet, amt)
        won = one_hand(rng)
        side = "B" if (won and rng.random() < 0.60) else "P"
        m.apply_result(won=won, side=side)
        pnl_curve.append(m.session_pnl)
        peak = max(peak, m.session_pnl)
        max_dd = max(max_dd, peak - m.session_pnl)
    return {"pnl": m.session_pnl, "max_dd": max_dd, "max_bet": max_bet, "bets": m.total_bets}


def montecarlo(mode, n_sessions=4000, hands=700, unit=1.0):
    rng = random.Random(SEED + hash(mode) % 1000)
    pnls, dds, maxbets = [], [], []
    for _ in range(n_sessions):
        r = sim_session(mode, hands, unit, rng)
        pnls.append(r["pnl"]); dds.append(r["max_dd"]); maxbets.append(r["max_bet"])
    pnls.sort(); dds.sort()
    n = len(pnls)
    return {
        "mode": mode,
        "mean_pnl": statistics.mean(pnls),
        "median_pnl": pnls[n // 2],
        "p05_pnl": pnls[int(n * 0.05)],
        "p95_pnl": pnls[int(n * 0.95)],
        "worst_pnl": pnls[0],
        "median_dd": dds[n // 2],
        "p95_dd": dds[int(n * 0.95)],
        "worst_dd": dds[-1],
        "max_bet": max(maxbets),
        "neg_days_pct": 100.0 * sum(1 for p in pnls if p < 0) / n,
    }


def main():
    deterministic_check()
    print("=== Monte-Carlo: 4000 sessions x 700 hands, $1 unit, v3 edge (~50.9% / 9.5% tie) ===")
    print(f"{'mode':<11}{'mean':>8}{'median':>8}{'p05':>8}{'p95':>8}{'worst':>9}{'medDD':>8}{'p95DD':>8}{'worstDD':>9}{'maxBet':>8}{'neg%':>7}")
    for mode in ("flat", "bet123", "bet123set", "dalembertset", "small1"):
        r = montecarlo(mode)
        print(f"{r['mode']:<11}{r['mean_pnl']:>8.1f}{r['median_pnl']:>8.1f}{r['p05_pnl']:>8.1f}"
              f"{r['p95_pnl']:>8.1f}{r['worst_pnl']:>9.1f}{r['median_dd']:>8.1f}{r['p95_dd']:>8.1f}"
              f"{r['worst_dd']:>9.1f}{r['max_bet']:>8.1f}{r['neg_days_pct']:>7.1f}")
    print("\nnote: max_bet shows the bounded exposure. bet123set must show max_bet=3 (unit*3, never blows up).")


if __name__ == "__main__":
    main()
