#!/usr/bin/env python3
"""カード分析v3: 「ドラマチック配布」仮説(ユーザー言語化 2026-07-15) — VPS上で実行

ユーザーの機序: 好調な時間は
  M1: ハンドの1枚目(プレイヤー1枚目)にバカラ値7/8/9がよく出る
  M2: ナチュラル8/9が出る
  M3: 相手が2枚で7-8リードでも、3枚目で9を作って逆転する(逆転9・厳格版)
がドラマチックに目立つ。これが見えない窓では×が固まる。

検定: 実decision(v3/v4)×カードフィード突合(v2と同方式)で、シグナル直前10ハンドの
  drama10 = M2またはM3の該当ハンド数 (主特徴)
  m1_10   = M1該当ハンド数 (副特徴・基準値 10×3/13≈2.3)
  m3_10   = M3該当ハンド数 (単体・希少)
バケツ別の的中率を比較。系統別(合算禁止)。

⚠️本スクリプトは探索段階(R3の区切り決定用)。凍結済みR1/R2(_card_analysis_v2_testB.py)には触れない。
使い方: python3 _card_analysis_v3_drama.py ["2026-07-14 07:33:00"]
"""
import collections
import json
import re
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/laplace2")
from _card_analysis_v1 import card_val, wr, z_two_prop  # noqa: E402

FEED = "/opt/laplace2/card_feed.jsonl"
LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
DB = "/opt/bacopy/data/bacopy.sqlite3"
WIN_N = 10

RE_TS = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
RE_V3 = re.compile(RE_TS + r".* resolve (.+?): pred=([PB]) outcome=([PBT]) (WIN|LOSE|TIE)")
RE_V4 = re.compile(RE_TS + r".*\[v4-publish\] accepted did=\S+ table=(\S+) pattern=(\S+)")


def jst_epoch(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def load_tables_drama():
    tables = collections.defaultdict(list)
    with open(FEED, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("k") != "r":
                continue
            win = d.get("win")
            if win not in ("P", "B", "T"):
                continue
            pc, bc = d.get("pc") or [], d.get("bc") or []
            ps, bs = d.get("ps"), d.get("bs")
            m2 = (len(pc) == 2 and ps in (8, 9)) or (len(bc) == 2 and bs in (8, 9))
            # M1: プレイヤー1枚目のバカラ値が7/8/9 (10/絵札=0は含まない)
            v0 = card_val(pc[0]) if pc else None
            m1 = v0 in (7, 8, 9)
            # M3(厳格): 2枚時点で相手が7or8でリード → 勝者が3枚目で最終9を作り逆転
            m3 = False
            if len(pc) >= 2 and len(bc) >= 2 and win in ("P", "B"):
                vals = [card_val(x) for x in pc[:2] + bc[:2]]
                if None not in vals:
                    p2 = (vals[0] + vals[1]) % 10
                    b2 = (vals[2] + vals[3]) % 10
                    ok = True
                    if len(pc) == 2 and isinstance(ps, int) and p2 != ps:
                        ok = False  # 数値コード検証NGハンドは除外
                    if ok:
                        leader2 = "P" if p2 > b2 else ("B" if b2 > p2 else "")
                        if leader2 and win != leader2:
                            lead_total = p2 if leader2 == "P" else b2
                            wscore = ps if win == "P" else bs
                            wcards = pc if win == "P" else bc
                            if lead_total in (7, 8) and wscore == 9 and len(wcards) >= 3:
                                m3 = True
            tables[str(d.get("tb"))].append({
                "ts": d.get("ts") or 0.0, "win": win,
                "m1": m1, "m2": m2, "m3": m3, "drama": (m2 or m3),
            })
    for tb in tables:
        tables[tb].sort(key=lambda h: h["ts"])
    return tables


def window_feats(hands, end_idx):
    lo = max(0, end_idx - WIN_N + 1)
    seg = hands[lo:end_idx + 1]
    return (sum(1 for h in seg if h["drama"]),
            sum(1 for h in seg if h["m1"]),
            sum(1 for h in seg if h["m3"]))


def collect(tables, since_ep):
    name2tb = {}
    for line in open(FEED, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("k") == "r" and d.get("tn"):
            name2tb[str(d["tn"]).strip()] = str(d["tb"])
    qpid2name = {}
    con = sqlite3.connect(DB)
    for tid, tn in con.execute(
        "SELECT DISTINCT table_id, table_name FROM decisions "
        "WHERE received_at > '2026-07-14' AND table_name != ''"
    ):
        qpid2name[str(tid)] = str(tn).strip()
    con.close()

    v3_sig, v4_sig = [], []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        m = RE_V3.match(line)
        if m:
            t_res = jst_epoch(m.group(1))
            if t_res < since_ep or m.group(5) == "TIE":
                continue
            hands = tables.get(name2tb.get(m.group(2).strip())) or []
            best_i, best_dt = -1, 45.0
            for i, h in enumerate(hands):
                dt = abs(h["ts"] - t_res)
                if h["win"] == m.group(4) and dt < best_dt:
                    best_i, best_dt = i, dt
                if h["ts"] > t_res + 60:
                    break
            if best_i <= 0:
                continue
            dr, m1c, m3c = window_feats(hands, best_i - 1)
            v3_sig.append({"drama": dr, "m1": m1c, "m3": m3c, "win": m.group(5) == "WIN"})
            continue
        m = RE_V4.match(line)
        if m:
            t_sig = jst_epoch(m.group(1))
            if t_sig < since_ep:
                continue
            side = m.group(3).rsplit("|", 1)[-1]
            hands = tables.get(name2tb.get(qpid2name.get(m.group(2), ""))) or []
            j = -1
            for i, h in enumerate(hands):
                if h["ts"] <= t_sig + 10:
                    j = i
                else:
                    break
            if j < 0 or j + 1 >= len(hands):
                continue
            settle = hands[j + 1]
            if settle["win"] == "T" or settle["ts"] - t_sig > 120:
                continue
            dr, m1c, m3c = window_feats(hands, j)
            v4_sig.append({"drama": dr, "m1": m1c, "m3": m3c, "win": settle["win"] == side})
    return v3_sig, v4_sig


def report(sig, label):
    n = len(sig)
    w = sum(1 for s in sig if s["win"])
    out = [f"[v3-drama] {label}: {n}件 全体 {wr(w, n):.2f}%"]
    for key, title, bfn, order in (
        ("drama", f"drama10(M2+M3該当ハンド数/直近{WIN_N})",
         lambda k: "0-2" if k <= 2 else ("3-4" if k <= 4 else "5+"), ("0-2", "3-4", "5+")),
        ("m1", f"m1_10(P1枚目7-9の数/基準2.3)",
         lambda k: "0-1" if k <= 1 else ("2-3" if k <= 3 else "4+"), ("0-1", "2-3", "4+")),
        ("m3", "m3_10(逆転9・厳格)",
         lambda k: "0" if k == 0 else "1+", ("0", "1+")),
    ):
        bk = collections.defaultdict(lambda: [0, 0])
        for s in sig:
            b = bfn(s[key])
            bk[b][0] += 1 if s["win"] else 0
            bk[b][1] += 1
        out.append(f"  {title}:")
        for b in order:
            ww, nn = bk[b]
            out.append(f"    {b}: {wr(ww, nn):.2f}% (n={nn})")
        lo, hi = bk[order[0]], bk[order[-1]]
        out.append(f"    z({order[-1]} vs {order[0]}) = {z_two_prop(hi[0], hi[1], lo[0], lo[1]):+.2f}")
    return "\n".join(out)


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-07-14 07:33:00"
    tables = load_tables_drama()
    n_hands = sum(len(v) for v in tables.values())
    f_m1 = sum(1 for v in tables.values() for h in v if h["m1"])
    f_m2 = sum(1 for v in tables.values() for h in v if h["m2"])
    f_m3 = sum(1 for v in tables.values() for h in v if h["m3"])
    print(f"hands={n_hands} M1(初手7-9)={wr(f_m1, n_hands):.1f}% (基準23.1%) "
          f"M2(nat)={wr(f_m2, n_hands):.1f}% M3(逆転9厳格)={wr(f_m3, n_hands):.2f}%")
    v3_sig, v4_sig = collect(tables, jst_epoch(since))
    print(f"since={since}")
    print()
    print(report(v3_sig, "v3(6パターン)"))
    print()
    print(report(v4_sig, "v4(10パターン)"))


if __name__ == "__main__":
    main()
