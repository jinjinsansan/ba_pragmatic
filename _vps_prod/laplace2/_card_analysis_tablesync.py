#!/usr/bin/env python3
"""テーブル同期検定 (2026-07-16) — VPS上で実行。探索専用(凍結R1/R2とは別)。

ユーザー確認済みの翻訳:
  A) nat率(rev率)が卓を跨いで時間同期するか。
     統計量 = 時間ブロックごとの「卓内平均を引いたnat率」の全卓平均の2乗平均 S。
     対照 = 各卓のブロック列を独立に巡回シフト(卓内の時間構造=シュー由来の
     クラスタリングは保存・卓間の時刻整列だけ破壊)×300回。
     検出可: 卓を跨いだ時間同期。検出不可: 同期の原因(人為か運用要因かは区別不能)。
  B) R1比較(v4 rev0 vs rev1+)を時間ブロック(3h)層別で再計算=時間交絡チェックの予行。
"""
import re
import sqlite3
import sys
import time
import collections
import json
import glob
import itertools
import random

sys.path.insert(0, "/opt/laplace2")
from _card_analysis_v1 import load_tables, wr, z_two_prop  # noqa: E402

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
DB = "/opt/bacopy/data/bacopy.sqlite3"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-15 08:00:00"
N_PERM = 300
SEED = 20260716

RE_TS = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
RE_V4 = re.compile(RE_TS + r".*\[v4-publish\] accepted did=\S+ table=(\S+) pattern=(\S+)")


def jst_epoch(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


# ---------- A) 卓間時間同期の permutation 検定 ----------

def sync_test(tables, feat, block_sec, min_n, rng):
    # (tb, block) ごとの率
    cells = collections.defaultdict(lambda: [0, 0])  # (tb, blk) -> [hit, n]
    for tb, hands in tables.items():
        for h in hands:
            blk = int(h["ts"] // block_sec)
            c = cells[(tb, blk)]
            c[0] += 1 if h[feat] else 0
            c[1] += 1
    # 卓ごとのブロック率列(n>=min_n のセルのみ・ブロック昇順)
    per_tb = collections.defaultdict(list)  # tb -> [(blk, rate)]
    for (tb, blk), (k, n) in cells.items():
        if n >= min_n:
            per_tb[tb].append((blk, k / n))
    per_tb = {tb: sorted(v) for tb, v in per_tb.items() if len(v) >= 5}
    # 卓内平均を引く(卓の恒常的な率の違いを除去)
    xs = {}  # tb -> [(blk, x)]
    for tb, v in per_tb.items():
        m = sum(r for _, r in v) / len(v)
        xs[tb] = [(blk, r - m) for blk, r in v]

    def stat(shift):
        by_blk = collections.defaultdict(list)
        for tb, v in xs.items():
            s = shift.get(tb, 0)
            L = len(v)
            for i, (blk, _) in enumerate(v):
                # 巡回シフト: ブロックの並びはそのまま、値だけ回す
                by_blk[blk].append(v[(i + s) % L][1])
        num = cnt = 0.0
        for blk, arr in by_blk.items():
            if len(arr) < 8:
                continue
            m = sum(arr) / len(arr)
            num += m * m
            cnt += 1
        return (num / cnt if cnt else 0.0), cnt

    s_obs, n_blk = stat({})
    perms = []
    for _ in range(N_PERM):
        shift = {tb: rng.randrange(1, len(v)) for tb, v in xs.items()}
        perms.append(stat(shift)[0])
    ge = sum(1 for p in perms if p >= s_obs)
    mu = sum(perms) / len(perms)
    sd = (sum((p - mu) ** 2 for p in perms) / len(perms)) ** 0.5
    z = (s_obs - mu) / sd if sd else 0.0
    return (f"  {feat} block={block_sec//60}min: S_obs={s_obs*1e4:.3f} "
            f"(対照 {mu*1e4:.3f}±{sd*1e4:.3f}) z={z:+.2f} "
            f"p={(ge+1)/(N_PERM+1):.3f} (卓{len(xs)}・ブロック{n_blk:.0f})")


# ---------- B) R1の時間ブロック層別(予行) ----------

def collect_v4(tables):
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

    sig = []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        m = RE_V4.match(line)
        if not m:
            continue
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
        if settle["win"] == "T" or settle["ts"] - t_sig > 120:
            continue
        lo = max(0, j - 9)
        seg = hands[lo:j + 1]
        rev10 = sum(1 for h in seg if h["rev"])
        sig.append({"win": settle["win"] == side, "pk": pk, "rev10": rev10, "ts": t_sig})
    return sig


def pooled_diff(sig, strata_fn, in_hi, title):
    by = collections.defaultdict(lambda: [0, 0, 0, 0])
    for s in sig:
        g = by[strata_fn(s)]
        if in_hi(s):
            g[0] += 1 if s["win"] else 0
            g[1] += 1
        else:
            g[2] += 1 if s["win"] else 0
            g[3] += 1
    num = den = 0.0
    used = 0
    for g in by.values():
        if g[1] + g[3] < 10 or not g[1] or not g[3]:
            continue
        wgt = 1.0 / (0.25 / g[1] + 0.25 / g[3])
        num += (g[0] / g[1] - g[2] / g[3]) * wgt
        den += wgt
        used += 1
    if not den:
        return f"  {title}: 層不足"
    d = num / den
    se = (1.0 / den) ** 0.5
    return f"  {title}: プール差={d*100:+.2f}pt z={d/se:+.2f} (有効層{used})"


def main():
    rng = random.Random(SEED)
    tables, _, _ = load_tables()
    n_hands = sum(len(v) for v in tables.values())
    print(f"フィード全期間 {n_hands}ハンド / {len(tables)}卓")
    print()
    print("[A] 卓間時間同期 permutation検定 (卓内構造保存・巡回シフト対照)")
    for feat in ("nat", "rev"):
        for block_sec, min_n in ((1800, 15), (3600, 30), (10800, 60)):
            print(sync_test(tables, feat, block_sec, min_n, rng))
    print()
    print(f"[B] R1(v4 rev0 vs rev1+)の層別再計算 予行 (SINCE={SINCE})")
    sig = collect_v4(tables)
    n = len(sig)
    hi = [s for s in sig if s["rev10"] == 0]
    lo = [s for s in sig if s["rev10"] > 0]
    print(f"  v4 n={n}: rev0 {wr(sum(s['win'] for s in hi), len(hi)):.1f}%({len(hi)}) "
          f"vs rev1+ {wr(sum(s['win'] for s in lo), len(lo)):.1f}%({len(lo)}) "
          f"生差={wr(sum(s['win'] for s in hi), len(hi)) - wr(sum(s['win'] for s in lo), len(lo)):+.1f}pt")
    in_hi = lambda s: s["rev10"] == 0  # noqa: E731
    print(pooled_diff(sig, lambda s: s["pk"], in_hi, "パターン層別(従来)"))
    print(pooled_diff(sig, lambda s: int(s["ts"] // 10800), in_hi, "時間3hブロック層別"))
    print(pooled_diff(sig, lambda s: (s["pk"], int(s["ts"] // 10800)), in_hi,
                      "パターン×時間 両層別"))


if __name__ == "__main__":
    main()
