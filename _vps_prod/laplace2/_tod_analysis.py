#!/usr/bin/env python3
# Time-of-day regime test for the user's hypothesis:
#   "morning..~23:00 = ~55% win & stable; after 23:00 = more 5-6 loss streaks & ~48%".
import re, math, subprocess
from collections import defaultdict, Counter
from datetime import datetime

LOG = '/opt/laplace2/dual_line_pragmatic_bot.log'
out = subprocess.run(['grep','-hE',' resolve .*(WIN|LOSE)',LOG],
                     capture_output=True, text=True).stdout
rows = []
for line in out.splitlines():
    m = re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*resolve [^:]+: pred=[PB] outcome=[PBT] (WIN|LOSE)', line)
    if not m:
        continue
    ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    rows.append((ts, 1 if m.group(2) == 'WIN' else 0))
rows.sort()
n = len(rows)
if n == 0:
    print('no data'); raise SystemExit
wins = sum(w for _, w in rows)
print(f'TOTAL n={n}  win_rate={wins/n:.4f}  span={rows[0][0]}..{rows[-1][0]} (JST)')

# ── win rate by hour-of-day ──
byhr_n = Counter(); byhr_w = Counter()
for ts, w in rows:
    byhr_n[ts.hour] += 1; byhr_w[ts.hour] += w
print('\nHOUR  n    win%   bar')
for h in range(24):
    nh = byhr_n[h]
    if nh == 0:
        print(f'{h:02d}    0     -'); continue
    wr = byhr_w[h]/nh
    bar = '#' * int(round(wr*40))
    print(f'{h:02d}   {nh:4d}  {wr*100:5.1f}  {bar}')

def ztest(w1,n1,w2,n2):
    if n1==0 or n2==0: return 0.0
    p1,p2 = w1/n1, w2/n2; p=(w1+w2)/(n1+n2)
    se = math.sqrt(p*(1-p)*(1/n1+1/n2))
    return (p1-p2)/se if se>0 else 0.0

# ── user's cut: "day" = 06:00-22:59 vs "night" = 23:00-05:59 ──
day_n=day_w=night_n=night_w=0
for ts,w in rows:
    if 6 <= ts.hour < 23:
        day_n+=1; day_w+=w
    else:
        night_n+=1; night_w+=w
print(f'\nDAY  (06:00-22:59): n={day_n:4d}  win={day_w/day_n*100:.2f}%' if day_n else 'DAY n=0')
print(f'NIGHT(23:00-05:59): n={night_n:4d}  win={night_w/night_n*100:.2f}%' if night_n else 'NIGHT n=0')
z = ztest(day_w,day_n,night_w,night_n)
print(f'day-vs-night two-proportion Z = {z:+.2f}  (|Z|>=1.96 = significant at 5%)')

# ── narrower night (the exact 23:00+ the user named, until ~05:00) by single hours already shown ──

# ── loss streaks on the time-ordered combined stream ──
streaks=[]; cur=0
for _,w in rows:
    if w==0: cur+=1
    else:
        if cur>0: streaks.append(cur)
        cur=0
if cur>0: streaks.append(cur)
sc=Counter(streaks)
print('\nLOSS-STREAK length distribution (time-ordered, all tables combined):')
for k in sorted(sc): print(f'  {k} losses in a row: {sc[k]} times')
print(f'  >=5 loss streaks: {sum(v for k,v in sc.items() if k>=5)}')

# ── when do >=5 loss streaks START (hour-of-day of the first loss of each >=5 run) ──
start_hours=[]; cur=0; start_ts=None
for ts,w in rows:
    if w==0:
        if cur==0: start_ts=ts
        cur+=1
    else:
        if cur>=5 and start_ts: start_hours.append(start_ts.hour)
        cur=0
if cur>=5 and start_ts: start_hours.append(start_ts.hour)
hc=Counter(start_hours)
print('\n>=5 loss-streak START hour-of-day:')
for h in sorted(hc): print(f'  {h:02d}:00  x{hc[h]}')
night_starts = sum(v for h,v in hc.items() if (h>=23 or h<6))
print(f'  starts in NIGHT(23:00-05:59): {night_starts} / {len(start_hours)} total')
