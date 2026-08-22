#!/usr/bin/env python3
"""_card_ml_probe_v1.py — assumption-free predictability net (test 3).

Question: can ANY function of the observable card/outcome history predict the next
hand better than chance? Targets:
  T1: next winner (B vs P, ex-tie)   — direct bet edge if lift exists
  T2: next hand is TIE               — 8:1 payout, breakeven 11.11%

Features (strictly causal, per table): rank counts in last 52/156 cards, last 8
ex-tie outcomes, streak, prev-hand score/margin/cards/naturals/third-card values,
rolling B-rate(20/50), nat count(10), comeback count(10), hour-of-day (JST).

Split: time-ordered — train < 2026-07-17 00:00 JST, test >= (true OOS, no shuffling).
Models: HistGradientBoosting (nonlinear interactions) + LogisticRegression (linear).
Report: accuracy/AUC vs base rate + confidence-decile table (edge would concentrate
in the confident tail even if average lift is ~0).
"""
import json, glob, math, time
import numpy as np

T0 = time.time()
RANK_IDX = {'1':0,'2':1,'3':2,'4':3,'5':4,'6':5,'7':6,'8':7,'9':8,'0':9,'J':10,'Q':11,'K':12}
VAL = [1,2,3,4,5,6,7,8,9,0,0,0,0]
CUT_TS = 1784214000.0   # 2026-07-17 00:00 JST = 2026-07-16 15:00 UTC

from collections import defaultdict, deque
tables = defaultdict(list)
seen_g = set()
for f in sorted(glob.glob('/opt/laplace2/card_feed.jsonl*')):
    for line in open(f, encoding='utf-8', errors='replace'):
        try: d = json.loads(line)
        except Exception: continue
        if d.get('k') != 'r': continue
        g = d.get('g')
        if not g or g in seen_g: continue
        pc, bc = d.get('pc') or [], d.get('bc') or []
        if len(pc) < 2 or len(bc) < 2: continue
        if any(len(str(c)) != 2 or str(c)[0] not in RANK_IDX for c in pc + bc): continue
        win = d.get('win')
        if win not in ('B', 'P', 'T'): continue
        seen_g.add(g)
        tables[str(d.get('tb'))].append((float(d.get('ts') or 0), win,
                                         int(d.get('ps')), int(d.get('bs')),
                                         [str(c) for c in pc], [str(c) for c in bc]))
seen_g = None
NH = sum(len(v) for v in tables.values())
print(f"[load] hands={NH} tables={len(tables)} ({time.time()-T0:.0f}s)", flush=True)

X, yW, yT, TS = [], [], [], []
for tb in sorted(tables):
    hands = sorted(tables[tb], key=lambda x: x[0])
    cards = deque(maxlen=156)          # rank idx stream
    cnt52 = [0]*13
    q52 = deque(maxlen=52)
    ex = deque(maxlen=50)              # ex-tie outcomes 1=B
    nat10 = deque(maxlen=10)
    cb10 = deque(maxlen=10)
    prev = None
    streak = 0
    last_ts = None
    for (ts, win, ps, bs, pc, bc) in hands:
        if last_ts is not None and ts - last_ts > 120.0:
            cards.clear(); q52.clear(); cnt52 = [0]*13
            ex.clear(); nat10.clear(); cb10.clear(); prev = None; streak = 0
        last_ts = ts
        # emit sample (predict this hand) if history is warm
        if prev is not None and len(q52) == 52 and len(cards) == 156 and len(ex) >= 20:
            (pps, pbs, ppc, pbc, pwin) = prev
            cnt156 = [0]*13
            for r in cards: cnt156[r] += 1
            pt2 = (VAL[RANK_IDX[ppc[0][0]]] + VAL[RANK_IDX[ppc[1][0]]]) % 10
            bt2 = (VAL[RANK_IDX[pbc[0][0]]] + VAL[RANK_IDX[pbc[1][0]]]) % 10
            exl = list(ex)
            h = (ts/3600 + 9) % 24
            feats = (
                cnt52 + cnt156 +
                exl[-8:] +
                [streak,
                 pps, pbs, abs(pps-pbs), len(ppc)+len(pbc),
                 1 if pt2 >= 8 else 0, 1 if bt2 >= 8 else 0, 1 if pwin == 'T' else 0,
                 VAL[RANK_IDX[ppc[2][0]]] if len(ppc) > 2 else -1,
                 VAL[RANK_IDX[pbc[2][0]]] if len(pbc) > 2 else -1,
                 sum(exl[-20:])/20.0, sum(exl)/len(exl),
                 sum(nat10), sum(cb10),
                 math.sin(2*math.pi*h/24), math.cos(2*math.pi*h/24)]
            )
            X.append(feats)
            yW.append(1 if win == 'B' else (0 if win == 'P' else -1))
            yT.append(1 if win == 'T' else 0)
            TS.append(ts)
        # update state with this hand
        pt2 = (VAL[RANK_IDX[pc[0][0]]] + VAL[RANK_IDX[pc[1][0]]]) % 10
        bt2 = (VAL[RANK_IDX[bc[0][0]]] + VAL[RANK_IDX[bc[1][0]]]) % 10
        nat10.append(1 if (pt2 >= 8 or bt2 >= 8) else 0)
        cb = 0
        if (len(pc)+len(bc)) > 4 and win != 'T' and pt2 != bt2:
            cb = 1 if win != ('P' if pt2 > bt2 else 'B') else 0
        cb10.append(cb)
        if win != 'T':
            b = 1 if win == 'B' else 0
            if ex and ex[-1] == b: streak += 1
            else: streak = 1
            if win == 'P': streak_signed = -streak
            ex.append(b)
        seq = [pc[0], bc[0], pc[1], bc[1]] + ([pc[2]] if len(pc) > 2 else []) + ([bc[2]] if len(bc) > 2 else [])
        for card in seq:
            r = RANK_IDX[card[0]]
            cards.append(r)
            q52.append(r)
            cnt52[r] += 1
            if len(q52) == 52 and len(q52) == q52.maxlen and sum(cnt52) > 52:
                pass
        # maintain cnt52 correctly (deque popped silently) -> rebuild cheaply
        if len(q52) == 52:
            # incremental: subtract popped items; deque with maxlen pops silently,
            # so rebuild every hand (52 ops, cheap)
            cnt52 = [0]*13
            for r in q52: cnt52[r] += 1
        prev = (ps, bs, pc, bc, win)

X = np.asarray(X, dtype=np.float32)
yW = np.asarray(yW); yT = np.asarray(yT); TS = np.asarray(TS)
tr = TS < CUT_TS; te = ~tr
print(f"[features] X={X.shape} train={tr.sum()} test={te.sum()} ({time.time()-T0:.0f}s)", flush=True)

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

def decile_table(p, y, label, payout_note):
    print(f"  confidence deciles [{label}] (test):")
    order = np.argsort(p)
    n = len(p)
    for dec in (0, 8, 9):
        lo = order[int(n*dec/10):int(n*(dec+1)/10)]
        r = y[lo].mean()
        print(f"    decile {dec+1}: pred[{p[lo].min():.3f}-{p[lo].max():.3f}] actual={r*100:.2f}% n={len(lo)}")
    print(f"    ({payout_note})")

# T1: winner B vs P (ex-tie)
m = yW >= 0
Xw, yw, trw, tew = X[m], yW[m], tr[m], te[m]
base = yw[tew].mean()
print(f"\n=== T1 next winner (ex-tie)  test n={tew.sum()}  base B-rate={base*100:.2f}% ===")
for name, clf in (
    ("HGB", HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08,
                                           max_leaf_nodes=63, min_samples_leaf=200,
                                           validation_fraction=0.1, random_state=7)),
    ("LogReg", LogisticRegression(max_iter=2000, C=0.5)),
):
    t1 = time.time()
    clf.fit(Xw[trw], yw[trw])
    p = clf.predict_proba(Xw[tew])[:, 1]
    acc = ((p > 0.5).astype(int) == yw[tew]).mean()
    auc = roc_auc_score(yw[tew], p)
    # accuracy of majority-class baseline
    accb = max(base, 1-base)
    nte = tew.sum()
    zacc = (acc - accb) / math.sqrt(accb*(1-accb)/nte)
    print(f"  {name}: test acc={acc*100:.2f}% (base {accb*100:.2f}%, z={zacc:+.2f})  AUC={auc:.4f}  ({time.time()-t1:.0f}s)")
    decile_table(p, yw[tew], name, "edge needs top-decile >51.3%(B bet, after 5% comm) or <48.0%(P bet)")

# T2: tie
print(f"\n=== T2 next hand TIE  test n={te.sum()}  base tie-rate={yT[te].mean()*100:.2f}% ===")
clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=63,
                                     min_samples_leaf=200, validation_fraction=0.1, random_state=7)
clf.fit(X[tr], yT[tr])
p = clf.predict_proba(X[te])[:, 1]
auc = roc_auc_score(yT[te], p)
print(f"  HGB: AUC={auc:.4f}")
decile_table(p, yT[te], "HGB-tie", "tie bet pays 8:1 -> breakeven 11.11%")
print(f"\n[done] {time.time()-T0:.0f}s")
