#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 曜日別・週別 順方向/逆方向 勝率レポート
#   源  : /opt/laplace2/pattern_prereg_history.csv
#          (既存の pattern_prereg_watch cron が毎日 ~09:05 に累計W/Lスナップショットを追記)
#   方法: 同一(system,pattern)を日付順に並べ、連続日の累計を差分→その日のW/L。
#         差分は「後スナップショットの前日」に帰属(活動は主に前日昼〜夜・~9時境界)。
#         曜日/ISO週に集計。逆張り勝率 = 順方向の負け率(反転はB<->Pのみ・Tは不変)。
#   注意: これは記述統計(過去の傾向)であり予測ではない。日内自己相関≈0。
#         セルnが小さいので単独日の数字はノイズ。曜日は事前判明ゆえスケジュール化のみ意味を持つ。
import csv, sys, os
from datetime import date
from collections import defaultdict

CSV = os.environ.get("PREREG_CSV", "/opt/laplace2/pattern_prereg_history.csv")
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DOW_JP = {"Mon": "月", "Tue": "火", "Wed": "水", "Thu": "木", "Fri": "金", "Sat": "土", "Sun": "日"}


def load_rows():
    rows = []
    with open(CSV, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append((r["date"].strip(), r["system"].strip(),
                             r["pattern"].strip(), int(r["wins"]), int(r["losses"])))
            except Exception:
                continue
    return rows


def daily_deltas(rows):
    series = defaultdict(list)                      # (system,pattern) -> [(date,w,l)]
    for d, s, p, w, l in rows:
        series[(s, p)].append((d, w, l))
    day = defaultdict(lambda: {"w": 0, "l": 0})     # (date,system) -> {w,l}
    for (s, p), lst in series.items():
        lst.sort()
        for i in range(1, len(lst)):
            d0, w0, l0 = lst[i - 1]
            d1, w1, l1 = lst[i]
            dw, dl = w1 - w0, l1 - l0
            if dw < 0 or dl < 0:                    # counter reset → skip
                continue
            day[(d0, s)]["w"] += dw
            day[(d0, s)]["l"] += dl
    return day


def to_date(s):
    y, m, d = map(int, s.split("-"))
    return date(y, m, d)


def pct(w, l):
    n = w + l
    return (100.0 * w / n) if n else 0.0


def main():
    rows = load_rows()
    if not rows:
        print("NO DATA")
        return
    day = daily_deltas(rows)
    dates = sorted({d for (d, s) in day.keys()})
    span = f"{dates[0]}〜{dates[-1]}" if dates else "-"

    # ---- 曜日別 集計 ----
    dow_agg = {d: {"v3": {"w": 0, "l": 0}, "v4": {"w": 0, "l": 0}} for d in DOW}
    wk_agg = defaultdict(lambda: {"v3": {"w": 0, "l": 0}, "v4": {"w": 0, "l": 0}})
    for (d, s), c in day.items():
        if s not in ("v3", "v4"):
            continue
        dt = to_date(d)
        dname = DOW[dt.isoweekday() - 1]
        dow_agg[dname][s]["w"] += c["w"]
        dow_agg[dname][s]["l"] += c["l"]
        iso = dt.isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"
        wk_agg[wk][s]["w"] += c["w"]
        wk_agg[wk][s]["l"] += c["l"]

    print(f"# 曜日別・週別 順方向/逆方向 勝率  (源=pattern_prereg_history.csv 差分 / 期間 {span} / JST日付)")
    print("順方向=パターン側に賭け  逆方向=反転(逆張り)=順の負け率  ※記述統計・予測でない・日内自己相関≈0\n")

    for label, key in (("v3 (6パターン)", "v3"), ("v4 (10パターン)", "v4")):
        print(f"## {label}")
        print("曜日 | n(W+L) | 順勝率 | 逆勝率")
        print("---|---|---|---")
        tw = tl = 0
        for d in DOW:
            c = dow_agg[d][key]
            w, l = c["w"], c["l"]
            tw += w; tl += l
            print(f"{DOW_JP[d]} | {w + l} | {pct(w, l):.1f}% | {pct(l, w):.1f}%")
        print(f"**計** | {tw + tl} | {pct(tw, tl):.1f}% | {pct(tl, tw):.1f}%\n")

    # ---- 週末 vs 平日 ----
    print("## 週末(土日) vs 平日(月〜金)  順勝率")
    print("区分 | system | n | 順勝率 | 逆勝率")
    print("---|---|---|---|---")
    for key in ("v3", "v4"):
        for grpname, days in (("平日", DOW[:5]), ("週末", DOW[5:])):
            w = sum(dow_agg[d][key]["w"] for d in days)
            l = sum(dow_agg[d][key]["l"] for d in days)
            print(f"{grpname} | {key} | {w + l} | {pct(w, l):.1f}% | {pct(l, w):.1f}%")

    # ---- 週ごと (week-over-week) ----
    print("\n## 週ごと (v3+v4 合算 順勝率)")
    print("週 | n | 順勝率 | 逆勝率")
    print("---|---|---|---")
    for wk in sorted(wk_agg.keys()):
        w = wk_agg[wk]["v3"]["w"] + wk_agg[wk]["v4"]["w"]
        l = wk_agg[wk]["v3"]["l"] + wk_agg[wk]["v4"]["l"]
        print(f"{wk} | {w + l} | {pct(w, l):.1f}% | {pct(l, w):.1f}%")


if __name__ == "__main__":
    main()
