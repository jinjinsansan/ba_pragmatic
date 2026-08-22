#!/usr/bin/env python3
"""_bt_now_density_v2.py — NOW密度の「過疎ほど当たる(仮説と逆)」をOOS+パターン交絡で精査。

v1所見: フリート全体NOW密度で dense<sparse が v3/v4・全指標で符号一致・時刻層別後も残存。
本スクリプト: 主指標=フリート全体d30に絞り、
  (1) 時系列OOS(train70/test30・cut=trainで確定)で sparse>dense がホールドするか
  (2) パターン別(pattern_key)にプールした dense-sparse(パターン構成交絡の除去)
  (3) sparse群が手数料分岐(~50.8%)を超えるか(=BET/skipフィルタとして使えるか)
"""
import re, time, math, collections, bisect

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
RE_TS  = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
RE_PUB = re.compile(r"\[v4-publish\] accepted did=(\S+) table=\S+ pattern=(\S+)\|([PB])")
RE_SET = re.compile(r"\[v4-settle\] posted settled did=(\S+) result=(WIN|LOSE|TIE)")
RE_V3  = re.compile(r"resolve .+?: pred=([PB]) outcome=[PBT] (WIN|LOSE|TIE) pattern=(\S+)")
RE_V3B = re.compile(r"resolve .+?: pred=([PB]) outcome=[PBT] (WIN|LOSE|TIE)")

def load():
    v4meta = {}
    v3, v4 = [], []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        mts = RE_TS.match(line)
        if not mts: continue
        ts = time.mktime(time.strptime(mts.group(1), "%Y-%m-%d %H:%M:%S"))
        m = RE_PUB.search(line)
        if m:
            v4meta[m.group(1)] = (m.group(3), m.group(2)); continue
        m = RE_SET.search(line)
        if m:
            if m.group(2) == "TIE": continue
            meta = v4meta.get(m.group(1))
            if meta:
                v4.append({"ts": ts, "win": m.group(2)=="WIN", "side": meta[0], "pat": meta[1]})
            continue
        m = RE_V3.search(line)
        if m:
            if m.group(2) == "TIE": continue
            v3.append({"ts": ts, "win": m.group(2)=="WIN", "side": m.group(1), "pat": m.group(3)})
            continue
        m = RE_V3B.search(line)
        if m:
            if m.group(2) == "TIE": continue
            v3.append({"ts": ts, "win": m.group(2)=="WIN", "side": m.group(1), "pat": "?"})
    v3.sort(key=lambda x: x["ts"]); v4.sort(key=lambda x: x["ts"])
    return v3, v4

def z2(p1,n1,p2,n2):
    if not n1 or not n2: return 0.0
    p=(p1*n1+p2*n2)/(n1+n2); se=math.sqrt(p*(1-p)*(1/n1+1/n2)) or 1e-12
    return (p1-p2)/se

def d30_fleet(stream, all_ts):
    st = sorted(all_ts)
    for r in stream:
        t = r["ts"]
        r["d30"] = bisect.bisect_left(st, t) - bisect.bisect_left(st, t-1800)

def wr(rows):
    n=len(rows); w=sum(1 for r in rows if r["win"]); return (w,n,w/n*100 if n else 0)

def run(name, stream, all_ts):
    print(f"\n{'='*60}\n=== {name} n={len(stream)} ===")
    d30_fleet(stream, all_ts)
    k=int(len(stream)*0.7); tr,te=stream[:k],stream[k:]
    lo=sorted(r["d30"] for r in tr)[len(tr)//3]
    hi=sorted(r["d30"] for r in tr)[2*len(tr)//3]
    def split(rows):
        sp=[r for r in rows if r["d30"]<lo]; dn=[r for r in rows if r["d30"]>=hi]
        return sp,dn
    for part,seg in (("TRAIN",tr),("OOS",te)):
        sp,dn=split(seg)
        ws,ns,rs=wr(sp); wd,nd,rd=wr(dn)
        print(f"  [{part}] sparse={rs:.2f}%(n={ns}) dense={rd:.2f}%(n={nd}) "
              f"diff={rs-rd:+.2f}pt z={z2(ws/ns,ns,wd/nd,nd):+.2f}  (分岐~50.8%)")
    # pattern-stratified pooled (full stream)
    sp,dn=split(stream)
    bypat=collections.defaultdict(lambda:{"s":[0,0],"d":[0,0]})
    for r in sp: c=bypat[r["pat"]]["s"]; c[1]+=1; c[0]+=1 if r["win"] else 0
    for r in dn: c=bypat[r["pat"]]["d"]; c[1]+=1; c[0]+=1 if r["win"] else 0
    num=den=0.0
    for p,d in bypat.items():
        sn,dn_=d["s"][1],d["d"][1]
        if sn<50 or dn_<50: continue
        sw,dw=d["s"][0]/sn,d["d"][0]/dn_
        w=1.0/(1.0/sn+1.0/dn_); num+=w*(sw-dw); den+=w
    if den:
        print(f"  パターン別プール sparse-dense = {num/den*100:+.2f}pt (パターン構成交絡除去後)")

def main():
    v3,v4=load()
    all_ts=[r["ts"] for r in v3]+[r["ts"] for r in v4]
    print(f"[load] v3={len(v3)} v4={len(v4)} (v3 pat付き={sum(1 for r in v3 if r['pat']!='?')})")
    run("v3", v3, all_ts)
    run("v4", v4, all_ts)

if __name__=="__main__":
    main()
