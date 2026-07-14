#!/usr/bin/env python3
"""カードフィード第1回分析 (2026-07-15) — VPS上で実行 (PYTHONPATH不要・sys.path追加)

ユーザー仮説: 「波が来ている時はナチュラル(8/9)が多い・3枚目逆転も多い→波の方向を予想できる」

テストA(記述):
  A1: 継続ハンド(直前と同じ側が勝つ) vs 反転ハンドのナチュラル率
  A2(仮説の直接検定): 直近10ハンドのナチュラル数 → 次ハンドの継続率が変わるか
      + シャッフル対照(卓内で順序破壊×3)でアーティファクト検出
  A3: ナチュラル自己相関(直近10のナチュラル数 → 次ハンドがナチュラルか)
  A4: 3枚目逆転率でも A2 と同じ検定

テストB(本命・予測):
  本番と同じ decide() + v3/v4ホワイトリストをカードフィード出目系列にリプレイして
  仮想decisionを生成(卓ごとpending1件=本番同等)。直近10ハンドのナチュラル数/
  逆転数バケツ別に的中率を比較。系統別(合算禁止)。

既知の限界(v1): フィードにシュー境界が無いため、シュー跨ぎ窓が約1割混入(希釈方向)。
"""
import collections
import json
import random
import sys

sys.path.insert(0, "/opt/laplace2")
from dual_line_match import LIVE_SIGNAL_PATTERNS, decide  # noqa: E402

try:
    from dual_line_match import LIVE_SIGNAL_PATTERNS_V4
except ImportError:
    LIVE_SIGNAL_PATTERNS_V4 = frozenset()

FEED = "/opt/laplace2/card_feed.jsonl"
NAT_WIN = 10          # 直近何ハンドでナチュラル数を数えるか
SEQ_WINDOW = 180      # decide()に渡す系列の最大長(6の倍数境界で切る=珠盤路の列保存)


def card_val(c):
    """"QS"/"9H"/"0H"(=10) と 3桁数値コード("015"等) 両対応。10/J/Q/K=0, A=1。"""
    c = str(c or "")
    if c.isdigit() and len(c) >= 3:
        r = (int(c) - 1) % 13 + 1
        return 0 if r >= 10 else r
    r = c[:-1]
    if r in ("J", "Q", "K", "10", "0"):
        return 0
    if r in ("A", "1"):
        return 1
    try:
        v = int(r)
        return v if v < 10 else 0
    except Exception:
        return None


def load_tables():
    tables = collections.defaultdict(list)
    score_ok = score_ng = 0
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
            natural = (len(pc) == 2 and ps in (8, 9)) or (len(bc) == 2 and bs in (8, 9))
            # 2枚時点のリーダーと最終勝者の逆転(どちらかが3枚目を引いた手のみ)
            reversal = rev9 = False
            valid_vals = True
            if len(pc) >= 2 and len(bc) >= 2:
                vals = [card_val(x) for x in pc[:2] + bc[:2]]
                if None in vals:
                    valid_vals = False
                else:
                    p2 = (vals[0] + vals[1]) % 10
                    b2 = (vals[2] + vals[3]) % 10
                    # スコア自己検証: 2枚で終わった側は最終スコアと一致するはず
                    if len(pc) == 2 and isinstance(ps, int):
                        if p2 == ps:
                            score_ok += 1
                        else:
                            score_ng += 1
                            valid_vals = False
                    if valid_vals and (len(pc) > 2 or len(bc) > 2) and win in ("P", "B"):
                        leader2 = "P" if p2 > b2 else ("B" if b2 > p2 else "")
                        if leader2 and win != leader2:
                            reversal = True
                            wscore = ps if win == "P" else bs
                            rev9 = wscore == 9
            tables[str(d.get("tb"))].append({
                "ts": d.get("ts") or 0.0, "win": win, "nat": natural,
                "rev": reversal, "rev9": rev9, "vv": valid_vals,
            })
    for tb in tables:
        tables[tb].sort(key=lambda h: h["ts"])
    return tables, score_ok, score_ng


def wr(w, n):
    return (w / n * 100) if n else 0.0


def z_two_prop(w1, n1, w2, n2):
    if not n1 or not n2:
        return 0.0
    p1, p2 = w1 / n1, w2 / n2
    p = (w1 + w2) / (n1 + n2)
    se = (p * (1 - p) * (1 / n1 + 1 / n2)) ** 0.5
    return (p1 - p2) / se if se else 0.0


def bucket_nat(k):
    return "0-2" if k <= 2 else ("3-4" if k <= 4 else "5+")


def continuation_test(tables, label, shuffle=False, seed=0):
    """A2: 直近10ハンドのnat数 → 次ハンド継続率(直前の非T勝者と同じ側が勝つか)。"""
    rng = random.Random(seed)
    buckets = collections.defaultdict(lambda: [0, 0])  # bucket -> [cont, n]
    natnext = collections.defaultdict(lambda: [0, 0])  # A3: bucket -> [nat, n]
    for hands in tables.values():
        hs = list(hands)
        if shuffle:
            rng.shuffle(hs)
        wins = [h["win"] for h in hs]
        nats = [h["nat"] for h in hs]
        for i in range(NAT_WIN, len(hs)):
            k = sum(nats[i - NAT_WIN:i])
            b = bucket_nat(k)
            natnext[b][0] += 1 if nats[i] else 0
            natnext[b][1] += 1
            if wins[i] not in ("P", "B"):
                continue
            prev = next((wins[j] for j in range(i - 1, -1, -1) if wins[j] in ("P", "B")), "")
            if not prev:
                continue
            buckets[b][0] += 1 if wins[i] == prev else 0
            buckets[b][1] += 1
    out = [f"[A2{'/shuffle' if shuffle else ''}] {label}: 直近{NAT_WIN}のnat数 → 次ハンド継続率"]
    for b in ("0-2", "3-4", "5+"):
        c, n = buckets[b]
        out.append(f"  nat{b}: 継続 {wr(c, n):.2f}% (n={n})")
    lo, hi = buckets["0-2"], buckets["5+"]
    out.append(f"  z(5+ vs 0-2) = {z_two_prop(hi[0], hi[1], lo[0], lo[1]):+.2f}")
    if not shuffle:
        out.append(f"[A3] nat自己相関: 次ハンドがnatの率")
        for b in ("0-2", "3-4", "5+"):
            c, n = natnext[b]
            out.append(f"  nat{b}: 次nat率 {wr(c, n):.2f}% (n={n})")
        lo, hi = natnext["0-2"], natnext["5+"]
        out.append(f"  z(5+ vs 0-2) = {z_two_prop(hi[0], hi[1], lo[0], lo[1]):+.2f}")
    return "\n".join(out)


def a1_descriptive(tables):
    cont = [0, 0]
    brk = [0, 0]
    for hands in tables.values():
        wins = [h["win"] for h in hands]
        for i in range(1, len(hands)):
            if wins[i] not in ("P", "B"):
                continue
            prev = next((wins[j] for j in range(i - 1, -1, -1) if wins[j] in ("P", "B")), "")
            if not prev:
                continue
            tgt = cont if wins[i] == prev else brk
            tgt[0] += 1 if hands[i]["nat"] else 0
            tgt[1] += 1
    return (f"[A1] 継続ハンドのnat率 {wr(*cont):.2f}% (n={cont[1]}) vs "
            f"反転ハンドのnat率 {wr(*brk):.2f}% (n={brk[1]}) "
            f"z={z_two_prop(cont[0], cont[1], brk[0], brk[1]):+.2f}")


def replay_system(tables, whitelist, label):
    """テストB: decide()+ホワイトリストをリプレイし、nat/revバケツ別の的中率。"""
    sig = []  # (nat10, rev10, side, win_bool)
    for hands in tables.values():
        seq = []
        pending = None
        nats = [h["nat"] for h in hands]
        revs = [h["rev"] for h in hands]
        for i, h in enumerate(hands):
            o = h["win"]
            if pending is not None and o in ("P", "B"):
                sig.append((pending[0], pending[1], pending[2], o == pending[2]))
                pending = None
            seq.append(o)
            start = max(0, len(seq) - SEQ_WINDOW)
            start -= start % 6  # 珠盤路の6行グリッド位置を保存
            window = "".join(seq[start:])
            try:
                d = decide(window, next_n=len(window) + 1)
            except Exception:
                continue
            act = str(getattr(d, "action", ""))
            if act not in ("BET_P", "BET_B") or pending is not None:
                continue
            side = "P" if act == "BET_P" else "B"
            pk = f"{d.china_pattern}|{d.big_pattern}|{side}"
            if pk not in whitelist:
                continue
            if i + 1 < NAT_WIN:
                continue
            nat10 = sum(nats[max(0, i - NAT_WIN + 1):i + 1])
            rev10 = sum(revs[max(0, i - NAT_WIN + 1):i + 1])
            pending = (nat10, rev10, side)
    n_all = len(sig)
    w_all = sum(1 for s in sig if s[3])
    out = [f"[B] {label}: 仮想decision {n_all}件 全体的中率 {wr(w_all, n_all):.2f}%"]
    out.append(f"  直近{NAT_WIN}のナチュラル数バケツ別:")
    bk = collections.defaultdict(lambda: [0, 0])
    for nat10, _, _, win in sig:
        b = bucket_nat(nat10)
        bk[b][0] += 1 if win else 0
        bk[b][1] += 1
    for b in ("0-2", "3-4", "5+"):
        w, n = bk[b]
        out.append(f"    nat{b}: {wr(w, n):.2f}% (n={n})")
    lo, hi = bk["0-2"], bk["5+"]
    out.append(f"    z(5+ vs 0-2) = {z_two_prop(hi[0], hi[1], lo[0], lo[1]):+.2f}")
    out.append(f"  直近{NAT_WIN}の3枚目逆転数バケツ別:")
    bk2 = collections.defaultdict(lambda: [0, 0])
    for _, rev10, _, win in sig:
        b = "0" if rev10 == 0 else ("1" if rev10 == 1 else "2+")
        bk2[b][0] += 1 if win else 0
        bk2[b][1] += 1
    for b in ("0", "1", "2+"):
        w, n = bk2[b]
        out.append(f"    rev{b}: {wr(w, n):.2f}% (n={n})")
    lo, hi = bk2["0"], bk2["2+"]
    out.append(f"    z(2+ vs 0) = {z_two_prop(hi[0], hi[1], lo[0], lo[1]):+.2f}")
    return "\n".join(out)


def main():
    tables, score_ok, score_ng = load_tables()
    n_hands = sum(len(v) for v in tables.values())
    n_nat = sum(1 for v in tables.values() for h in v if h["nat"])
    n_rev = sum(1 for v in tables.values() for h in v if h["rev"])
    n_rev9 = sum(1 for v in tables.values() for h in v if h["rev9"])
    print(f"hands={n_hands} tables={len(tables)} nat={wr(n_nat, n_hands):.1f}% "
          f"rev={wr(n_rev, n_hands):.1f}% rev9={wr(n_rev9, n_hands):.1f}%")
    print(f"カード値の自己検証(2枚側スコア一致): OK={score_ok} NG={score_ng}")
    print()
    print(a1_descriptive(tables))
    print()
    print(continuation_test(tables, "実データ"))
    print()
    for s in range(3):
        print(continuation_test(tables, f"対照#{s + 1}", shuffle=True, seed=s + 1))
    print()
    print(replay_system(tables, LIVE_SIGNAL_PATTERNS, "v3(6パターン)"))
    print()
    if LIVE_SIGNAL_PATTERNS_V4:
        print(replay_system(tables, LIVE_SIGNAL_PATTERNS_V4, "v4(10パターン)"))
    else:
        print("[B] v4: LIVE_SIGNAL_PATTERNS_V4 が import できずスキップ")


if __name__ == "__main__":
    main()
