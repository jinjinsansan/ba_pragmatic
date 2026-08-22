#!/usr/bin/env python3
"""_bt_now_density_v3_arrival.py — 到着(publish)時刻ベースでNOW密度フィルタを凍結用に確定。

変更(v2→v3): 密度と現在NOWの基準時刻を「決済時刻」から「到着(publish)時刻」へ。
  - v4: [v4-publish] の時刻=到着。[v4-settle] result で勝敗。publish時刻でd30を数える。
  - 密度母集団(フリート全体NOW到着): v4 publish時刻 + v3 resolve時刻(v3は到着行が無い
    ため resolve=決済時刻で代用・約1手ラグ・30分窓に対し軽微)。
  - d30 = 現NOWの到着時刻 t の直前30分[t-1800,t) に出たフリートNOW本数(自分除く)。

出力: (1) 全履歴でのtercile cut(=凍結値) (2) sparse/dense的中率 (3) 70/30 OOS再確認。
FREEZE = 実行時刻(この時刻より前を「凍結前データ」としてcut確定)。
"""
import re, time, math, bisect

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
RE_TS  = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
RE_PUB = re.compile(r"\[v4-publish\] accepted did=(\S+) table=\S+ pattern=(\S+)\|([PB])")
RE_SET = re.compile(r"\[v4-settle\] posted settled did=(\S+) result=(WIN|LOSE|TIE)")
RE_V3  = re.compile(r"resolve .+?: pred=[PB] outcome=[PBT] (WIN|LOSE|TIE)")

def load():
    pub = {}   # did -> (pub_ts, side, pat)
    v4 = []    # settled with arrival ts
    v3_ts = []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        mts = RE_TS.match(line)
        if not mts: continue
        ts = time.mktime(time.strptime(mts.group(1), "%Y-%m-%d %H:%M:%S"))
        m = RE_PUB.search(line)
        if m:
            pub[m.group(1)] = (ts, m.group(3), m.group(2)); continue
        m = RE_SET.search(line)
        if m:
            if m.group(2) == "TIE": continue
            p = pub.get(m.group(1))
            if p:
                v4.append({"pub_ts": p[0], "win": m.group(2)=="WIN", "side": p[1], "pat": p[2]})
            continue
        m = RE_V3.search(line)
        if m and m.group(1) != "TIE":
            v3_ts.append(ts)
    v4.sort(key=lambda x: x["pub_ts"])
    # fleet arrival population = v4 publish + v3 resolve
    pop = sorted([p[0] for p in pub.values()] + v3_ts)
    return v4, pop

def z2(p1,n1,p2,n2):
    if not n1 or not n2: return 0.0
    p=(p1*n1+p2*n2)/(n1+n2); se=math.sqrt(p*(1-p)*(1/n1+1/n2)) or 1e-12
    return (p1-p2)/se

def wr(rows):
    n=len(rows); w=sum(1 for r in rows if r["win"]); return w,n,(w/n*100 if n else 0)

def main():
    v4, pop = load()
    for r in v4:
        t=r["pub_ts"]
        r["d30"]=bisect.bisect_left(pop,t)-bisect.bisect_left(pop,t-1800)
    FREEZE = time.time()
    hist=[r for r in v4 if r["pub_ts"]<FREEZE]   # 全部(実行時点で全て過去)
    ds=sorted(r["d30"] for r in hist)
    lo=ds[len(ds)//3]; hi=ds[2*len(ds)//3]
    print(f"[load] v4 settled={len(v4)} fleet_pop={len(pop)}  span="
          f"{time.strftime('%m/%d',time.localtime(v4[0]['pub_ts']))}〜{time.strftime('%m/%d',time.localtime(v4[-1]['pub_ts']))}")
    print(f"\n★凍結cut(到着30分密度・全履歴tercile): sparse = d30 < {lo} / dense = d30 >= {hi}")
    sp=[r for r in hist if r["d30"]<lo]; dn=[r for r in hist if r["d30"]>=hi]
    ws,ns,rs=wr(sp); wd,nd,rd=wr(dn)
    print(f"  全履歴: sparse={rs:.2f}%(n={ns}) dense={rd:.2f}%(n={nd}) diff={rs-rd:+.2f}pt "
          f"z={z2(ws/ns,ns,wd/nd,nd):+.2f}  (分岐~50.8%)")
    # 70/30 OOS re-confirm with arrival-time measure + train cut
    k=int(len(v4)*0.7); tr,te=v4[:k],v4[k:]
    lo2=sorted(r["d30"] for r in tr)[len(tr)//3]; hi2=sorted(r["d30"] for r in tr)[2*len(tr)//3]
    for part,seg in (("TRAIN",tr),("OOS",te)):
        sp=[r for r in seg if r["d30"]<lo2]; dn=[r for r in seg if r["d30"]>=hi2]
        ws,ns,rs=wr(sp); wd,nd,rd=wr(dn)
        print(f"  [{part} cut={lo2}/{hi2}] sparse={rs:.2f}%(n={ns}) dense={rd:.2f}%(n={nd}) "
              f"diff={rs-rd:+.2f}pt z={z2(ws/ns,ns,wd/nd,nd):+.2f}")
    print(f"\n凍結タイムスタンプ(この時刻以降を前向き検証データに): "
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(FREEZE))}  (epoch {int(FREEZE)})")

if __name__=="__main__":
    main()
