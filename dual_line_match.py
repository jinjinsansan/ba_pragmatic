"""dual_line_match.py — 後方互換ラッパー (実装本体は dual_line_logic.py)

公開 API (後方互換):
    decide(history, next_n) -> Decision
    chinese_road_predict(history, next_n) -> (pred_or_undecided, pattern_name)
    big_road_predict(history) -> (pred_or_undecided, pattern_name)
    score_proximity(history, next_n) -> (score, direction)
"""
from __future__ import annotations
from itertools import product
from typing import Any, Optional, Tuple, Literal

from dual_line_logic import (
    Decision,
    decide,
    predict_bead,
    predict_big_road,
    _bead_row_cells,
)

PredictionType = Literal["P", "B", "undecided"]
ActionType = Literal["BET_P", "BET_B", "LOOK"]

LIVE_SIGNAL_PATTERNS = frozenset({
    "telecho|telecho|B",
    "telecho|nikoichi|P",
    "telecho|niconico|P",
    "niconico|niconico|B",
    "niconico|dragon|P",
    "sansan|telecho|P",
})

# v4: 勝率≥51% かつ $/BET>0 かつ サンプル≥200 の10パターン
# (dual_line_all_patterns_report.html / 全データT込み方式で選定)。
# v3 の niconico|dragon|P(50.5%) は基準未満のため v4 から外れる。
# v3 と別モードとして並走・forward検証する (PLAN_DUAL_LINE_V4_10PATTERN.md)。
# 【2026-06-07 入替】最弱候補 niconico|niconico|B (102k 再BTで 50.0%/-0.0221/Z-0.26)
#   を外し、新bline定義(5個以上・逆1個まで・T無視)で見つかった
#   bline|telecho|B (51.7%/+0.0078/Z+1.68) を投入。v3(6パターン)は無変更。
LIVE_SIGNAL_PATTERNS_V4 = frozenset({
    "sansan|telecho|P",
    "bline|telecho|B",
    "telecho|niconico|P",
    "niconico|nikoichi|B",
    "telecho|niconico|B",
    "niconico|dragon|B",
    "niconico|nikoichi|P",
    "telecho|nikoichi|B",
    "telecho|telecho|B",
    "telecho|nikoichi|P",
})

__all__ = [
    "Decision",
    "decide",
    "chinese_road_predict",
    "big_road_predict",
    "score_proximity",
    "LIVE_SIGNAL_PATTERNS",
    "LIVE_SIGNAL_PATTERNS_V4",
    "live_signal_for_history",
    "live_preposition_for_history",
]


def chinese_road_predict(history: str, next_n: int) -> Tuple[str, Optional[str]]:
    """後方互換: (pred_or_'undecided', pattern_name_or_None)"""
    cells = _bead_row_cells(history, next_n)
    pred, pat = predict_bead(cells)
    return (pred if pred else "undecided"), (pat if pat else None)


def big_road_predict(history: str) -> Tuple[str, Optional[str]]:
    """後方互換: (pred_or_'undecided', pattern_name_or_None)"""
    pred, pat = predict_big_road(history)
    return (pred if pred else "undecided"), (pat if pat else None)


def score_proximity(history: str, next_n: Optional[int] = None) -> tuple[int, str]:
    """テーブル選択用スコア: 0=cold, 1=warm, 2=hot(=BET候補)"""
    if next_n is None:
        next_n = len(history) + 1
    china_pred, _ = chinese_road_predict(history, next_n)
    big_pred, _ = big_road_predict(history)
    if china_pred != "undecided" and big_pred != "undecided" and china_pred == big_pred:
        return (2, china_pred)
    if china_pred != "undecided" or big_pred != "undecided":
        direction = china_pred if china_pred != "undecided" else big_pred
        return (1, direction)
    return (0, "")


def live_signal_for_history(
    history: str, patterns: "frozenset[str]" = LIVE_SIGNAL_PATTERNS
) -> Optional[dict[str, str]]:
    """Return an actionable signal only when it is one of the live patterns.

    ``patterns`` defaults to the v3 six-pattern whitelist so all existing
    callers (which pass only ``history``) keep the exact v3 behaviour. Pass
    ``LIVE_SIGNAL_PATTERNS_V4`` to evaluate the v4 ten-pattern whitelist.
    """
    seq = "".join(c for c in str(history or "") if c in ("P", "B"))
    d = decide(seq, len(seq) + 1)
    if d.action == "LOOK":
        return None
    side = str(d.bet_side or ("P" if d.action == "BET_P" else "B"))
    pattern_key = f"{d.china_pattern}|{d.big_pattern}|{side}"
    if pattern_key not in patterns:
        return None
    return {
        "pattern_key": pattern_key,
        "side": side,
        "china_pattern": str(d.china_pattern or ""),
        "big_pattern": str(d.big_pattern or ""),
    }


def live_preposition_for_history(
    history: str, patterns: "frozenset[str]" = LIVE_SIGNAL_PATTERNS
) -> dict[str, Any]:
    """Find only one/two-hand forecasts that can become a live signal.

    One-hand forecasts take precedence over two-hand forecasts. Multiple future
    outcomes are kept as candidates; a side is exposed only when all candidates
    resolve to the same bet side.

    ``patterns`` defaults to the v3 six-pattern whitelist (callers passing only
    ``history`` are unchanged). Pass ``LIVE_SIGNAL_PATTERNS_V4`` for v4 forecasts.
    """
    seq = "".join(c for c in str(history or "") if c in ("P", "B"))
    if live_signal_for_history(seq, patterns) is not None:
        return {"score": 0, "steps_before": 0, "side": "", "candidates": [], "pattern_keys": []}
    for steps_before in (1, 2):
        candidates: dict[tuple[str, str], dict[str, str]] = {}
        for suffix_tuple in product("PB", repeat=steps_before):
            suffix = "".join(suffix_tuple)
            signal = live_signal_for_history(seq + suffix, patterns)
            if signal is None:
                continue
            key = (signal["pattern_key"], signal["side"])
            candidate = dict(signal)
            candidate["outcome_suffix"] = suffix
            candidates.setdefault(key, candidate)
        if not candidates:
            continue
        rows = list(candidates.values())
        sides = {row["side"] for row in rows}
        return {
            "score": 2 if steps_before == 1 else 1,
            "steps_before": steps_before,
            "side": next(iter(sides)) if len(sides) == 1 else "",
            "candidates": rows,
            "pattern_keys": sorted({row["pattern_key"] for row in rows}),
        }
    return {"score": 0, "steps_before": 0, "side": "", "candidates": [], "pattern_keys": []}
