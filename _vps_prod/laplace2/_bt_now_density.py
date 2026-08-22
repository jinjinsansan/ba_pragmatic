#!/usr/bin/env python3
"""_bt_now_density.py — NOW密度レジーム方向検証 (2026-07-19)

仮説(ユーザー): NOWは時間的に固まって出る/過疎の時間帯もある。
  過疎の局面(久しぶりに出たNOW)ほど的中率が低い。密集ほど高い。

翻訳(ユーザー確認済 2026-07-19・密度は「フリート全体」と「系統別」の両方):
  各NOW(実決済)の直前情報のみで(look-ahead厳禁):
    density_T = 直前T分(30/60)に出たNOW本数
    gap       = 前NOWからの経過秒
  三分位(過疎/中/密集)で層別 → 各局面の順張り的中率を比較。仮説=密集>過疎。
  密度の単位: (A)フリート全体=全卓合算NOW / (B)系統別=v3内・v4内。
  的中率は常に系統別に集計。時間帯(時刻)層別でも密度差が残るか交絡チェック。

データ: /opt/laplace2/dual_line_pragmatic_bot.log (5/22〜, dry-run)
"""
import re, sys, time, math, collections

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
RE_TS  = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
RE_PUB = re.compile(r"\[v4-publish\] accepted did=(\S+) table=\S+ pattern=\S+\|([PB])")
RE_SET = re.compile(r"\[v4-settle\] posted settled did=(\S+) result=(WIN|LOSE|TIE)")
RE_V3  = re.compile(r"resolve .+?: pred=([PB]) outcome=[PBT] (WIN|LOSE|TIE)")

def load():
    v4map = {}
    v3, v4 = [], []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        mts = RE_TS.match(line)
        if not mts:
            continue
        ts = time.mktime(time.strptime(mts.group(1), "%Y-%m-%d %H:%M:%S"))
        m = RE_PUB.search(line)
        if m:
            v4map[m.group(1)] = m.group(2); continue
        m = RE_SET.search(line)
        if m:
            if m.group(2) == "TIE": continue
            side = v4map.get(m.group(1))
            if side:
                v4.append({"ts": ts, "win": m.group(2) == "WIN", "side": side})
            continue
        m = RE_V3.search(line)
        if m:
            if m.group(2) == "TIE": continue
            v3.append({"ts": ts, "win": m.group(2) == "WIN", "side": m.group(1)})
    v3.sort(key=lambda x: x["ts"]); v4.sort(key=lambda x: x["ts"])
    return v3, v4

def add_density(target, source_ts):
    """target各行に density30/density60(source_tsの本数, 直前・自分除く)とgap(target内)を付与。
    source_ts は密度カウント母集団(全卓合算 or 同系統)のts昇順list。"""
    st = sorted(source_ts)
    import bisect
    prev_ts = None
    j = 0
    for r in target:
        t = r["ts"]
        lo30 = bisect.bisect_left(st, t - 1800)
        lo60 = bisect.bisect_left(st, t - 3600)
        hi   = bisect.bisect_left(st, t)          # strictly before t
        r["d30"] = hi - lo30
        r["d60"] = hi - lo60
        # gap = 直前の同系統NOWからの経過(targetは同系統前提)
    # gap over target itself
    for i, r in enumerate(target):
        r["gap"] = (r["ts"] - target[i-1]["ts"]) if i > 0 else None

def terciles(vals):
    s = sorted(vals)
    return s[len(s)//3], s[2*len(s)//3]

def z2(p1, n1, p2, n2):
    if not n1 or not n2: return 0.0
    p = (p1*n1+p2*n2)/(n1+n2)
    se = math.sqrt(p*(1-p)*(1/n1+1/n2)) or 1e-12
    return (p1-p2)/se

def bin_report(stream, key, label, higher_is_dense=True):
    rows = [r for r in stream if r.get(key) is not None]
    lo, hi = terciles([r[key] for r in rows])
    b = {"sparse": [], "mid": [], "dense": []}
    for r in rows:
        v = r[key]
        if higher_is_dense:
            grp = "sparse" if v < lo else ("dense" if v >= hi else "mid")
        else:  # gap: 大きいほど過疎
            grp = "dense" if v < lo else ("sparse" if v >= hi else "mid")
        b[grp].append(r)
    out = []
    wr = {}
    for g in ("sparse", "mid", "dense"):
        rows_g = b[g]
        n = len(rows_g)
        if not n:
            wr[g] = None; continue
        w = sum(1 for r in rows_g if r["win"])
        wr[g] = (w, n)
        out.append(f"{g}={w/n*100:.2f}%(n={n})")
    line = f"    {label} [cut={lo}/{hi}]: " + " ".join(out)
    if wr["dense"] and wr["sparse"]:
        zz = z2(wr["dense"][0]/wr["dense"][1], wr["dense"][1],
                wr["sparse"][0]/wr["sparse"][1], wr["sparse"][1])
        line += f"  z(dense-sparse)={zz:+.2f}"
    print(line)
    return wr

def hour_control(stream, key, higher_is_dense=True):
    """時間帯層別: 各時刻(JST)内で dense vs sparse の的中率差をプールしてz。"""
    rows = [r for r in stream if r.get(key) is not None]
    lo, hi = terciles([r[key] for r in rows])
    # per hour, split dense/sparse by global cut
    agg_d = [0,0]; agg_s = [0,0]
    for r in rows:
        v = r[key]
        if higher_is_dense:
            g = "sparse" if v < lo else ("dense" if v >= hi else "mid")
        else:
            g = "dense" if v < lo else ("sparse" if v >= hi else "mid")
        if g == "mid": continue
        # weight within-hour by removing hour mean -> use stratified pooled via hour buckets
        r["_g"] = g
    byhour = collections.defaultdict(lambda: {"dense":[0,0], "sparse":[0,0]})
    for r in rows:
        g = r.get("_g")
        if not g: continue
        h = int((r["ts"] + 9*3600) // 3600 % 24)
        c = byhour[h][g]
        c[1] += 1; c[0] += 1 if r["win"] else 0
    # Cochran-Mantel-Haenszel-ish: sum within-hour (dense_wr - sparse_wr) weighted by n
    num = den = 0.0
    for h, d in byhour.items():
        dn, sn = d["dense"][1], d["sparse"][1]
        if dn < 20 or sn < 20: continue
        dwr, swr = d["dense"][0]/dn, d["sparse"][0]/sn
        w = 1.0/(1.0/dn + 1.0/sn)
        num += w*(dwr-swr); den += w
    if den:
        print(f"      └ 時間帯層別プール dense-sparse = {num/den*100:+.2f}pt (時刻交絡除去後)")

def report(v3, v4):
    # density source sets
    all_ts = sorted([r["ts"] for r in v3] + [r["ts"] for r in v4])
    for name, stream, own_ts in (("v3", v3, [r["ts"] for r in v3]),
                                 ("v4", v4, [r["ts"] for r in v4])):
        print(f"\n{'='*64}\n=== {name}  n={len(stream)} base順張り={sum(r['win'] for r in stream)/len(stream)*100:.2f}% ===")
        # (A) fleet-wide density
        add_density(stream, all_ts)
        print("  [A] フリート全体NOW密度:")
        bin_report(stream, "d30", "直前30分本数", higher_is_dense=True)
        hour_control(stream, "d30", higher_is_dense=True)
        bin_report(stream, "d60", "直前60分本数", higher_is_dense=True)
        bin_report(stream, "gap", "前NOW間隔(系統内)", higher_is_dense=False)
        hour_control(stream, "gap", higher_is_dense=False)
        # (B) same-lineage density
        add_density(stream, own_ts)
        print("  [B] 系統別NOW密度:")
        bin_report(stream, "d30", "直前30分本数(系統内)", higher_is_dense=True)
        hour_control(stream, "d30", higher_is_dense=True)
        bin_report(stream, "d60", "直前60分本数(系統内)", higher_is_dense=True)

def main():
    v3, v4 = load()
    print(f"[load] v3={len(v3)} v4={len(v4)}")
    report(v3, v4)

if __name__ == "__main__":
    main()
