#!/usr/bin/env python3
"""_card_order_scan_v1.py — shuffle-order layer tests on the dealt-card stream.

Test (2) adjacency / rising-run signatures of imperfect or artificial shuffles.
Test (1) quantitative depletion: P(next card = rank r | count of r in last 52 cards)
         compared against fair-shuffle Monte Carlo at two penetrations.

Stream reconstruction: per table, hands sorted by ts; within a hand the physical
dealing order is P1,B1,P2,B2,P3,B3. Across-hand pairs only when gap <= 120s.
Shoe boundaries are invisible in historical data (dilutes, same order of dilution
as MC keeps its cross-shoe pairs).

Null = 8-deck, standard tableau, burn = value of first card (10 for 0-value),
memoryless shuffle. Exploratory scan — multiple comparisons apply.
"""
import json, glob, math, random, time, sys
from collections import defaultdict, Counter

T0 = time.time()
RANK_IDX = {'1':0,'2':1,'3':2,'4':3,'5':4,'6':5,'7':6,'8':7,'9':8,'0':9,'J':10,'Q':11,'K':12}
VAL = [1,2,3,4,5,6,7,8,9,0,0,0,0]
W = 52                 # depletion window (cards)
GAP_S = 120.0          # across-hand pair cutoff
CMAX = 6               # depletion count bucket cap (0..6+)

def z2(c1, n1, c0, n0):
    if not n1 or not n0: return 0.0
    p1, p0 = c1/n1, c0/n0
    p = (c1+c0)/(n1+n0)
    se = math.sqrt(p*(1-p)*(1/n1+1/n0)) or 1e-12
    return (p1-p0)/se

class OrderStats:
    def __init__(self, name):
        self.name = name
        self.pairs = 0
        self.same_rank = 0
        self.same_card = 0
        self.same_suit = 0
        self.plus1 = 0
        self.minus1 = 0
        self.asc_runs = Counter()   # run length (number of consecutive +1 steps)
        self.desc_runs = Counter()
        self.samerank_runs = Counter()
        # depletion: dep[c][0]=hits (next==r), dep[c][1]=trials, per window count c
        self.dep = [[0,0] for _ in range(CMAX+1)]
        self.dep_rank = {r: [[0,0],[0,0]] for r in range(13)}  # per rank: [c<=1, c>=4] -> [hit, n]
        self._win = []            # deque of rank idx (window W)
        self._cnt = [0]*13
        self._prev = None         # (ridx, suit)
        self._asc = 0
        self._desc = 0
        self._srun = 0

    def reset_stream(self):
        self._win.clear(); self._cnt = [0]*13
        self._prev = None
        self._flush_runs()

    def _flush_runs(self):
        if self._asc: self.asc_runs[self._asc] += 1
        if self._desc: self.desc_runs[self._desc] += 1
        if self._srun: self.samerank_runs[self._srun] += 1
        self._asc = self._desc = self._srun = 0

    def feed(self, ridx, suit):
        # depletion (window full only)
        if len(self._win) == W:
            cnt = self._cnt
            dep = self.dep
            for r in range(13):
                c = cnt[r]
                if c > CMAX: c = CMAX
                cell = dep[c]
                cell[1] += 1
                if r == ridx: cell[0] += 1
                dr = self.dep_rank[r]
                if cnt[r] <= 1:
                    dr[0][1] += 1
                    if r == ridx: dr[0][0] += 1
                elif cnt[r] >= 4:
                    dr[1][1] += 1
                    if r == ridx: dr[1][0] += 1
        # adjacency
        p = self._prev
        if p is not None:
            pr, psuit = p
            self.pairs += 1
            if ridx == pr:
                self.same_rank += 1
                if suit == psuit: self.same_card += 1
                self._srun += 1
            else:
                if self._srun: self.samerank_runs[self._srun] += 1
                self._srun = 0
            if suit == psuit: self.same_suit += 1
            if ridx == (pr + 1) % 13:
                self.plus1 += 1
                self._asc += 1
            else:
                if self._asc: self.asc_runs[self._asc] += 1
                self._asc = 0
            if ridx == (pr - 1) % 13:
                self.minus1 += 1
                self._desc += 1
            else:
                if self._desc: self.desc_runs[self._desc] += 1
                self._desc = 0
        self._prev = (ridx, suit)
        # window update
        self._win.append(ridx)
        self._cnt[ridx] += 1
        if len(self._win) > W:
            old = self._win.pop(0)
            self._cnt[old] -= 1

# ── observed stream ──────────────────────────────────────────────────
obs = OrderStats('obs')
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
        seen_g.add(g)
        tables[str(d.get('tb'))].append((float(d.get('ts') or 0), pc, bc))
seen_g = None
NH = sum(len(v) for v in tables.values())
print(f"[load] hands={NH} tables={len(tables)} ({time.time()-T0:.0f}s)")

for tb in sorted(tables):
    hands = sorted(tables[tb], key=lambda x: x[0])
    obs.reset_stream()
    last_ts = None
    for (ts, pc, bc) in hands:
        if last_ts is not None and ts - last_ts > GAP_S:
            obs.reset_stream()      # gap: don't pair across, drop stale window
        last_ts = ts
        seq = [pc[0], bc[0], pc[1], bc[1]]
        if len(pc) > 2: seq.append(pc[2])
        if len(bc) > 2: seq.append(bc[2])
        for card in seq:
            obs.feed(RANK_IDX[card[0]], card[1])
print(f"[obs stream done] pairs={obs.pairs} ({time.time()-T0:.0f}s)")

# ── MC baseline ──────────────────────────────────────────────────────
def run_mc(name, cut_remaining, n_cards_target, seed):
    random.seed(seed)
    mc = OrderStats(name)
    deck = [(r, s) for r in range(13) for s in 'SHDC'] * 8
    shoe = []
    dealt = 0
    while dealt < n_cards_target:
        if len(shoe) < max(cut_remaining, 6):
            shoe = deck[:]
            random.shuffle(shoe)
            # burn: first card + value more (faces/tens = 10)
            b = shoe.pop()
            burn_n = VAL[b[0]] or 10
            for _ in range(burn_n):
                shoe.pop()
            # NOTE: shoe change is invisible to the stream (mirrors obs): no reset
        pr = [shoe.pop(), shoe.pop()]; br = [shoe.pop(), shoe.pop()]
        pt2 = (VAL[pr[0][0]] + VAL[pr[1][0]]) % 10
        bt2 = (VAL[br[0][0]] + VAL[br[1][0]]) % 10
        p3 = b3 = None
        if pt2 < 8 and bt2 < 8:
            if pt2 <= 5: p3 = shoe.pop()
            if p3 is None:
                if bt2 <= 5: b3 = shoe.pop()
            else:
                v3 = VAL[p3[0]]
                if (bt2 <= 2 or (bt2 == 3 and v3 != 8) or (bt2 == 4 and 2 <= v3 <= 7)
                        or (bt2 == 5 and 4 <= v3 <= 7) or (bt2 == 6 and 6 <= v3 <= 7)):
                    b3 = shoe.pop()
        seq = [pr[0], br[0], pr[1], br[1]]
        if p3: seq.append(p3)
        if b3: seq.append(b3)
        for (r, s) in seq:
            mc.feed(r, s)
        dealt += len(seq)
    return mc

NC_TARGET = obs.pairs + 60
mc_deep = run_mc('mc_deep(cut16)', 16, NC_TARGET, 11)     # ~96% penetration
print(f"[mc_deep done] ({time.time()-T0:.0f}s)")
mc_shal = run_mc('mc_shallow(cut85)', 85, NC_TARGET, 12)  # ~79% penetration
print(f"[mc_shallow done] ({time.time()-T0:.0f}s)")

# ── report ───────────────────────────────────────────────────────────
def rate_line(lbl, getter):
    o_c = getter(obs); d_c = getter(mc_deep); s_c = getter(mc_shal)
    po = o_c/obs.pairs; pd = d_c/mc_deep.pairs; ps = s_c/mc_shal.pairs
    print(f"  {lbl}: obs {po*100:.4f}% | MC96 {pd*100:.4f}% (z={z2(o_c, obs.pairs, d_c, mc_deep.pairs):+.2f})"
          f" | MC79 {ps*100:.4f}% (z={z2(o_c, obs.pairs, s_c, mc_shal.pairs):+.2f})")

print("\n=== (2) ADJACENCY (consecutive dealt cards, per table) ===")
print(f"  pairs: obs={obs.pairs} mc={mc_deep.pairs}")
rate_line("same rank      ", lambda x: x.same_rank)
rate_line("same exact card", lambda x: x.same_card)
rate_line("same suit      ", lambda x: x.same_suit)
rate_line("rank +1 (asc)  ", lambda x: x.plus1)
rate_line("rank -1 (desc) ", lambda x: x.minus1)

def runs_line(kind, attr):
    print(f"  {kind} run counts (len>=2 steps = 3+ cards):")
    for L in (2, 3, 4):
        o = sum(c for l, c in getattr(obs, attr).items() if l >= L)
        d = sum(c for l, c in getattr(mc_deep, attr).items() if l >= L)
        s = sum(c for l, c in getattr(mc_shal, attr).items() if l >= L)
        print(f"    len>={L}: obs {o} | MC96 {d} (z={z2(o, obs.pairs, d, mc_deep.pairs):+.2f})"
              f" | MC79 {s} (z={z2(o, obs.pairs, s, mc_shal.pairs):+.2f})")
runs_line("ascending", 'asc_runs')
runs_line("descending", 'desc_runs')
runs_line("same-rank", 'samerank_runs')

print(f"\n=== (1) DEPLETION  P(next==r | count of r in last {W} cards) pooled over ranks ===")
print("  c    obs%      MC96%     MC79%    z(obs-MC96) z(obs-MC79)")
for c in range(CMAX+1):
    o = obs.dep[c]; d = mc_deep.dep[c]; s = mc_shal.dep[c]
    if not o[1]: continue
    print(f"  {c}{'+' if c==CMAX else ' '}  {o[0]/o[1]*100:.4f}  {d[0]/d[1]*100:.4f}  {s[0]/s[1]*100:.4f}"
          f"   {z2(o[0], o[1], d[0], d[1]):+.2f}      {z2(o[0], o[1], s[0], s[1]):+.2f}")
# summary slope: rate(c>=4) - rate(c<=1), obs vs each MC
def slope(st):
    lo = [0,0]; hi = [0,0]
    for c in range(CMAX+1):
        tgt = lo if c <= 1 else (hi if c >= 4 else None)
        if tgt is not None:
            tgt[0] += st.dep[c][0]; tgt[1] += st.dep[c][1]
    return lo, hi
olo, ohi = slope(obs); dlo, dhi = slope(mc_deep); slo, shi = slope(mc_shal)
def pp(x): return x[0]/x[1]*100 if x[1] else 0.0
print(f"  slope(c>=4 minus c<=1): obs {pp(ohi)-pp(olo):+.4f}pt | MC96 {pp(dhi)-pp(dlo):+.4f}pt | MC79 {pp(shi)-pp(slo):+.4f}pt")
print("\n  per-rank slope anomaly (obs diff minus MC96 diff, z):")
for r in range(13):
    o = obs.dep_rank[r]; d = mc_deep.dep_rank[r]
    do = (o[1][0]/o[1][1] - o[0][0]/o[0][1]) if o[0][1] and o[1][1] else 0.0
    dd = (d[1][0]/d[1][1] - d[0][0]/d[0][1]) if d[0][1] and d[1][1] else 0.0
    # z via combined variance of the four proportions (approx)
    def var(cell):
        if not cell[1]: return 0.0
        p = cell[0]/cell[1]
        return p*(1-p)/cell[1]
    se = math.sqrt(var(o[0])+var(o[1])+var(d[0])+var(d[1])) or 1e-12
    rank_name = '1234567890JQK'[r]
    print(f"    rank {rank_name}: obs {do*100:+.3f}pt vs MC96 {dd*100:+.3f}pt  z={(do-dd)/se:+.2f}")
print(f"\n[done] {time.time()-T0:.0f}s")
