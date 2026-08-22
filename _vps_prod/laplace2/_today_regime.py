#!/usr/bin/env python3
# (1) TODAY by hour incl the user's "18:00+ = bad stretch" claim.
# (2) The core test of "predictable regimes": does being in a losing run predict
#     more losses? (conditional win rate after loss / after N-loss streak).
import re, subprocess, math
from datetime import datetime
from collections import Counter
LOG='/opt/laplace2/dual_line_pragmatic_bot.log'
out=subprocess.run(['grep','-hE',' resolve .*(WIN|LOSE)',LOG],capture_output=True,text=True).stdout
rows=[]
for line in out.splitlines():
    m=re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*resolve [^:]+: pred=[PB] outcome=[PBT] (WIN|LOSE)',line)
    if m: rows.append((datetime.strptime(m.group(1),'%Y-%m-%d %H:%M:%S'),1 if m.group(2)=='WIN' else 0))
rows.sort()
today=rows[-1][0].date()
tr=[(ts,w) for ts,w in rows if ts.date()==today]
print(f'=== TODAY {today} (JST)  n={len(tr)}  win={sum(w for _,w in tr)/max(1,len(tr))*100:.1f}% ===')
byhr_n=Counter(); byhr_w=Counter()
for ts,w in tr: byhr_n[ts.hour]+=1; byhr_w[ts.hour]+=w
for h in range(9,24):
    nh=byhr_n[h]
    if nh: print(f'  {h:02d}:00  n={nh:3d}  win={byhr_w[h]/nh*100:5.1f}%  ' + '#'*int(round(byhr_w[h]/nh*40)))
pre=[w for ts,w in tr if ts.hour<18]; post=[w for ts,w in tr if ts.hour>=18]
if pre and post:
    p1=sum(pre)/len(pre); p2=sum(post)/len(post); p=(sum(pre)+sum(post))/(len(pre)+len(post))
    se=math.sqrt(p*(1-p)*(1/len(pre)+1/len(post))); z=(p1-p2)/se if se else 0
    print(f"  PRE-18:00 n={len(pre)} {p1*100:.1f}%   18:00+ n={len(post)} {p2*100:.1f}%   Z={z:+.2f}")

# (2) conditional win rate over ALL 4122 (the real regime test)
seq=[w for _,w in rows]; n=len(seq); base=sum(seq)/n
print(f'\n=== CONDITIONAL win rate (all {n}, base={base*100:.2f}%) — "does a bad run predict more losses?" ===')
# after a win vs after a loss
aw=[seq[i] for i in range(1,n) if seq[i-1]==1]; al=[seq[i] for i in range(1,n) if seq[i-1]==0]
print(f'  next win% after a WIN : {sum(aw)/len(aw)*100:.2f}%  (n={len(aw)})')
print(f'  next win% after a LOSE: {sum(al)/len(al)*100:.2f}%  (n={len(al)})')
# after k consecutive losses, what is next win%
for k in (2,3,4,5):
    nxt=[]
    run=0
    for i in range(n):
        if run>=k: nxt.append(seq[i])
        run = run+1 if seq[i]==0 else 0
    if nxt: print(f'  next win% after >={k} losses in a row: {sum(nxt)/len(nxt)*100:.2f}%  (n={len(nxt)})')
print('  → if "bad times" were real/predictable, win% after losses would be WELL BELOW base. Equal = random.')
