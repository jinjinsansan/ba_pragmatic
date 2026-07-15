#!/usr/bin/env python3
"""窓幅スイープ探索 (2026-07-16) — VPS上で実行。事前登録R1/R2(窓10)とは別の探索専用。

ユーザー質問「直近30ハンドとかでも無意味か?」に答える。
testB(_card_analysis_v2_testB.py)と同一の実決済突合を使い、窓幅W∈{10,20,30}の
直近nat数/rev数(いずれもBET時点で既知=因果的)を**三分位**で比較する。
窓30では rev=0 がほぼ存在しない(期待値~4.7)ためゼロ判定は使えない。

注意: 多重比較(窓3×特徴2×系統2)の探索であり、ここの数字で判定・実装はしない。
"""
import re
import sqlite3
import sys
import time
import collections
import json
import glob
import itertools

sys.path.insert(0, "/opt/laplace2")
from _card_analysis_v1 import load_tables, wr, z_two_prop  # noqa: E402

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
DB = "/opt/bacopy/data/bacopy.sqlite3"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-15 08:00:00"
WINDOWS = (10, 20, 30)

RE_TS = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
RE_V3 = re.compile(
    RE_TS + r".* resolve (.+?): pred=([PB]) outcome=([PBT]) (WIN|LOSE|TIE).*pattern=(\S+)")
RE_V4 = re.compile(RE_TS + r".*\[v4-publish\] accepted did=\S+ table=(\S+) pattern=(\S+)")


def jst_epoch(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def wcounts(hands, end_idx, w):
    lo = max(0, end_idx - w + 1)
    seg = hands[lo:end_idx + 1]
    return sum(1 for h in seg if h["nat"]), sum(1 for h in seg if h["rev"]), len(seg)


def collect():
    tables, _, _ = load_tables()
    since_ep = jst_epoch(SINCE)

    name2tb = {}
    for line in itertools.chain(*[open(x, encoding="utf-8")
                                  for x in sorted(glob.glob("/opt/laplace2/card_feed.jsonl*"))]):
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
            if t_res < since_ep:
                continue
            name, side, outcome, res = m.group(2), m.group(3), m.group(4), m.group(5)
            if res == "TIE":
                continue
            tb = name2tb.get(name.strip())
            hands = tables.get(tb) or []
            best_i, best_dt = -1, 45.0
            for i, h in enumerate(hands):
                dt = abs(h["ts"] - t_res)
                if h["win"] == outcome and dt < best_dt:
                    best_i, best_dt = i, dt
                if h["ts"] > t_res + 60:
                    break
            if best_i <= 0:
                continue
            s = {"win": res == "WIN", "pk": m.group(6)}
            for w in WINDOWS:
                nat, rev, ln = wcounts(hands, best_i - 1, w)
                s[f"nat{w}"], s[f"rev{w}"], s[f"full{w}"] = nat, rev, ln == w
            v3_sig.append(s)
            continue
        m = RE_V4.match(line)
        if m:
            t_sig = jst_epoch(m.group(1))
            if t_sig < since_ep:
                continue
            qpid, pk = m.group(2), m.group(3)
            side = pk.rsplit("|", 1)[-1]
            tb = name2tb.get(qpid2name.get(qpid, ""))
            hands = tables.get(tb) or []
            j = -1
            for i, h in enumerate(hands):
                if h["ts"] <= t_sig + 10:
                    j = i
                else:
                    break
            if j < 0 or j + 1 >= len(hands):
                continue
            settle = hands[j + 1]
            if settle["win"] == "T":
                continue
            if settle["ts"] - t_sig > 120:
                continue
            s = {"win": settle["win"] == side, "pk": pk}
            for w in WINDOWS:
                nat, rev, ln = wcounts(hands, j, w)
                s[f"nat{w}"], s[f"rev{w}"], s[f"full{w}"] = nat, rev, ln == w
            v4_sig.append(s)
    return v3_sig, v4_sig


def terciles(vals):
    """整数カウント列の三分位しきい値 (t1, t2)。low<=t1 / high>=t2。"""
    sv = sorted(vals)
    t1 = sv[len(sv) // 3]
    t2 = sv[(2 * len(sv)) // 3]
    return t1, t2


def sweep(sig, label):
    out = [f"=== {label}: n={len(sig)} 的中率 {wr(sum(s['win'] for s in sig), len(sig)):.2f}% ==="]
    for w in WINDOWS:
        full = [s for s in sig if s[f"full{w}"]]
        out.append(f"-- 窓{w} (フル窓のみ n={len(full)}) --")
        for feat, good_side in (("nat", "high"), ("rev", "low")):
            key = f"{feat}{w}"
            vals = [s[key] for s in full]
            if not vals:
                continue
            t1, t2 = terciles(vals)
            if t1 == t2:
                # 分布が狭く三分位が潰れる場合は中央値で2分割
                groups = {"low": [s for s in full if s[key] <= t1],
                          "high": [s for s in full if s[key] > t1]}
                gorder = ("low", "high")
                thr = f"低<= {t1} < 高"
            else:
                groups = {"low": [s for s in full if s[key] <= t1],
                          "mid": [s for s in full if t1 < s[key] < t2],
                          "high": [s for s in full if s[key] >= t2]}
                gorder = ("low", "mid", "high")
                thr = f"低<={t1} / 高>={t2}"
            line = [f"  {feat}({thr}):"]
            for g in gorder:
                gs = groups[g]
                line.append(f"{g} {wr(sum(s['win'] for s in gs), len(gs)):.1f}%({len(gs)})")
            hi, lo = groups["high"], groups["low"]
            z = z_two_prop(sum(s["win"] for s in hi), len(hi),
                           sum(s["win"] for s in lo), len(lo))
            # 仮説方向: natは多いほど良い(+z) / revは少ないほど良い(-z)
            hyp = z if good_side == "high" else -z
            line.append(f"z(高vs低)={z:+.2f} 仮説方向z={hyp:+.2f}")
            out.append(" ".join(line))
            out.append("    " + strat_line(full, key, t1, t2, good_side))
    return "\n".join(out)


def strat_line(full, key, t1, t2, good_side):
    """パターン層別プール差(仮説方向group vs その他)。testBのstratifiedと同じ逆分散重み。"""
    def in_hi(s):
        return (s[key] >= t2) if good_side == "high" else (s[key] <= t1)
    by_pk = collections.defaultdict(lambda: [0, 0, 0, 0])  # hw hn lw ln
    for s in full:
        g = by_pk[s["pk"]]
        if in_hi(s):
            g[0] += 1 if s["win"] else 0
            g[1] += 1
        else:
            g[2] += 1 if s["win"] else 0
            g[3] += 1
    num = den = 0.0
    for g in by_pk.values():
        if g[1] + g[3] < 10 or not g[1] or not g[3]:
            continue
        wgt = 1.0 / (0.25 / g[1] + 0.25 / g[3])
        num += (g[0] / g[1] - g[2] / g[3]) * wgt
        den += wgt
    if not den:
        return "層別: 層不足"
    d = num / den
    se = (1.0 / den) ** 0.5
    return f"層別プール差(仮説方向vs他)={d*100:+.2f}pt z={d/se:+.2f}"


def main():
    v3_sig, v4_sig = collect()
    print(f"SINCE={SINCE}")
    print(sweep(v4_sig, "v4(本命)"))
    print()
    print(sweep(v3_sig, "v3(参考・v4と同ハンド重複あり)"))


if __name__ == "__main__":
    main()
