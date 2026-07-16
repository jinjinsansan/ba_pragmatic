#!/usr/bin/env python3
"""小川/sara式 停止開始ルールのバックテスト (2026-07-16) — VPS上で実行。

ユーザー確認済みの翻訳(2026-07-16):
  ○×列 = VPS監視botのv4(10パターン)決済ストリーム([v4-settle]行・TIE除外)
  部品1 方向: 累積勝率の全期間レンジ内位置 p で切替
        p<=1/3 → 順張り / p>=2/3 → 逆張り / 中間 → 直前モード維持
        + 火水木=順張り / 金土日=逆張り / 月=維持 (DOW理論)
        D ∈ {range, dow, both(一致時のみ参戦)}
  部品2 状態機械: 停止中にシグナル×がk連続→参戦 / 参戦中に自分がm連勝→停止
        k=m=5が本人体感。k,m∈{3,5,7}をスイープ(多重比較は前後半分割で担保)
  彼の勝敗 = (シグナルWIN and 順張り) or (シグナルLOSE and 逆張り)

追加の部品単体テスト:
  - レンジ位置デシル → 次決済の勝率(オシレーターに予測力があるか)
  - 曜日別勝率(火水木/金土日理論の直接検定)
"""
import re
import sys
import time
import collections

LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
BURN_IN = 300  # 累積%が安定するまでレンジ計算から除外
RE_SETTLE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*\[v4-settle\] posted settled did=\S+ "
    r"result=(WIN|LOSE|TIE)")


def load_stream():
    out = []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        m = RE_SETTLE.match(line)
        if not m or m.group(2) == "TIE":
            continue
        st = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        out.append({"ts": m.group(1), "dow": st.tm_wday, "win": m.group(2) == "WIN"})
    return out


def enrich(stream):
    """累積%・全期間レンジ位置(因果的=その時点までのmin/max)を付与。"""
    w = l = 0
    rmin, rmax = None, None
    for i, s in enumerate(stream):
        # 位置はこの決済を見る「前」の値(=BET判断時に既知)
        c = w / (w + l) if (w + l) else 0.5
        s["cum"] = c
        if i >= BURN_IN:
            rmin = c if rmin is None else min(rmin, c)
            rmax = c if rmax is None else max(rmax, c)
        s["pos"] = ((c - rmin) / (rmax - rmin)
                    if (rmin is not None and rmax is not None and rmax > rmin) else None)
        if s["win"]:
            w += 1
        else:
            l += 1
    return stream


def direction(s, prev, rule):
    """BET時点のモード。jun=順張り / gyaku=逆張り / None=参戦禁止(bothの不一致)。"""
    rng = prev
    if s["pos"] is not None:
        if s["pos"] <= 1 / 3:
            rng = "jun"
        elif s["pos"] >= 2 / 3:
            rng = "gyaku"
    dow = prev
    if s["dow"] in (1, 2, 3):      # 火水木
        dow = "jun"
    elif s["dow"] in (4, 5, 6):    # 金土日
        dow = "gyaku"
    if rule == "range":
        return rng
    if rule == "dow":
        return dow
    return rng if rng == dow else None  # both: 一致時のみ


def run(stream, rule, k, m, lo=0, hi=None):
    hi = hi if hi is not None else len(stream)
    on = False
    sig_l_streak = 0
    my_w_streak = 0
    prev_mode = "jun"
    bets = w = 0
    jun_bets = 0
    for s in stream[lo:hi]:
        mode = direction(s, prev_mode, rule)
        if mode:
            prev_mode = mode
        if on and mode:
            mywin = s["win"] == (mode == "jun")
            bets += 1
            w += 1 if mywin else 0
            jun_bets += 1 if mode == "jun" else 0
            my_w_streak = my_w_streak + 1 if mywin else 0
            if my_w_streak >= m:
                on = False
                my_w_streak = 0
                sig_l_streak = 0
        # シグナル×連カウント(参戦判定・OFF中もON中も列は見えている)
        sig_l_streak = 0 if s["win"] else sig_l_streak + 1
        if not on and sig_l_streak >= k:
            on = True
            my_w_streak = 0
    return bets, w, jun_bets


def z_vs(wins, n, p0):
    if not n:
        return 0.0
    se = (p0 * (1 - p0) / n) ** 0.5
    return (wins / n - p0) / se


def main():
    stream = enrich(load_stream())
    n = len(stream)
    base_w = sum(s["win"] for s in stream)
    print(f"v4決済ストリーム: {n}件 ({stream[0]['ts']} 〜 {stream[-1]['ts']})")
    print(f"打ちっ放し順張り基準: {base_w/n*100:.2f}%")
    cums = [s["cum"] for s in stream[BURN_IN:]]
    print(f"累積%のレンジ(burn-in {BURN_IN}以降): {min(cums)*100:.2f}% 〜 {max(cums)*100:.2f}%")
    print()

    # 部品単体1: レンジ位置 → 次決済勝率
    print("[部品1] レンジ位置5分位 → その決済の勝率(位置はBET時点で既知):")
    q = collections.defaultdict(lambda: [0, 0])
    for s in stream:
        if s["pos"] is None:
            continue
        b = min(4, int(s["pos"] * 5))
        q[b][0] += 1 if s["win"] else 0
        q[b][1] += 1
    for b in range(5):
        wq, nq = q[b]
        lab = ["0-20%(下限側)", "20-40%", "40-60%", "60-80%", "80-100%(上限側)"][b]
        print(f"  {lab}: {wq/nq*100 if nq else 0:.2f}% (n={nq})")
    print()

    # 部品単体2: 曜日別勝率
    print("[部品2] 曜日別勝率(月火水木金土日):")
    dw = collections.defaultdict(lambda: [0, 0])
    for s in stream:
        dw[s["dow"]][0] += 1 if s["win"] else 0
        dw[s["dow"]][1] += 1
    for d, name in enumerate("月火水木金土日"):
        wq, nq = dw[d]
        print(f"  {name}: {wq/nq*100 if nq else 0:.2f}% (n={nq})")
    print()

    # 複合状態機械スイープ
    half = n // 2
    print("[複合] 状態機械スイープ (勝率=彼の枠・zは50%比・()内=前半/後半):")
    for rule in ("range", "dow", "both"):
        for k in (3, 5, 7):
            for m in (3, 5, 7):
                b, w, jb = run(stream, rule, k, m)
                b1, w1, _ = run(stream, rule, k, m, 0, half)
                b2, w2, _ = run(stream, rule, k, m, half, n)
                mark = " ★主設定" if (k == 5 and m == 5) else ""
                print(f"  {rule:5s} k={k} m={m}: {w/b*100 if b else 0:.2f}% n={b} "
                      f"z={z_vs(w, b, 0.5):+.2f} 順比率{jb/b*100 if b else 0:.0f}% "
                      f"(前半{w1/b1*100 if b1 else 0:.1f}%/{b1} "
                      f"後半{w2/b2*100 if b2 else 0:.1f}%/{b2}){mark}")
        print()


if __name__ == "__main__":
    main()
