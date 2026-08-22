#!/usr/bin/env python3
# Pre-registered clustering test for the "synchronized good/bad-run times" hypothesis.
# PRIMARY: 10-min-window overdispersion phi (Zchi>=2 sustained = clustering supported).
# OOS cutoff (pre-registered 2026-06-08): only resolves AFTER this are used.
import subprocess, re, math, csv, os
from datetime import datetime
from collections import defaultdict
LOG='/opt/laplace2/dual_line_pragmatic_bot.log'
DATA='/opt/laplace2/clustering_oos.csv'
RESULTS='/opt/laplace2/clustering_results.csv'
CUTOFF='2026-06-08 00:36:03'   # PRE-REGISTERED: OOS = resolves strictly after this

data={}
if os.path.exists(DATA):
    for r in csv.reader(open(DATA)):
        if len(r)>=4:
            data[(r[0],r[1],r[2])]=(r[0],int(r[3]))
try:
    out=subprocess.run(['grep','-hE',' resolve .*(WIN|LOSE)',LOG],capture_output=True,text=True,timeout=240).stdout
except Exception:
    out=''
for line in out.splitlines():
    m=re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*resolve ([^:]+): pred=[PB] outcome=[PBT] (WIN|LOSE).*pattern=(.+)$',line)
    if not m: continue
    ts=m.group(1)
    if ts<=CUTOFF: continue
    data[(ts,m.group(2),m.group(4))]=(ts,1 if m.group(3)=='WIN' else 0)
with open(DATA,'w',newline='') as f:
    w=csv.writer(f)
    for (ts,tbl,pat),(ts2,win) in sorted(data.items()):
        w.writerow([ts,tbl,pat,win])
bets=sorted((datetime.strptime(v[0],'%Y-%m-%d %H:%M:%S'),v[1]) for v in data.values())
n=len(bets)
def phi(wmin,p):
    b=defaultdict(list)
    for dt,wn in bets: b[int(dt.timestamp()//(wmin*60))].append(wn)
    bb=[v for v in b.values() if len(v)>=5]
    if len(bb)<2 or p*(1-p)==0: return (0.0,0.0,len(bb))
    chi=sum((sum(v)-len(v)*p)**2/(len(v)*p*(1-p)) for v in bb); df=len(bb)-1
    return (chi/df,(chi-df)/math.sqrt(2*df),len(bb))
res={'n':n}
if n>=30:
    seq=[w for _,w in bets]; nW=sum(seq); nL=n-nW; p=nW/n
    runs=1+sum(1 for i in range(1,n) if seq[i]!=seq[i-1])
    ER=2*nW*nL/n+1; VR=2*nW*nL*(2*nW*nL-n)/(n*n*(n-1)) if n>1 else 0
    rz=(runs-ER)/math.sqrt(VR) if VR>0 else 0
    den=sum((x-p)**2 for x in seq); ac1=(sum((seq[i]-p)*(seq[i-1]-p) for i in range(1,n))/den) if den else 0
    p10,z10,b10=phi(10,p); p15,z15,b15=phi(15,p)
    res.update(p=p,runsZ=rz,ac1=ac1,phi10=p10,z10=z10,phi15=p15,z15=z15,buck10=b10)
run_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
hdr=not os.path.exists(RESULTS)
with open(RESULTS,'a',newline='') as f:
    w=csv.writer(f)
    if hdr: w.writerow(['run_at','n_oos','winrate','phi10','z10','phi15','z15','runsZ','ac1','buckets10'])
    if 'p' in res:
        w.writerow([run_at,n,round(res['p'],4),round(res['phi10'],3),round(res['z10'],2),round(res['phi15'],3),round(res['z15'],2),round(res['runsZ'],2),round(res['ac1'],4),res['buck10']])
    else:
        w.writerow([run_at,n,'','','','','','','','(n<30)'])
print('OOS n=%d'%n, ({k:(round(v,3) if isinstance(v,float) else v) for k,v in res.items()} if 'p' in res else '(n<30: 蓄積待ち)'))
