#!/usr/bin/env python3
"""_card_blanksheet_v1.py — blank-slate edge scan on raw card feed (no v3/v4 pattern assumptions).

Null model = physical 8-deck standard baccarat with memoryless shuffle.
This is a measurement zero-point, NOT a conclusion.

Battery:
  A. Marginals vs theory/MC: winner rates, tie, naturals, pairs, rank freq, score dist, cards/hand
  B. Physics: third-card tableau compliance, score consistency
  C. Sequence (per-table, causal): lag-k winner Markov, streak survival, order-2 Markov,
     prev-hand features -> next winner, natural autocorrelation (positive control),
     rolling 60-card composition -> next natural / next winner
  D. Time/entity: hour-of-day, day, per-table heterogeneity (overdispersion),
     30-min block pooled overdispersion (global sync), game-id digit
  E. Drama: comeback rate (leader after 4 cards loses) vs MC

Usage: python3 _card_blanksheet_v1.py [--mc 300000]
"""
import sys, json, glob, math, random, time
from collections import defaultdict, deque, Counter

T0 = time.time()
MC_N = 300000
for i, a in enumerate(sys.argv):
    if a == '--mc' and i + 1 < len(sys.argv):
        MC_N = int(sys.argv[i + 1])

VAL = {'1':1,'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'0':0,'J':0,'Q':0,'K':0}
RANKS = ['1','2','3','4','5','6','7','8','9','0','J','Q','K']

# theoretical 8-deck
TH_B, TH_P, TH_T = 0.458597, 0.446247, 0.095156
TH_PAIR = 31.0/415.0

def z2p(p1, n1, p2, n2):
    """two-proportion z (group1 - group2)"""
    if n1 == 0 or n2 == 0: return 0.0
    p = (p1*n1 + p2*n2) / (n1 + n2)
    se = math.sqrt(p*(1-p)*(1.0/n1 + 1.0/n2)) or 1e-12
    return (p1 - p2) / se

def z1(p_obs, n, p0):
    if n == 0: return 0.0
    se = math.sqrt(p0*(1-p0)/n) or 1e-12
    return (p_obs - p0) / se

# ── load ─────────────────────────────────────────────────────────────
files = sorted(glob.glob('/opt/laplace2/card_feed.jsonl*'))
tables = defaultdict(list)   # tb -> list of (ts, win, ps, bs, pr, br) pr/br = rank strings
seen_g = set()
n_raw = n_dup = n_bad = 0
for f in files:
    with open(f, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            n_raw += 1
            try:
                r = json.loads(line)
            except Exception:
                n_bad += 1; continue
            g = r.get('g')
            if not g or g in seen_g:
                n_dup += 1; continue
            pc, bc = r.get('pc'), r.get('bc')
            win = r.get('win')
            if not pc or not bc or win not in ('B','P','T'):
                n_bad += 1; continue
            if any(len(c) != 2 for c in pc + bc):
                n_bad += 1; continue
            try:
                pr = ''.join(c[0] for c in pc)
                br = ''.join(c[0] for c in bc)
                ps, bs = int(r.get('ps')), int(r.get('bs'))
                ts = float(r.get('ts'))
            except Exception:
                n_bad += 1; continue
            if not all(ch in VAL for ch in pr + br):
                n_bad += 1; continue
            seen_g.add(g)
            tables[r.get('tb') or '?'].append((ts, win, ps, bs, pr, br, g))
seen_g = None

for tb in tables: tables[tb].sort(key=lambda x: x[0])
NH = sum(len(v) for v in tables.values())
print(f"[load] files={len(files)} raw={n_raw} dup={n_dup} bad={n_bad} hands={NH} tables={len(tables)}  ({time.time()-T0:.0f}s)")

def hand_total(rs): return sum(VAL[c] for c in rs) % 10

def tableau_ok(pr, br):
    """check third-card rule; returns (ok, reason)"""
    if len(pr) < 2 or len(br) < 2 or len(pr) > 3 or len(br) > 3:
        return False, 'count'
    pt2 = (VAL[pr[0]] + VAL[pr[1]]) % 10
    bt2 = (VAL[br[0]] + VAL[br[1]]) % 10
    if pt2 >= 8 or bt2 >= 8:
        return (len(pr) == 2 and len(br) == 2), 'nat'
    p_draw = pt2 <= 5
    if len(pr) != 2 + (1 if p_draw else 0): return False, 'p3'
    if not p_draw:
        b_draw = bt2 <= 5
    else:
        v3 = VAL[pr[2]]
        if   bt2 <= 2: b_draw = True
        elif bt2 == 3: b_draw = (v3 != 8)
        elif bt2 == 4: b_draw = (2 <= v3 <= 7)
        elif bt2 == 5: b_draw = (4 <= v3 <= 7)
        elif bt2 == 6: b_draw = (6 <= v3 <= 7)
        else: b_draw = False
    if len(br) != 2 + (1 if b_draw else 0): return False, 'b3'
    return True, ''

# ── A+B: marginals & physics ────────────────────────────────────────
wc = Counter(); rankc = Counter(); first_rank = {'P': Counter(), 'B': Counter()}
ps_dist = Counter(); bs_dist = Counter(); cards_dist = Counter()
n_pnat = n_bnat = n_anynat = n_ppair = n_bpair = 0
n_score_bad = n_tab_bad = 0; tab_bad_reasons = Counter(); score_bad_samples = []; tab_bad_by_tb = Counter()
n_comeback_elig = n_comeback = 0
for tb, hands in tables.items():
    for (ts, win, ps, bs, pr, br, g) in hands:
        wc[win] += 1
        for c in pr + br: rankc[c] += 1
        first_rank['P'][pr[0]] += 1; first_rank['B'][br[0]] += 1
        ps_dist[ps] += 1; bs_dist[bs] += 1; cards_dist[len(pr)+len(br)] += 1
        if hand_total(pr) != ps or hand_total(br) != bs:
            n_score_bad += 1
            if len(score_bad_samples) < 3: score_bad_samples.append((tb, g, pr, ps, br, bs))
        ok, why = tableau_ok(pr, br)
        if not ok:
            n_tab_bad += 1; tab_bad_reasons[why] += 1; tab_bad_by_tb[tb] += 1
        pt2 = (VAL[pr[0]] + VAL[pr[1]]) % 10; bt2 = (VAL[br[0]] + VAL[br[1]]) % 10
        pnat = pt2 >= 8; bnat = bt2 >= 8
        if pnat: n_pnat += 1
        if bnat: n_bnat += 1
        if pnat or bnat: n_anynat += 1
        if pr[0] == pr[1]: n_ppair += 1
        if br[0] == br[1]: n_bpair += 1
        # comeback: leader after initial 4 cards loses (only hands w/ 3rd card, no tie result)
        if (len(pr) + len(br)) > 4 and win != 'T' and pt2 != bt2:
            n_comeback_elig += 1
            leader = 'P' if pt2 > bt2 else 'B'
            if win != leader: n_comeback += 1

print("\n=== A. MARGINALS (obs vs 8-deck theory) ===")
for side, th in (('B', TH_B), ('P', TH_P), ('T', TH_T)):
    p = wc[side]/NH
    print(f"  win {side}: {p*100:.3f}%  (theory {th*100:.3f}%)  z={z1(p, NH, th):+.2f}")
p = n_anynat/NH
print(f"  natural(any): {p*100:.3f}%  n={n_anynat}")
print(f"  natural P: {n_pnat/NH*100:.3f}%  B: {n_bnat/NH*100:.3f}%")
for lbl, n in (('P pair', n_ppair), ('B pair', n_bpair)):
    p = n/NH
    print(f"  {lbl}: {p*100:.3f}% (theory {TH_PAIR*100:.3f}%)  z={z1(p, NH, TH_PAIR):+.2f}")
NC = sum(rankc.values())
print(f"  card ranks (n={NC}, expect 7.692% each):")
worst = sorted(RANKS, key=lambda r: -abs(rankc[r]/NC - 1/13.0))
for r in worst[:5]:
    p = rankc[r]/NC
    print(f"    {r}: {p*100:.3f}%  z={z1(p, NC, 1/13.0):+.2f}")
chi = sum((rankc[r] - NC/13.0)**2/(NC/13.0) for r in RANKS)
print(f"    chi2(12df)={chi:.1f} (95%crit=21.0)")
# first-card rank symmetry P vs B side
chi_fs = 0.0
for r in RANKS:
    e = (first_rank['P'][r] + first_rank['B'][r]) / 2.0
    if e > 0:
        chi_fs += (first_rank['P'][r]-e)**2/e + (first_rank['B'][r]-e)**2/e
print(f"  first-card rank P-side vs B-side: chi2(12df)={chi_fs:.1f} (95%crit=21.0)")

print("\n=== B. PHYSICS ===")
print(f"  score mismatch: {n_score_bad}/{NH}  samples={score_bad_samples}")
print(f"  tableau violations: {n_tab_bad}/{NH}  reasons={dict(tab_bad_reasons)}")
print(f"  violations by table (top5): {tab_bad_by_tb.most_common(5)}")
print(f"  cards/hand: {dict(sorted(cards_dist.items()))}")
cb = n_comeback/n_comeback_elig if n_comeback_elig else 0
print(f"  comeback rate (4-card leader loses, ex-tie): {cb*100:.3f}% n={n_comeback_elig}  [MC baseline below]")

# ── C: sequence structure per table ─────────────────────────────────
# ex-tie winner series + all-hand feature series per table
LAGS = list(range(1, 11))
lag_bb = {k: [0,0] for k in LAGS}   # after B at lag k: [B_next, total]
lag_pb = {k: [0,0] for k in LAGS}   # after P at lag k
mk2 = defaultdict(lambda: [0,0])    # last-2 pattern -> [B, total]
streak_surv = defaultdict(lambda: [0,0])  # streak len L (>=1) -> [continued, total]
nat_auto = {True: [0,0], False: [0,0]}    # prev any-nat -> [next nat, total]
feat_next = defaultdict(lambda: [0,0])    # feature label -> [next=B, total] (ex-tie next)
roll_nat = defaultdict(lambda: [0,0])     # composition bucket -> [next nat, total]
roll_win = defaultdict(lambda: [0,0])     # composition bucket -> [next B, total]
prev_repeat = defaultdict(lambda: [0,0])  # ('natwin'/'plainwin') -> [next same side, total]

for tb, hands in tables.items():
    ex = []            # ex-tie winner chars
    prev = None        # previous hand tuple (all hands)
    prev_nat = None
    cardq = deque(maxlen=60)
    streak_side, streak_len = None, 0
    for h in hands:
        (ts, win, ps, bs, pr, br, g) = h
        pt2 = (VAL[pr[0]]+VAL[pr[1]])%10; bt2 = (VAL[br[0]]+VAL[br[1]])%10
        anynat = pt2 >= 8 or bt2 >= 8
        # rolling composition -> this hand (predict BEFORE adding this hand's cards)
        if len(cardq) == 60:
            n9 = sum(1 for c in cardq if c == '9')
            n0 = sum(1 for c in cardq if VAL[c] == 0)
            b9 = 'lo' if n9 <= 3 else ('hi' if n9 >= 6 else 'mid')
            b0 = 'lo' if n0 <= 16 else ('hi' if n0 >= 21 else 'mid')
            roll_nat[('9dens', b9)][0] += 1 if anynat else 0; roll_nat[('9dens', b9)][1] += 1
            roll_nat[('0dens', b0)][0] += 1 if anynat else 0; roll_nat[('0dens', b0)][1] += 1
            if win != 'T':
                roll_win[('9dens', b9)][0] += 1 if win == 'B' else 0; roll_win[('9dens', b9)][1] += 1
                roll_win[('0dens', b0)][0] += 1 if win == 'B' else 0; roll_win[('0dens', b0)][1] += 1
        # prev-hand features -> this winner (ex-tie)
        if prev is not None and win != 'T':
            (_, pw, pps, pbs, ppr, pbr, _) = prev
            isB = 1 if win == 'B' else 0
            margin = abs(pps - pbs)
            fl = []
            fl.append(('prevT', pw == 'T'))
            fl.append(('prevNat', prev_nat))
            fl.append(('prevMargin>=6', margin >= 6))
            fl.append(('prevMargin<=1', margin <= 1))
            fl.append(('prev6cards', len(ppr)+len(pbr) == 6))
            fl.append(('prev4cards', len(ppr)+len(pbr) == 4))
            fl.append(('prevHas9', '9' in ppr+pbr))
            for name, cond in fl:
                key = (name, bool(cond))
                feat_next[key][0] += isB; feat_next[key][1] += 1
        # nat autocorrelation (all hands)
        if prev_nat is not None:
            nat_auto[prev_nat][0] += 1 if anynat else 0
            nat_auto[prev_nat][1] += 1
        # nat-win streak-break test: prev winner won WITH natural -> repeat prob
        if prev is not None and prev[1] in ('B','P') and win != 'T':
            pwin = prev[1]
            pwin_nat = (pt2b := (VAL[prev[4][0]]+VAL[prev[4][1]])%10) >= 8 if pwin=='P' else ((VAL[prev[5][0]]+VAL[prev[5][1]])%10) >= 8
            key = 'natwin' if pwin_nat else 'plainwin'
            prev_repeat[key][0] += 1 if win == pwin else 0
            prev_repeat[key][1] += 1
        # ex-tie series updates
        if win != 'T':
            for k in LAGS:
                if len(ex) >= k:
                    tgt = lag_bb[k] if ex[-k] == 'B' else lag_pb[k]
                    tgt[0] += 1 if win == 'B' else 0; tgt[1] += 1
            if len(ex) >= 2:
                key = ex[-2] + ex[-1]
                mk2[key][0] += 1 if win == 'B' else 0; mk2[key][1] += 1
            if streak_side is not None:
                L = min(streak_len, 8)
                streak_surv[L][0] += 1 if win == streak_side else 0
                streak_surv[L][1] += 1
            if win == streak_side: streak_len += 1
            else: streak_side, streak_len = win, 1
            ex.append(win)
            if len(ex) > 20: ex.pop(0)
        for c in pr + br: cardq.append(c)
        prev = h; prev_nat = anynat

print("\n=== C. SEQUENCE (per-table causal) ===")
print("  lag-k Markov  P(B|B at lag k) vs P(B|P at lag k):")
for k in LAGS:
    (b1, t1), (b2, t2) = lag_bb[k], lag_pb[k]
    p1, p2 = b1/t1, b2/t2
    print(f"    k={k:2d}: {p1*100:.2f}% vs {p2*100:.2f}%  diff={100*(p1-p2):+.2f}pt z={z2p(p1,t1,p2,t2):+.2f} (n={t1+t2})")
print("  order-2 Markov P(next=B | last2):")
base_b = wc['B']/(wc['B']+wc['P'])
for key in ('BB','BP','PB','PP'):
    b, t = mk2[key]
    print(f"    {key}: {b/t*100:.2f}% (n={t}) z_vs_base={z1(b/t, t, base_b):+.2f}")
print("  streak survival P(continue | len=L)  [null: p_B/p_P mix ~ const]:")
for L in sorted(streak_surv):
    c, t = streak_surv[L]
    print(f"    L={L}: {c/t*100:.2f}% (n={t})")
(nb1, nt1), (nb0, nt0) = nat_auto[True], nat_auto[False]
pn1, pn0 = nb1/nt1, nb0/nt0
print(f"  natural autocorr [positive control]: P(nat|prev nat)={pn1*100:.2f}% vs {pn0*100:.2f}%  z={z2p(pn1,nt1,pn0,nt0):+.2f}")
print("  prev-hand feature -> P(next=B) (ex-tie):")
names = sorted(set(n for (n, c) in feat_next))
for name in names:
    bT, tT = feat_next[(name, True)]; bF, tF = feat_next[(name, False)]
    if tT == 0 or tF == 0: continue
    pT, pF = bT/tT, bF/tF
    print(f"    {name}: True {pT*100:.2f}% (n={tT}) vs False {pF*100:.2f}%  z={z2p(pT,tT,pF,tF):+.2f}")
print("  prev winner repeat rate: after nat-win vs plain-win:")
(rb1, rt1), (rb0, rt0) = prev_repeat['natwin'], prev_repeat['plainwin']
print(f"    natwin repeat {rb1/rt1*100:.2f}% (n={rt1}) vs plain {rb0/rt0*100:.2f}% (n={rt0})  z={z2p(rb1/rt1,rt1,rb0/rt0,rt0):+.2f}")
print("  rolling last-60-cards composition -> next hand:")
for (feat, bkt) in sorted(roll_nat):
    c, t = roll_nat[(feat, bkt)]
    print(f"    nat | {feat}={bkt}: {c/t*100:.2f}% (n={t})")
for (feat, bkt) in sorted(roll_win):
    c, t = roll_win[(feat, bkt)]
    print(f"    P(B)| {feat}={bkt}: {c/t*100:.2f}% (n={t})")
# hi-vs-lo z for composition
for feat in ('9dens', '0dens'):
    for tgt, dic in (('nat', roll_nat), ('B', roll_win)):
        chi_, tlo = dic[(feat,'lo')]; chih, thi = dic[(feat,'hi')]
        if tlo and thi:
            print(f"    z {tgt} {feat} hi-vs-lo = {z2p(chih/thi, thi, chi_/tlo, tlo):+.2f}")

# ── D: time / entity ────────────────────────────────────────────────
print("\n=== D. TIME / ENTITY ===")
hourly = defaultdict(lambda: [0,0,0,0])  # h -> [B_extie, extie_n, nat, all_n]
daily  = defaultdict(lambda: [0,0,0,0])
blocks = defaultdict(lambda: [0,0])      # 30-min block -> [B, extie n]
blocksN = defaultdict(lambda: [0,0])     # 30-min block -> [nat, n]
tbl    = {}
gdig   = defaultdict(lambda: [0,0])
for tb, hands in tables.items():
    tB = tE = tT_ = tN = 0
    for (ts, win, ps, bs, pr, br, g) in hands:
        h = int((ts/3600 + 9) % 24)
        d = int((ts + 9*3600)//86400)
        blk = int(ts//1800)
        anynat = ((VAL[pr[0]]+VAL[pr[1]])%10 >= 8) or ((VAL[br[0]]+VAL[br[1]])%10 >= 8)
        hourly[h][2] += anynat; hourly[h][3] += 1
        daily[d][2] += anynat; daily[d][3] += 1
        blocksN[blk][0] += anynat; blocksN[blk][1] += 1
        if win != 'T':
            isB = 1 if win == 'B' else 0
            hourly[h][0] += isB; hourly[h][1] += 1
            daily[d][0] += isB; daily[d][1] += 1
            blocks[blk][0] += isB; blocks[blk][1] += 1
            tB += isB; tE += 1
        else:
            tT_ += 1
        tN += anynat
        gdig[int(str(g)[-1])][0] += 1 if win == 'B' else 0
        gdig[int(str(g)[-1])][1] += 0 if win == 'T' else 1
    tbl[tb] = (tB, tE, tT_, tN, len(hands))

pB_all = wc['B']/(wc['B']+wc['P']); pN_all = n_anynat/NH
chi_h = sum((hourly[h][0]-hourly[h][1]*pB_all)**2/(hourly[h][1]*pB_all*(1-pB_all)) for h in hourly if hourly[h][1])
chi_hn = sum((hourly[h][2]-hourly[h][3]*pN_all)**2/(hourly[h][3]*pN_all*(1-pN_all)) for h in hourly if hourly[h][3])
print(f"  hour-of-day(JST) B-rate chi2({len(hourly)-1}df)={chi_h:.1f} (95%crit~35.2) | nat chi2={chi_hn:.1f}")
hw = max(hourly, key=lambda h: abs(z1(hourly[h][0]/max(hourly[h][1],1), hourly[h][1], pB_all)))
print(f"    worst hour {hw:02d}h: B={hourly[hw][0]/hourly[hw][1]*100:.2f}% (n={hourly[hw][1]}) z={z1(hourly[hw][0]/hourly[hw][1], hourly[hw][1], pB_all):+.2f}")
print("  daily: date  B-rate(ex-tie)  nat-rate  n")
for d in sorted(daily):
    B, E, N_, A = daily[d]
    dt = time.strftime('%m/%d', time.gmtime(d*86400))
    print(f"    {dt}: B={B/E*100:.2f}% z={z1(B/E, E, pB_all):+.2f} | nat={N_/A*100:.2f}% z={z1(N_/A, A, pN_all):+.2f} | n={A}")
# per-table overdispersion
chi_t = sum((tbl[t][0]-tbl[t][1]*pB_all)**2/(tbl[t][1]*pB_all*(1-pB_all)) for t in tbl if tbl[t][1] > 100)
ntab = sum(1 for t in tbl if tbl[t][1] > 100)
print(f"  per-table B-rate overdispersion: chi2={chi_t:.1f} df={ntab-1} (ratio {chi_t/max(ntab-1,1):.2f}, fair~1.0)")
chi_tn = sum((tbl[t][3]-tbl[t][4]*pN_all)**2/(tbl[t][4]*pN_all*(1-pN_all)) for t in tbl if tbl[t][4] > 100)
print(f"  per-table nat-rate overdispersion: chi2={chi_tn:.1f} df={ntab-1} (ratio {chi_tn/max(ntab-1,1):.2f})")
natz = sorted(((t, tbl[t][3]/tbl[t][4], tbl[t][4], z1(tbl[t][3]/tbl[t][4], tbl[t][4], pN_all)) for t in tbl if tbl[t][4] > 500), key=lambda x: x[3])
print("  nat-rate outlier tables (bottom3/top3):")
for (t, pnat_, n_, zz) in natz[:3] + natz[-3:]:
    print(f"    table {t}: nat={pnat_*100:.2f}% (n={n_}) z={zz:+.2f}")
tt = {t: tbl[t][2]/tbl[t][4] for t in tbl if tbl[t][4] > 500}
chi_tt = sum((tbl[t][2]-tbl[t][4]*TH_T)**2/(tbl[t][4]*TH_T*(1-TH_T)) for t in tt)
print(f"  per-table tie-rate vs theory: chi2={chi_tt:.1f} df={len(tt)}")
worst_t = sorted(tt, key=lambda t: -abs(z1(tt[t], tbl[t][4], TH_T)))[:3]
for t in worst_t:
    print(f"    table {t}: tie={tt[t]*100:.2f}% (n={tbl[t][4]}) z={z1(tt[t], tbl[t][4], TH_T):+.2f}")
# 30-min block overdispersion (global sync)
bl = [(b, blocks[b][0], blocks[b][1]) for b in blocks if blocks[b][1] >= 200]
chi_b = sum((x - n*pB_all)**2/(n*pB_all*(1-pB_all)) for (_, x, n) in bl)
print(f"  30-min block pooled B-rate overdispersion: chi2={chi_b:.1f} df={len(bl)-1} (ratio {chi_b/max(len(bl)-1,1):.2f}, sync>1)")
bln = [(b, blocksN[b][0], blocksN[b][1]) for b in blocksN if blocksN[b][1] >= 200]
chi_bn = sum((x - n*pN_all)**2/(n*pN_all*(1-pN_all)) for (_, x, n) in bln)
print(f"  30-min block pooled nat-rate overdispersion: chi2={chi_bn:.1f} df={len(bln)-1} (ratio {chi_bn/max(len(bln)-1,1):.2f})")
# game id last digit
chi_g = 0.0
for dgt in sorted(gdig):
    b, n = gdig[dgt]
    if n: chi_g += (b - n*pB_all)**2/(n*pB_all*(1-pB_all))
print(f"  game-id last digit B-rate: chi2(9df)={chi_g:.1f} (95%crit=16.9)")

# ── E: Monte Carlo baseline ─────────────────────────────────────────
print(f"\n=== E. MONTE CARLO baseline (8-deck, {MC_N} hands, seed=7) ===")
random.seed(7)
mc = Counter(); mc_nat = 0; mc_cb = 0; mc_cbe = 0; mc_ps = Counter(); mc_cards = Counter()
hands_done = 0
shoe = []
while hands_done < MC_N:
    if len(shoe) < 20:
        shoe = [r for r in RANKS for _ in range(4*8)]
        random.shuffle(shoe)
        burn = VAL[shoe.pop()] or 10
        for _ in range(burn): shoe.pop()
    pr = [shoe.pop(), shoe.pop()]; br = [shoe.pop(), shoe.pop()]
    pt2 = (VAL[pr[0]]+VAL[pr[1]])%10; bt2 = (VAL[br[0]]+VAL[br[1]])%10
    if pt2 < 8 and bt2 < 8:
        if pt2 <= 5: pr.append(shoe.pop())
        if len(pr) == 2:
            if bt2 <= 5: br.append(shoe.pop())
        else:
            v3 = VAL[pr[2]]
            if (bt2 <= 2 or (bt2 == 3 and v3 != 8) or (bt2 == 4 and 2 <= v3 <= 7)
                    or (bt2 == 5 and 4 <= v3 <= 7) or (bt2 == 6 and 6 <= v3 <= 7)):
                br.append(shoe.pop())
    ps = sum(VAL[c] for c in pr) % 10; bs = sum(VAL[c] for c in br) % 10
    win = 'T' if ps == bs else ('P' if ps > bs else 'B')
    mc[win] += 1; hands_done += 1
    if pt2 >= 8 or bt2 >= 8: mc_nat += 1
    mc_ps[ps] += 1; mc_cards[len(pr)+len(br)] += 1
    if len(pr)+len(br) > 4 and win != 'T' and pt2 != bt2:
        mc_cbe += 1
        if win != ('P' if pt2 > bt2 else 'B'): mc_cb += 1
p_mc_nat = mc_nat/MC_N
print(f"  MC nat(any)={p_mc_nat*100:.3f}%  obs={n_anynat/NH*100:.3f}%  z={z2p(n_anynat/NH, NH, p_mc_nat, MC_N):+.2f}")
p_mc_cb = mc_cb/mc_cbe
print(f"  MC comeback={p_mc_cb*100:.3f}%  obs={cb*100:.3f}%  z={z2p(cb, n_comeback_elig, p_mc_cb, mc_cbe):+.2f}")
print(f"  MC cards/hand %: {[ (k, round(mc_cards[k]/MC_N*100,2)) for k in sorted(mc_cards)]}")
print(f"  obs cards/hand %: {[ (k, round(cards_dist[k]/NH*100,2)) for k in sorted(cards_dist)]}")
print("  player score dist obs% vs MC%:")
for s in range(10):
    po, pm = ps_dist[s]/NH, mc_ps[s]/MC_N
    print(f"    ps={s}: {po*100:.2f} vs {pm*100:.2f}  z={z2p(po, NH, pm, MC_N):+.2f}")
print(f"\n[done] {time.time()-T0:.0f}s")
