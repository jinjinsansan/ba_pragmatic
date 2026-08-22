#!/usr/bin/env python3
"""_card_a3_check.py — replicate v1's A3 (window-10 nat count -> next-hand nat)
with and without variant tables, to test the mixture-artifact hypothesis for z=+8.

A3 logic (as in _card_analysis_v1.py): per table, k = sum(nat in last 10 hands),
bucket 0-2 / 3-4 / 5+, tally whether NEXT hand is nat. Pool buckets across tables.
z = two-prop(5+ vs 0-2).
"""
import json, glob, math, collections

def z2(c1, n1, c0, n0):
    if not n1 or not n0: return 0.0
    p1, p0 = c1/n1, c0/n0
    p = (c1+c0)/(n1+n0)
    se = math.sqrt(p*(1-p)*(1/n1+1/n0)) or 1e-12
    return (p1-p0)/se

import sys
DEDUP = '--dedup' in sys.argv
tables = collections.defaultdict(list)
variant = collections.defaultdict(int)   # tb -> count of 3-char-card hands
names = {}
seen_g = set(); n_dup_adj = 0; last_g_by_tb = {}
for f in sorted(glob.glob('/opt/laplace2/card_feed.jsonl*')):
    for line in open(f, encoding='utf-8', errors='replace'):
        try: d = json.loads(line)
        except Exception: continue
        if d.get('k') != 'r': continue
        win = d.get('win')
        if win not in ('P','B','T'): continue
        pc, bc = d.get('pc') or [], d.get('bc') or []
        if not pc or not bc: continue
        tb = str(d.get('tb'))
        if d.get('tn'): names[tb] = d.get('tn')
        g = d.get('g')
        if last_g_by_tb.get(tb) == g: n_dup_adj += 1
        last_g_by_tb[tb] = g
        if DEDUP:
            if g in seen_g: continue
            seen_g.add(g)
        if any(len(str(c)) != 2 for c in pc+bc): variant[tb] += 1
        ps, bs = d.get('ps'), d.get('bs')
        nat = (len(pc) == 2 and ps in (8,9)) or (len(bc) == 2 and bs in (8,9))
        tables[tb].append((d.get('ts') or 0.0, nat))
for tb in tables: tables[tb].sort()
print(f"dedup={'ON' if DEDUP else 'OFF'}  adjacent-duplicate records seen at load: {n_dup_adj}")

var_tabs = {tb for tb in tables if variant[tb] > len(tables[tb])*0.5}
print(f"variant tables (3-char cards >50%): {[(t, names.get(t,'?'), len(tables[t])) for t in var_tabs]}")
# nat rate per table extremes (all tables incl variants)
rates = sorted(((sum(n for _, n in v)/len(v), len(v), t) for t, v in tables.items() if len(v) > 300))
print("nat-rate extremes: low3=", [(t, f"{r*100:.1f}%", n) for r, n, t in rates[:3]],
      " high3=", [(t, f"{r*100:.1f}%", n) for r, n, t in rates[-3:]])

def a3(tabs, label):
    bk = collections.defaultdict(lambda: [0,0])
    for tb in tabs:
        nats = [n for _, n in tables[tb]]
        for i in range(10, len(nats)):
            k = sum(nats[i-10:i])
            b = '0-2' if k <= 2 else ('3-4' if k <= 4 else '5+')
            bk[b][0] += 1 if nats[i] else 0
            bk[b][1] += 1
    parts = [f"{b}:{bk[b][0]/max(bk[b][1],1)*100:.2f}%(n={bk[b][1]})" for b in ('0-2','3-4','5+')]
    z = z2(bk['5+'][0], bk['5+'][1], bk['0-2'][0], bk['0-2'][1])
    print(f"[A3 {label}] {' '.join(parts)}  z(5+ vs 0-2)={z:+.2f}")

a3(tables.keys(), 'ALL tables (v1-equivalent)')
a3([t for t in tables if t not in var_tabs], 'standard only')
a3(var_tabs, 'variants only')
# per-table z for the biggest standard tables (is any single table positive?)
big = sorted((t for t in tables if t not in var_tabs), key=lambda t: -len(tables[t]))[:8]
for t in big:
    bk = collections.defaultdict(lambda: [0,0])
    nats = [n for _, n in tables[t]]
    for i in range(10, len(nats)):
        k = sum(nats[i-10:i])
        b = '0-2' if k <= 2 else ('3-4' if k <= 4 else '5+')
        bk[b][0] += 1 if nats[i] else 0
        bk[b][1] += 1
    z = z2(bk['5+'][0], bk['5+'][1], bk['0-2'][0], bk['0-2'][1])
    print(f"  table {t} ({names.get(t,'?')}, n={len(nats)}): z={z:+.2f}")
