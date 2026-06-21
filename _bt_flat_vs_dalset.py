#!/usr/bin/env python3
"""Honest head-to-head: flat (bigger unit) vs 7-hand dalembertset, on v3 edge.

Claim under test: for the SAME expected profit, flat-at-a-bigger-unit has a
smaller drawdown/tail than dalembertset (because dalembertset stakes more only
AFTER losses = when already underwater). Matched two ways:
  A) match expected profit (flat unit = dalembertset's average stake) -> compare DD
  B) match risk (flat unit s.t. worstDD equals) -> compare profit
"""
import sys, os, random, statistics
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))
from dual_line_money import BetManager

P_TIE = 0.095
P_WIN_DEC = 0.509      # v3 ~50.9% of decided
BANKER_FRAC = 52       # ~52% of v3 wins are Banker (5% commission)
N_SESS = 8000
SEED = 4242


def sim(mode, hands, unit, rng, loss_cut=0.0):
    m = BetManager(mode=mode, unit=unit, loss_cut=loss_cut, on_limit="stop", seq_set_size=7)
    peak = maxdd = maxbet = 0.0
    tot_stake = 0.0
    nbet = 0
    win_i = 0
    for _ in range(hands):
        if m.limit_reached:
            break
        amt = m.next_bet()
        tot_stake += amt; nbet += 1; maxbet = max(maxbet, amt)
        r = rng.random()
        if r < P_TIE:
            won = None
        else:
            won = (rng.random() < P_WIN_DEC)
        side = "B" if (won and (win_i % 100) < BANKER_FRAC) else "P"
        if won:
            win_i += 1
        m.apply_result(won=won, side=side)
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
    return m.session_pnl, maxdd, maxbet, tot_stake, nbet


def run(mode, hands, unit, loss_cut=0.0):
    rng = random.Random(SEED + int(unit * 100) + (1 if mode == "flat" else 0) + int(loss_cut))
    pnls, dds, maxbets, stakes, nbets = [], [], [], 0.0, 0
    for _ in range(N_SESS):
        pnl, dd, mb, ts, nb = sim(mode, hands, unit, rng, loss_cut)
        pnls.append(pnl); dds.append(dd); maxbets.append(mb); stakes += ts; nbets += nb
    pnls.sort(); dds.sort()
    n = len(pnls)
    return {
        "mean": statistics.mean(pnls), "median": pnls[n // 2],
        "p05": pnls[int(n * 0.05)], "worst": pnls[0],
        "medDD": dds[n // 2], "p95DD": dds[int(n * 0.95)], "worstDD": dds[-1],
        "maxBet": max(maxbets), "avgStake": stakes / nbets,
    }


def show(label, r):
    print(f"  {label:<22} mean={r['mean']:+6.1f}  median={r['median']:+6.1f}  worst={r['worst']:8.1f}  "
          f"medDD={r['medDD']:6.1f}  p95DD={r['p95DD']:7.1f}  worstDD={r['worstDD']:8.1f}  "
          f"maxBet={r['maxBet']:6.1f}  RR(mean/p95DD)={r['mean']/r['p95DD'] if r['p95DD'] else 0:5.3f}")


for HANDS, htxt in ((250, "1日 ~250手"), (1250, "1週 ~1250手")):
    print(f"\n========== horizon = {htxt}  ({N_SESS} sessions, v3 ~50.9%, ~52%Banker) ==========")
    dal = run("dalembertset", HANDS, 1.0)
    matched_unit = round(dal["avgStake"], 2)              # flat unit = dalembertset avg stake
    flat1 = run("flat", HANDS, 1.0)
    flatM = run("flat", HANDS, matched_unit)               # A) matched EXPECTED PROFIT
    dal_lc = run("dalembertset", HANDS, 1.0, loss_cut=300) # dalembertset w/ recommended loss_cut

    print(" -- baselines --")
    show("flat $1", flat1)
    show("dalembertset $1", dal)
    show("dalembertset $1 +LC$300", dal_lc)
    print(f" -- A) MATCHED EXPECTED PROFIT: flat unit = dalembertset avg stake = ${matched_unit} --")
    show(f"flat ${matched_unit}", flatM)
    show("dalembertset $1", dal)
    print(f"    => same ~mean profit; compare worstDD ({flatM['worstDD']:.0f} vs {dal['worstDD']:.0f}) "
          f"and maxBet ({flatM['maxBet']:.1f} vs {dal['maxBet']:.0f}).")

    # B) match RISK: find flat unit whose worstDD ~ dalembertset worstDD (flat DD scales linearly with unit)
    risk_unit = round(dal["worstDD"] / flat1["worstDD"], 2)
    flatR = run("flat", HANDS, risk_unit)
    print(f" -- B) MATCHED RISK (worstDD): flat unit = ${risk_unit} (so flat worstDD ~ dalembertset's) --")
    show(f"flat ${risk_unit}", flatR)
    show("dalembertset $1", dal)
    print(f"    => same ~worstDD; compare mean profit ({flatR['mean']:+.1f} vs {dal['mean']:+.1f}).")
