#!/usr/bin/env python3
"""曜日方向ルール(火水木=順張り/金土日=逆張り)の精査 (2026-07-16) — VPS上で実行。

_bt_ogawa_stopstart.py の結果「複合ルールの効果は実質DOW成分のみ」を受けた追撃:
  1. DOW方向 常時ON のベースライン(状態機械なし)
  2. T除外バグ(6/30修正)を踏まえ、クリーン期間(7/01〜)限定の再計算
  3. ISO週ごとの 火水木vs金土日 ギャップ(安定性)
  4. 日単位クラスタ補正(各暦日を1標本とするt検定相当) ← 47日=各曜日約7回しかないため
  5. 手数料込みPnL(順張りでB賭け/逆張りのB signal=P賭けは手数料なし)
"""
import re
import sys
import time
import collections
import math

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
RE_SETTLE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*\[v4-settle\] posted settled did=(\S+) "
    r"result=(WIN|LOSE|TIE)")
RE_PUB = re.compile(r"^\d{4}.*\[v4-publish\] accepted did=(\S+) table=\S+ pattern=\S+\|([PB])")


def load():
    did_side = {}
    out = []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        m = RE_PUB.match(line)
        if m:
            did_side[m.group(1)] = m.group(2)
            continue
        m = RE_SETTLE.match(line)
        if not m or m.group(3) == "TIE":
            continue
        st = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        out.append({"date": m.group(1)[:10], "dow": st.tm_wday,
                    "iso": time.strftime("%G-W%V", st),
                    "win": m.group(3) == "WIN",
                    "side": did_side.get(m.group(2))})  # シグナルの賭け先(P/B)
    return out


def dow_mode(dow, prev):
    if dow in (1, 2, 3):
        return "jun"
    if dow in (4, 5, 6):
        return "gyaku"
    return prev  # 月=維持(日曜からの持ち越し=gyaku)


def evaluate(stream, label):
    prev = "jun"
    n = w = 0
    pnl = 0.0
    pnl_n = 0
    day_wr = collections.defaultdict(lambda: [0, 0, None])  # date -> [w, n, group]
    for s in stream:
        mode = dow_mode(s["dow"], prev)
        prev = mode
        mywin = s["win"] == (mode == "jun")
        n += 1
        w += 1 if mywin else 0
        grp = ("TWT" if s["dow"] in (1, 2, 3) else
               ("FSS" if s["dow"] in (4, 5, 6) else "MON"))
        d = day_wr[s["date"]]
        d[0] += 1 if mywin else 0
        d[1] += 1
        d[2] = grp
        # 手数料込みPnL: 実際に賭ける側 = jun→signal side / gyaku→反対側
        if s["side"]:
            bet_side = s["side"] if mode == "jun" else ("P" if s["side"] == "B" else "B")
            if mywin:
                pnl += 0.95 if bet_side == "B" else 1.0
            else:
                pnl -= 1.0
            pnl_n += 1
    se = (0.25 / n) ** 0.5 if n else 1
    print(f"[{label}] DOW方向常時ON: 勝率{w/n*100:.2f}% n={n} z(50%比)={(w/n-0.5)/se:+.2f}")
    print(f"  手数料込みPnL: {pnl:+.1f}u / {pnl_n}BET = 1BETあたり{pnl/pnl_n*100:+.2f}%" if pnl_n else "  side不明")
    # 日単位クラスタ: 各暦日の勝率を1標本に
    days = [(d[0] / d[1], d[2], d[1]) for d in day_wr.values() if d[1] >= 30]
    for grp in ("TWT", "FSS", "MON"):
        g = [x[0] for x in days if x[1] == grp]
        if len(g) >= 2:
            mu = sum(g) / len(g)
            sd = (sum((x - mu) ** 2 for x in g) / (len(g) - 1)) ** 0.5
            t = (mu - 0.5) / (sd / len(g) ** 0.5) if sd else 0
            gname = {"TWT": "火水木(順)", "FSS": "金土日(逆)", "MON": "月(維持)"}[grp]
            print(f"  日単位 {gname}: 平均{mu*100:.2f}% ±{sd*100:.2f} 日数{len(g)} t={t:+.2f}")
    print()


def main():
    stream = load()
    print(f"全期間: {len(stream)}件 ({stream[0]['date']} 〜 {stream[-1]['date']})")
    sides = sum(1 for s in stream if s["side"])
    print(f"side突合率: {sides}/{len(stream)}")
    print()
    evaluate(stream, "全期間 5/30〜")
    clean = [s for s in stream if s["date"] >= "2026-07-01"]
    evaluate(clean, "クリーン期間 7/01〜 (T除外バグ修正後)")
    buggy = [s for s in stream if s["date"] < "2026-07-01"]
    evaluate(buggy, "バグ期間 5/30〜6/30 (参考)")

    # ISO週ごとの 火水木(順枠) vs 金土日(逆枠) ギャップ
    print("[週別] 火水木の順勝率 / 金土日の逆勝率(=100-順) / 週ギャップ(順-逆で見た素材差):")
    wk = collections.defaultdict(lambda: {"tw": [0, 0], "fs": [0, 0]})
    for s in stream:
        if s["dow"] in (1, 2, 3):
            wk[s["iso"]]["tw"][0] += 1 if s["win"] else 0
            wk[s["iso"]]["tw"][1] += 1
        elif s["dow"] in (4, 5, 6):
            wk[s["iso"]]["fs"][0] += 1 if s["win"] else 0
            wk[s["iso"]]["fs"][1] += 1
    for k in sorted(wk):
        g = wk[k]
        tw = g["tw"][0] / g["tw"][1] * 100 if g["tw"][1] else float("nan")
        fs = g["fs"][0] / g["fs"][1] * 100 if g["fs"][1] else float("nan")
        tag = " ←バグ期間含む" if k < "2026-W27" else ""
        print(f"  {k}: 火水木順 {tw:.1f}%({g['tw'][1]}) 金土日順 {fs:.1f}%({g['fs'][1]}) "
              f"ギャップ {tw-fs:+.1f}pt{tag}")


if __name__ == "__main__":
    main()
