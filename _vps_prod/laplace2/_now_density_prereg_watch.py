#!/usr/bin/env python3
"""_now_density_prereg_watch.py — NOW密度フィルタ 事前登録の前向き進捗チェック (2026-07-19凍結)

★凍結定義(変更禁止・NOW_DENSITY_PREREG_2026-07-19.md 参照):
  系統       = v4 のみ(v3はOOSで消滅=参考)
  密度       = フリート全体NOW到着 = v4[v4-publish]時刻 + v3 resolve時刻
  d30        = 現v4 NOWの到着(publish)時刻 t の直前30分[t-1800,t)の本数(自分除く)
  cut(凍結)  = sparse: d30 < 11 / dense: d30 >= 16 (mid=11..15は主判定から除外)
  対象       = publish_ts > FREEZE_EPOCH のv4 NOW(ex-TIE)・順張り的中率
  合格条件(全て): (a)sparse-dense>=+2.0pt (b)sparse>50.8% (c)z(sparse,dense)>=2.0 (d)sparse n>=1500

使い方: python3 _now_density_prereg_watch.py   (ユーザー「NOW密度チェックして」で実行)
"""
import re, time, math, bisect

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
FREEZE_EPOCH = 1784396816       # 2026-07-19 02:46:56
CUT_LO, CUT_HI = 11, 16
BREAKEVEN = 50.8
RE_TS  = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
RE_PUB = re.compile(r"\[v4-publish\] accepted did=(\S+) table=\S+ pattern=(\S+)\|([PB])")
RE_SET = re.compile(r"\[v4-settle\] posted settled did=(\S+) result=(WIN|LOSE|TIE)")
RE_V3  = re.compile(r"resolve .+?: pred=[PB] outcome=[PBT] (WIN|LOSE|TIE)")

def z2(p1,n1,p2,n2):
    if not n1 or not n2: return 0.0
    p=(p1*n1+p2*n2)/(n1+n2); se=math.sqrt(p*(1-p)*(1/n1+1/n2)) or 1e-12
    return (p1-p2)/se

def main():
    pub={}; v4=[]; v3_ts=[]
    for line in open(LOG, encoding="utf-8", errors="replace"):
        mts=RE_TS.match(line)
        if not mts: continue
        ts=time.mktime(time.strptime(mts.group(1),"%Y-%m-%d %H:%M:%S"))
        m=RE_PUB.search(line)
        if m: pub[m.group(1)]=(ts,m.group(3),m.group(2)); continue
        m=RE_SET.search(line)
        if m:
            if m.group(2)=="TIE": continue
            p=pub.get(m.group(1))
            if p: v4.append({"pub_ts":p[0],"win":m.group(2)=="WIN"})
            continue
        m=RE_V3.search(line)
        if m and m.group(1)!="TIE": v3_ts.append(ts)
    pop=sorted([p[0] for p in pub.values()]+v3_ts)
    for r in v4:
        t=r["pub_ts"]; r["d30"]=bisect.bisect_left(pop,t)-bisect.bisect_left(pop,t-1800)
    fwd=[r for r in v4 if r["pub_ts"]>FREEZE_EPOCH]
    sp=[r for r in fwd if r["d30"]<CUT_LO]; dn=[r for r in fwd if r["d30"]>=CUT_HI]
    def wr(rows):
        n=len(rows); w=sum(1 for r in rows if r["win"]); return w,n,(w/n*100 if n else 0)
    ws,ns,rs=wr(sp); wd,nd,rd=wr(dn)
    zz=z2(ws/ns,ns,wd/nd,nd) if ns and nd else 0
    span_days=(max((r["pub_ts"] for r in fwd),default=FREEZE_EPOCH)-FREEZE_EPOCH)/86400 if fwd else 0
    print(f"=== NOW密度フィルタ 事前登録 前向き進捗 (凍結2026-07-19) ===")
    print(f"前向きv4 NOW: {len(fwd)}件 / {span_days:.1f}日経過")
    print(f"  sparse(d30<{CUT_LO}): {rs:.2f}% (n={ns})")
    print(f"  dense (d30>={CUT_HI}): {rd:.2f}% (n={nd})")
    print(f"  sparse - dense = {rs-rd:+.2f}pt  z={zz:+.2f}")
    print(f"合格条件チェック:")
    print(f"  (a) sparse-dense>=+2.0pt : {'OK' if rs-rd>=2.0 else '未達'} ({rs-rd:+.2f})")
    print(f"  (b) sparse>50.8%         : {'OK' if rs>BREAKEVEN else '未達'} ({rs:.2f})")
    print(f"  (c) z>=2.0               : {'OK' if zz>=2.0 else '未達'} ({zz:+.2f})")
    print(f"  (d) sparse n>=1500       : {'OK' if ns>=1500 else '未達'} ({ns}/1500, 進捗{ns/1500*100:.0f}%)")
    allok = rs-rd>=2.0 and rs>BREAKEVEN and zz>=2.0 and ns>=1500
    print(f"総合: {'★合格=実装検討へ' if allok else 'データ蓄積中(中間値で判定/実装しない)'}")

if __name__=="__main__":
    main()
