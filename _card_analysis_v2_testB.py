#!/usr/bin/env python3
"""カードフィード第1回分析 テストB v2 (2026-07-15) — VPS上で実行

v1のdecide()リプレイはシュー境界不明で珠盤路格子がズレ仮想decisionが激減したため、
**実際のVPS botの決済**を使う方式に変更:
  v3: bot log の "resolve <卓名>: pred=X outcome=Y WIN|LOSE .. pattern=.." 行(結果込み)
  v4: bot log の "[v4-publish] accepted table=<qpid> pattern=.." 行(シグナル)を
      カードフィードの次ハンドで決済再構成(bot実装と同じ「次ハンドで解決・TIEはタイ」)
突合: 卓名→tb(フィードのtn充足行から) / qpid→卓名(master decisions DB)。
各decisionの「シグナルハンドまでの直近10ハンド」のナチュラル数/3枚目逆転数バケツ別に
的中率を比較(系統別・合算禁止)。
"""
import re
import sqlite3
import sys
import time
import collections

sys.path.insert(0, "/opt/laplace2")
from _card_analysis_v1 import NAT_WIN, bucket_nat, load_tables, wr, z_two_prop  # noqa: E402

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
DB = "/opt/bacopy/data/bacopy.sqlite3"
# 集計開始時刻(JST)。引数で上書き可: python3 _card_analysis_v2_testB.py "2026-07-15 08:00:00"
# ※事前登録OOS評価は必ず "2026-07-15 08:00:00" を渡す(集計ロジック・バケツ境界は変更禁止)
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-14 07:33:00"

RE_TS = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
RE_V3 = re.compile(
    RE_TS + r".* resolve (.+?): pred=([PB]) outcome=([PBT]) (WIN|LOSE|TIE).*pattern=(\S+)")
RE_V4 = re.compile(RE_TS + r".*\[v4-publish\] accepted did=\S+ table=(\S+) pattern=(\S+)")


def jst_epoch(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def report(sig, label):
    n_all = len(sig)
    w_all = sum(1 for s in sig if s["win"])
    out = [f"[B-v2] {label}: 実decision {n_all}件 的中率 {wr(w_all, n_all):.2f}%"]
    for key, title, bfn, order in (
        ("nat10", f"直近{NAT_WIN}ナチュラル数", bucket_nat, ("0-2", "3-4", "5+")),
        ("rev10", f"直近{NAT_WIN}逆転数",
         lambda k: "0" if k == 0 else ("1" if k == 1 else "2+"), ("0", "1", "2+")),
    ):
        out.append(f"  {title}バケツ別:")
        bk = collections.defaultdict(lambda: [0, 0])
        for s in sig:
            b = bfn(s[key])
            bk[b][0] += 1 if s["win"] else 0
            bk[b][1] += 1
        for b in order:
            w, n = bk[b]
            out.append(f"    {b}: {wr(w, n):.2f}% (n={n})")
        lo, hi = bk[order[0]], bk[order[-1]]
        out.append(f"    z({order[-1]} vs {order[0]}) = {z_two_prop(hi[0], hi[1], lo[0], lo[1]):+.2f}")
    return "\n".join(out)


def window_counts(hands, end_idx):
    """hands[end_idx]をシグナルハンドとして、それを含む直近NAT_WIN件のnat/rev数。"""
    lo = max(0, end_idx - NAT_WIN + 1)
    seg = hands[lo:end_idx + 1]
    return sum(1 for h in seg if h["nat"]), sum(1 for h in seg if h["rev"])


def main():
    tables, _, _ = load_tables()
    since_ep = jst_epoch(SINCE)

    # 卓名 → tb (tn充足行から。フィードは同一tbで名が揺れない前提)
    name2tb = {}
    import json
    for line in open("/opt/laplace2/card_feed.jsonl", encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("k") == "r" and d.get("tn"):
            name2tb[str(d["tn"]).strip()] = str(d["tb"])

    # qpid → 卓名 (master decisions DB)
    qpid2name = {}
    con = sqlite3.connect(DB)
    for tid, tn in con.execute(
        "SELECT DISTINCT table_id, table_name FROM decisions "
        "WHERE received_at > '2026-07-14' AND table_name != ''"
    ):
        qpid2name[str(tid)] = str(tn).strip()
    con.close()

    v3_sig, v4_sig = [], []
    v3_seen = v3_unmatched = v4_seen = v4_unmatched = 0
    for line in open(LOG, encoding="utf-8", errors="replace"):
        m = RE_V3.match(line)
        if m:
            t_res = jst_epoch(m.group(1))
            if t_res < since_ep:
                continue
            name, side, outcome, res = m.group(2), m.group(3), m.group(4), m.group(5)
            if res == "TIE":
                continue
            v3_seen += 1
            tb = name2tb.get(name.strip())
            hands = tables.get(tb) or []
            # 決済ハンド=resolve時刻±45sで outcome が一致する最近接ハンド
            best_i, best_dt = -1, 45.0
            for i, h in enumerate(hands):
                dt = abs(h["ts"] - t_res)
                if h["win"] == outcome and dt < best_dt:
                    best_i, best_dt = i, dt
                if h["ts"] > t_res + 60:
                    break
            if best_i <= 0:
                v3_unmatched += 1
                continue
            nat10, rev10 = window_counts(hands, best_i - 1)  # シグナルハンド=決済の1つ前
            v3_sig.append({"nat10": nat10, "rev10": rev10, "win": res == "WIN",
                           "pk": m.group(6)})
            continue
        m = RE_V4.match(line)
        if m:
            t_sig = jst_epoch(m.group(1))
            if t_sig < since_ep:
                continue
            v4_seen += 1
            qpid, pk = m.group(2), m.group(3)
            side = pk.rsplit("|", 1)[-1]
            tb = name2tb.get(qpid2name.get(qpid, ""))
            hands = tables.get(tb) or []
            # シグナルハンド=publish時刻以前の最後のハンド(+10s slack)
            j = -1
            for i, h in enumerate(hands):
                if h["ts"] <= t_sig + 10:
                    j = i
                else:
                    break
            if j < 0 or j + 1 >= len(hands):
                v4_unmatched += 1
                continue
            settle = hands[j + 1]
            if settle["win"] == "T":
                continue  # bot実装と同じ: TIEはタイ(勝敗に数えない)
            if settle["ts"] - t_sig > 120:
                v4_unmatched += 1  # 次ハンドが遠すぎる=突合不良
                continue
            nat10, rev10 = window_counts(hands, j)
            v4_sig.append({"nat10": nat10, "rev10": rev10, "win": settle["win"] == side,
                           "pk": pk})

    print(f"v3: log {v3_seen}件 → 突合 {len(v3_sig)}件 (不一致 {v3_unmatched})")
    print(f"v4: log {v4_seen}件 → 突合 {len(v4_sig)}件 (不一致 {v4_unmatched}, TIE除く)")
    print()
    print(report(v3_sig, "v3(6パターン)"))
    print()
    print(report(v4_sig, "v4(10パターン)"))
    print()
    print(stratified(v4_sig, "v4", "rev10", lambda k: k == 0, "rev0 vs rev1+"))
    print(stratified(v4_sig, "v4", "nat10", lambda k: k >= 5, "nat5+ vs nat0-4"))
    print(stratified(v3_sig, "v3", "rev10", lambda k: k == 0, "rev0 vs rev1+"))


def stratified(sig, label, key, in_hi, title):
    """パターン層別の交絡チェック: 各pattern_key内で hi群 vs lo群 の勝率差を出し、
    層別プール(Mantel-Haenszel風の単純加重z)で「パターン構成では説明できない差か」を見る。"""
    by_pk = collections.defaultdict(lambda: {"hw": 0, "hn": 0, "lw": 0, "ln": 0})
    for s in sig:
        g = by_pk[s["pk"]]
        if in_hi(s[key]):
            g["hw"] += 1 if s["win"] else 0
            g["hn"] += 1
        else:
            g["lw"] += 1 if s["win"] else 0
            g["ln"] += 1
    out = [f"[層別] {label} {title} (pattern内比較・n>=10の層のみ):"]
    num = den = 0.0
    for pk, g in sorted(by_pk.items(), key=lambda kv: -(kv[1]["hn"] + kv[1]["ln"])):
        n = g["hn"] + g["ln"]
        if n < 10 or not g["hn"] or not g["ln"]:
            continue
        ph, pl = g["hw"] / g["hn"], g["lw"] / g["ln"]
        wgt = 1.0 / (0.25 / g["hn"] + 0.25 / g["ln"])  # 逆分散重み(p=0.5近似)
        num += (ph - pl) * wgt
        den += wgt
        out.append(f"  {pk}: hi {ph*100:.1f}%({g['hn']}) lo {pl*100:.1f}%({g['ln']}) "
                   f"diff {(ph-pl)*100:+.1f}pt")
    if den:
        d = num / den
        se = (1.0 / den) ** 0.5
        out.append(f"  層別プール差 = {d*100:+.2f}pt z={d/se:+.2f} "
                   f"(パターン構成を固定してもこの差が残るか)")
    return "\n".join(out)


if __name__ == "__main__":
    main()
