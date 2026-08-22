#!/usr/bin/env python3
import re, sys
from collections import defaultdict

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
# resolve <TABLE>: pred=X outcome=Y RESULT ... pattern=...
rx = re.compile(r'resolve (.+?): pred=(\w+) outcome=(\w+) (\w+).*?pattern=([^\s]+)')

def is_fast(name):
    n = name.lower()
    return any(k in n for k in ('speed', 'turbo', 'スピード', 'ターボ'))

groups = {'FAST (Speed/Turbo)': defaultdict(int), 'NORMAL': defaultdict(int)}
per_table = defaultdict(lambda: defaultdict(int))
per_pat_group = defaultdict(lambda: defaultdict(int))
total = 0
first_ts = last_ts = None

with open(LOG, encoding='utf-8', errors='replace') as f:
    for line in f:
        m = rx.search(line)
        if not m:
            continue
        table, pred, outcome, result, pattern = m.groups()
        result = result.upper()
        if result not in ('WIN', 'LOSE', 'LOSS', 'TIE', 'PUSH'):
            continue
        total += 1
        ts = line[:19]
        if first_ts is None: first_ts = ts
        last_ts = ts
        g = 'FAST (Speed/Turbo)' if is_fast(table) else 'NORMAL'
        key = 'win' if result == 'WIN' else ('tie' if result in ('TIE','PUSH') else 'lose')
        groups[g][key] += 1
        per_table[table][key] += 1
        per_pat_group[(g, pattern)][key] += 1

def wr(d):
    w, l, t = d['win'], d['lose'], d['tie']
    dec = w + l
    return (100.0*w/dec if dec else 0.0), w, l, t, w+l+t

print(f"=== v3 resolve lines: {total} total | span {first_ts} .. {last_ts} ===\n")
print(f"{'GROUP':<22} {'win%':>6} {'W':>5} {'L':>5} {'T':>5} {'N':>6}")
for g in ('NORMAL', 'FAST (Speed/Turbo)'):
    rate, w, l, t, n = wr(groups[g])
    print(f"{g:<22} {rate:6.2f} {w:5d} {l:5d} {t:5d} {n:6d}")
# combined
alld = defaultdict(int)
for g in groups.values():
    for k,v in g.items(): alld[k]+=v
rate,w,l,t,n = wr(alld)
print(f"{'ALL':<22} {rate:6.2f} {w:5d} {l:5d} {t:5d} {n:6d}")

print("\n=== top 18 tables by volume ===")
print(f"{'table':<28} {'fast':>4} {'win%':>6} {'W':>4} {'L':>4} {'T':>4} {'N':>5}")
for table, d in sorted(per_table.items(), key=lambda kv: -(kv[1]['win']+kv[1]['lose']+kv[1]['tie']))[:18]:
    rate,w,l,t,n = wr(d)
    print(f"{table[:28]:<28} {'Y' if is_fast(table) else '.':>4} {rate:6.2f} {w:4d} {l:4d} {t:4d} {n:5d}")

print("\n=== per pattern x group (win% / N decided) ===")
pats = sorted({p for (g,p) in per_pat_group})
print(f"{'pattern':<22} {'NORMAL win%/N':>16} {'FAST win%/N':>16}")
for p in pats:
    out=[]
    for g in ('NORMAL','FAST (Speed/Turbo)'):
        d=per_pat_group.get((g,p),{})
        rate,w,l,t,n=wr(d) if d else (0,0,0,0,0)
        out.append(f"{rate:5.1f}/{w+l:<4d}")
    print(f"{p:<22} {out[0]:>16} {out[1]:>16}")
