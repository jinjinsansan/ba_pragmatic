#!/usr/bin/env python3
"""_bt_hotcold_regime.py — 好不調レジーム方向検証 (2026-07-19)

翻訳(ユーザー確認済 2026-07-19):
  「調子」= 各系統(v3/v4)の直近N手の順張り的中率(=フリートの直近成績)。
  各決済の *直前* までの順張り勝敗だけで計算(look-ahead厳禁)。N=20,50。
  三分位(下位=不調/中位/好調)で層別し、各局面で
    順張り(NOWそのまま)的中率 と 逆張り(反転)的中率 を別々に集計。
  好調→順が手数料分岐(~50.8%)超えるか / 不調→逆が超えるか。
  v3/v4別々で両系統同方向のみ採用。OOS(時系列7:3分割・閾値はtrainで確定)。
  対照: 勝敗列をシャッフル(iid化)して、三分位平均回帰の見かけ効果を定量。

データ: /opt/laplace2/dual_line_pragmatic_bot.log (5/22〜, dry-run signal stream)
  v4: [v4-publish] accepted did=.. pattern=..|SIDE  +  [v4-settle] posted settled did=.. result=WIN/LOSE
  v3: resolve <name>: pred=SIDE outcome=X WIN/LOSE  (self-contained)
TIE除外。系統内でハンド重複は decision id / 行単位で自然に一意。
"""
import re, sys, time, random, math, collections

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
random.seed(19)

RE_TS   = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
RE_PUB  = re.compile(r"\[v4-publish\] accepted did=(\S+) table=\S+ pattern=\S+\|([PB])")
RE_SET  = re.compile(r"\[v4-settle\] posted settled did=(\S+) result=(WIN|LOSE|TIE)")
RE_V3   = re.compile(r"resolve .+?: pred=([PB]) outcome=[PBT] (WIN|LOSE|TIE)")

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
            v4map[m.group(1)] = m.group(2)
            continue
        m = RE_SET.search(line)
        if m:
            if m.group(2) == "TIE":
                continue
            side = v4map.get(m.group(1))
            if side:
                v4.append({"ts": ts, "win": m.group(2) == "WIN", "side": side})
            continue
        m = RE_V3.search(line)
        if m:
            if m.group(2) == "TIE":
                continue
            v3.append({"ts": ts, "win": m.group(2) == "WIN", "side": m.group(1)})
    v3.sort(key=lambda x: x["ts"]); v4.sort(key=lambda x: x["ts"])
    return v3, v4

def pnl_bet(side, mywin):
    # side = 実際に賭ける側; mywin = その賭けが当たったか
    if mywin:
        return 0.95 if side == "B" else 1.0
    return -1.0

def rolling_rate(stream, N):
    """各iに対し、直前min(N,i)手の順張り勝率(i未満のみ)。i<Nは母数不足でNone。"""
    out = [None] * len(stream)
    dq = collections.deque(maxlen=N)
    for i, s in enumerate(stream):
        if len(dq) == N:
            out[i] = sum(dq) / N
        dq.append(1 if s["win"] else 0)
    return out

def analyze(stream, N, label, cut=None, shuffle=False):
    st = stream
    if shuffle:
        wins = [s["win"] for s in st]
        random.shuffle(wins)
        st = [{"ts": s["ts"], "win": w, "side": s["side"]} for s, w in zip(st, wins)]
    rr = rolling_rate(st, N)
    idxs = [i for i in range(len(st)) if rr[i] is not None]
    rates = sorted(rr[i] for i in idxs)
    if cut is None:
        lo_c = rates[len(rates)//3]
        hi_c = rates[2*len(rates)//3]
    else:
        lo_c, hi_c = cut
    bins = {"cold": [], "mid": [], "hot": []}
    for i in idxs:
        b = "cold" if rr[i] < lo_c else ("hot" if rr[i] >= hi_c else "mid")
        bins[b].append(st[i])
    res = {}
    for b, rows in bins.items():
        n = len(rows)
        if not n:
            continue
        jun_hits = sum(1 for r in rows if r["win"])
        jun_pnl = sum(pnl_bet(r["side"], r["win"]) for r in rows)
        gya_pnl = sum(pnl_bet("P" if r["side"] == "B" else "B", not r["win"]) for r in rows)
        res[b] = {"n": n, "jun_wr": jun_hits/n, "gya_wr": (n-jun_hits)/n,
                  "jun_pnl": jun_pnl/n, "gya_pnl": gya_pnl/n}
    return res, (lo_c, hi_c)

def z2(p1, n1, p2, n2):
    if not n1 or not n2: return 0.0
    p = (p1*n1+p2*n2)/(n1+n2)
    se = math.sqrt(p*(1-p)*(1/n1+1/n2)) or 1e-12
    return (p1-p2)/se

def report(stream, name):
    print(f"\n{'='*66}\n=== {name}  n={len(stream)}  ===")
    base = sum(1 for s in stream if s["win"])/len(stream)
    print(f"  全体 順張り的中率(base) = {base*100:.2f}%  (手数料分岐 ~50.8%)")
    # time split 70/30
    k = int(len(stream)*0.7)
    tr, te = stream[:k], stream[k:]
    for N in (20, 50):
        print(f"\n  --- N={N} ---")
        _, cut = analyze(tr, N, name)          # cutpoints from train
        for part, seg in (("TRAIN", tr), ("OOS", te)):
            res, _ = analyze(seg, N, name, cut=cut)
            print(f"   [{part}] rolling-rate三分位(cut={cut[0]*100:.0f}/{cut[1]*100:.0f}%):")
            for b in ("cold", "mid", "hot"):
                if b not in res: continue
                r = res[b]
                print(f"     {b:4s} n={r['n']:5d}  順={r['jun_wr']*100:5.2f}%(pnl{r['jun_pnl']*100:+5.2f}%)"
                      f"  逆={r['gya_wr']*100:5.2f}%(pnl{r['gya_pnl']*100:+5.2f}%)")
            # 好不調スイッチ: hot→順 / cold→逆 / mid→順 の合成的中率
            if all(b in res for b in ("cold", "hot")):
                sw_hit = res["hot"]["jun_wr"]*res["hot"]["n"] + res["cold"]["gya_wr"]*res["cold"]["n"] \
                         + (res["mid"]["jun_wr"]*res["mid"]["n"] if "mid" in res else 0)
                sw_n = res["hot"]["n"]+res["cold"]["n"]+(res["mid"]["n"] if "mid" in res else 0)
                sw_pnl = res["hot"]["jun_pnl"]*res["hot"]["n"] + res["cold"]["gya_pnl"]*res["cold"]["n"] \
                         + (res["mid"]["jun_pnl"]*res["mid"]["n"] if "mid" in res else 0)
                print(f"     switch(hot順/cold逆/mid順): 的中={sw_hit/sw_n*100:.2f}% pnl={sw_pnl/sw_n*100:+.2f}%/bet (n={sw_n})")
        # shuffle control on full stream: distribution of (hot順wr - cold順wr)
        obs_res, _ = analyze(stream, N, name, cut=cut)
        obs_gap = obs_res["hot"]["jun_wr"] - obs_res["cold"]["jun_wr"]
        sh_gaps = []
        for _ in range(300):
            sr, _ = analyze(stream, N, name, cut=cut, shuffle=True)
            if "hot" in sr and "cold" in sr:
                sh_gaps.append(sr["hot"]["jun_wr"] - sr["cold"]["jun_wr"])
        mu = sum(sh_gaps)/len(sh_gaps)
        sd = (sum((g-mu)**2 for g in sh_gaps)/len(sh_gaps))**0.5 or 1e-9
        print(f"   対照(shuffle300): 観測hot順−cold順={obs_gap*100:+.2f}pt / "
              f"iid期待={mu*100:+.2f}pt sd={sd*100:.2f} → z={(obs_gap-mu)/sd:+.2f}")

def main():
    v3, v4 = load()
    print(f"[load] v3={len(v3)} v4={len(v4)}  span={time.strftime('%m/%d', time.localtime(v3[0]['ts']))}"
          f"〜{time.strftime('%m/%d', time.localtime(v3[-1]['ts']))}")
    report(v3, "v3(6パターン)")
    report(v4, "v4(10パターン)")

if __name__ == "__main__":
    main()
