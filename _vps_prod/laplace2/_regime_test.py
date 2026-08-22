#!/usr/bin/env python3
# Regime-clustering test for: "there exist (randomly-timed) stretches where the
# line follows through (high win%) and stretches where it doesn't (low win%)".
# = temporal overdispersion / autocorrelation at SOME timescale. Tests many scales.
import re, math, subprocess
from datetime import datetime
LOG='/opt/laplace2/dual_line_pragmatic_bot.log'
out=subprocess.run(['grep','-hE',' resolve .*(WIN|LOSE)',LOG],capture_output=True,text=True).stdout
rows=[]
for line in out.splitlines():
    m=re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*resolve [^:]+: pred=[PB] outcome=[PBT] (WIN|LOSE)',line)
    if m: rows.append((datetime.strptime(m.group(1),'%Y-%m-%d %H:%M:%S'),1 if m.group(2)=='WIN' else 0))
rows.sort()
seq=[w for _,w in rows]; n=len(seq); p=sum(seq)/n
print(f'N={n}  overall win%={p*100:.2f}  span={rows[0][0]}..{rows[-1][0]}')
print('H0 = IID random (no regimes). phi>1 & Z>=2 = regimes (overdispersion). Z=(chi2-df)/sqrt(2df).\n')

def disp_blocks(K):
    # consecutive-signal blocks of size K
    blocks=[seq[i:i+K] for i in range(0,n,K)]
    blocks=[b for b in blocks if len(b)>=max(5,K//2)]
    if len(blocks)<3 or p*(1-p)==0: return None
    chi=sum((sum(b)-len(b)*p)**2/(len(b)*p*(1-p)) for b in blocks); df=len(blocks)-1
    return (chi/df,(chi-df)/math.sqrt(2*df),len(blocks))

def disp_clock(Wmin):
    from collections import defaultdict
    b=defaultdict(list)
    for ts,w in rows: b[int(ts.timestamp()//(Wmin*60))].append(w)
    bb=[v for v in b.values() if len(v)>=5]
    if len(bb)<3 or p*(1-p)==0: return None
    chi=sum((sum(v)-len(v)*p)**2/(len(v)*p*(1-p)) for v in bb); df=len(bb)-1
    return (chi/df,(chi-df)/math.sqrt(2*df),len(bb))

print('A) OVERDISPERSION by CONSECUTIVE-SIGNAL blocks (regime measured in #signals):')
print('   K     phi    Z      nblocks')
for K in (10,15,20,30,50,75,100):
    r=disp_blocks(K)
    if r: print(f'  {K:4d}  {r[0]:5.3f}  {r[1]:+5.2f}   {r[2]}')

print('\nB) OVERDISPERSION by CLOCK windows (regime measured in minutes):')
print('   min   phi    Z      nwin')
for W in (5,10,15,30,60,120,180):
    r=disp_clock(W)
    if r: print(f'  {W:4d}  {r[0]:5.3f}  {r[1]:+5.2f}   {r[2]}')

# C) autocorrelation at many lags + Ljung-Box Q
print('\nC) AUTOCORRELATION (a win predicting later wins = regimes):')
den=sum((x-p)**2 for x in seq)
acs=[]
for lag in range(1,21):
    num=sum((seq[i]-p)*(seq[i-lag]-p) for i in range(lag,n))
    ac=num/den if den else 0; acs.append(ac)
print('   lag: ' + ' '.join(f'{a:+.3f}' for a in acs[:10]))
print('   lag11-20: ' + ' '.join(f'{a:+.3f}' for a in acs[10:]))
Q=n*(n+2)*sum(acs[k]**2/(n-(k+1)) for k in range(20))
# chi-square(20) 5% crit = 31.41 ; 1% = 37.57
print(f'   Ljung-Box Q(20)={Q:.1f}  (crit 5%=31.41, 1%=37.57; Q above => autocorrelation present)')

# D) runs test
nW=sum(seq); nL=n-nW
runs=1+sum(1 for i in range(1,n) if seq[i]!=seq[i-1])
ER=2*nW*nL/n+1; VR=2*nW*nL*(2*nW*nL-n)/(n*n*(n-1))
rz=(runs-ER)/math.sqrt(VR) if VR>0 else 0
print(f'\nD) RUNS test: runs={runs} expected={ER:.0f} Z={rz:+.2f}  (Z<<0 = clustering/regimes; ~0 = random)')
