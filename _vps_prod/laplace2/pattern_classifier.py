"""パターン分類器 — bead road から大路罫線パターンを判定

87.5万ハンドの backtest で発見したルーティングテーブルに従い、
シューを以下のいずれかに分類する:
  - "テレコ+ニコ混合"  (★最強, ROI +12〜15%)
  - "テレコ崩れ"        (中位, ROI +0〜+7%)
  - "ニコニコ・ニコイチ"  (BET禁止)
  - "縦流れ"            (Strategy D)
  - "ブリッジ"          (BET禁止)
  - "不規則"            (BET禁止)
  - "偏在"              (BET禁止)
  - "不明"              (シュー序盤、まだ判定不能)

判定は 大路罫線 (P/B/T 文字列を列に分解) ベース。

詳細: PATTERN_STRATEGY_FINDINGS.md 参照
"""
from __future__ import annotations
from typing import List


# ───────────────────────────────────────────────
# 大路罫線の列計算
# ───────────────────────────────────────────────
def compute_big_road_columns(seq: str) -> List[List[str]]:
    """P/B/T 文字列を大路罫線の列リストに変換 (タイは無視)

    Example:
        seq = "BPBBPP"  →  [['B'], ['P'], ['B','B'], ['P','P']]
                            列長:    1     1      2         2
    """
    if not seq:
        return []
    cols: List[List[str]] = []
    current: List[str] = []
    last_side = None
    for ch in seq:
        if ch == 'T':
            continue
        if ch != last_side:
            if current:
                cols.append(current)
            current = [ch]
            last_side = ch
        else:
            current.append(ch)
    if current:
        cols.append(current)
    return cols


def column_lengths(seq: str) -> List[int]:
    """大路罫線の列長リスト"""
    return [len(c) for c in compute_big_road_columns(seq)]


# ───────────────────────────────────────────────
# パターン分類 (5種類 + 不明)
# ───────────────────────────────────────────────
def classify_pattern(seq: str, min_cols: int = 5) -> str:
    """大路罫線からパターンを判定

    backtest (Step 3) の分類と完全一致させる。
    判定順序が重要: 縦流れ優先 → テレコ+ニコ混合 (ROI +12〜15% の最強セル)。

    Returns:
      "テレコ+ニコ混合"  : 1段+2段が80%以上 (★戦略A適用、ROI +12〜15%)
      "テレコ崩れ"        : 上記以外の混合 (戦略D適用、ROI +0〜+7%)
      "縦流れ"            : 5段+ × 3回以上
      "ブリッジ"          : 短列/長列の二極化 (BET禁止)
      "不規則"            : 列長分散が大きい (BET禁止)
      "偏在"              : P/B 比率が 40/60 から外れる (BET禁止)
      "不明"              : 列数 < min_cols
    """
    cols = compute_big_road_columns(seq)
    col_lens = [len(c) for c in cols]
    if len(col_lens) < min_cols:
        return "不明"

    n = len(col_lens)
    pct1 = sum(1 for L in col_lens if L == 1) / n
    pct2 = sum(1 for L in col_lens if L == 2) / n
    n_long = sum(1 for L in col_lens if L >= 5)
    p_count = sum(1 for ch in seq if ch == 'P')
    b_count = sum(1 for ch in seq if ch == 'B')
    total_pb = p_count + b_count
    p_ratio = (p_count / total_pb) if total_pb else 0.5
    mean_len = sum(col_lens) / n
    variance = sum((L - mean_len) ** 2 for L in col_lens) / n
    short_ratio = sum(1 for L in col_lens if L <= 2) / n
    mid_ratio = sum(1 for L in col_lens if 3 <= L <= 4) / n
    long_ratio = sum(1 for L in col_lens if L >= 5) / n

    # 1. 縦流れ (5段以上 × 3回以上)
    if n_long >= 3:
        return "縦流れ"

    # 2. テレコ+ニコ混合 (短列が 80%+)
    if pct1 + pct2 >= 0.80:
        return "テレコ+ニコ混合"

    # 3. ブリッジ (短列 + 長列の二極化)
    if short_ratio >= 0.40 and long_ratio >= 0.20 and mid_ratio < 0.20:
        return "ブリッジ"

    # 4. 不規則 (列長分散が大きい)
    if variance >= 2.0:
        return "不規則"

    # 5. 偏在 (P/B 比率が 40/60 から外れる)
    if p_ratio < 0.40 or p_ratio > 0.60:
        return "偏在"

    # 6. それ以外はテレコ崩れ (戦略D の対象)
    return "テレコ崩れ"


# ───────────────────────────────────────────────
# 規則性スコア (簡易版)
# ───────────────────────────────────────────────
def regularity_score(seq: str) -> float:
    """シューの規則性スコア (0〜100)

    高いほど規則的。短い列が多く分散が小さいほど高スコア。
    既存の Sync mode の reg=100 と概念的に近い。
    """
    cols = compute_big_road_columns(seq)
    if len(cols) < 5:
        return 0.0
    col_lens = [len(c) for c in cols]
    n = len(col_lens)

    # 短列比率 (1段+2段)
    pct12 = sum(1 for L in col_lens if L <= 2) / n

    # 列長の分散 (低いほど規則的)
    mean_len = sum(col_lens) / n
    var = sum((L - mean_len) ** 2 for L in col_lens) / n
    var_penalty = min(var * 10, 50)  # 0-50 ペナルティ

    score = pct12 * 100 - var_penalty
    return max(0.0, min(100.0, score))


# ───────────────────────────────────────────────
# 一括分類
# ───────────────────────────────────────────────
def classify_all(seq: str) -> dict:
    """シューの全特徴量を一括取得

    Returns:
      {
        'pattern': 'テレコ+ニコ混合' / 'テレコ崩れ' / 'ニコニコ・ニコイチ' / '縦流れ' / 'ブリッジ' / '不明',
        'cols': [[B], [P,P], ...],
        'col_lens': [1, 2, ...],
        'n_cols': 25,
        'pct_len1': 0.45,
        'pct_len2': 0.30,
        'max_col_len': 6,
        'n_long_cols': 1,
        'regularity': 78.5,
      }
    """
    cols = compute_big_road_columns(seq)
    col_lens = [len(c) for c in cols]
    n = len(col_lens) if col_lens else 1
    return {
        'pattern': classify_pattern(seq),
        'cols': cols,
        'col_lens': col_lens,
        'n_cols': len(col_lens),
        'pct_len1': sum(1 for L in col_lens if L == 1) / n if col_lens else 0,
        'pct_len2': sum(1 for L in col_lens if L == 2) / n if col_lens else 0,
        'max_col_len': max(col_lens) if col_lens else 0,
        'n_long_cols': sum(1 for L in col_lens if L >= 5),
        'regularity': regularity_score(seq),
    }


# ───────────────────────────────────────────────
# テスト用 main
# ───────────────────────────────────────────────
if __name__ == "__main__":
    # 簡易テスト
    test_cases = [
        ("BPBPBPBPBPBPBPBP", "テレコ+ニコ混合 (純粋テレコ)"),
        ("BPBBPBBPBBPBBPBB", "テレコ+ニコ混合 (1+2段80%+)"),
        ("BBBBBBBBPPPPPPPP", "縦流れ"),
        ("BPBPBPBPBBBBBPBP", "ブリッジ?"),
        ("BBPBBPBBPBBPBBP", "ニコイチ (2-1-2-1)"),
    ]
    for seq, expected in test_cases:
        result = classify_all(seq)
        print(f"seq: {seq}")
        print(f"  expected: {expected}")
        print(f"  pattern:  {result['pattern']}")
        print(f"  col_lens: {result['col_lens']}")
        print(f"  pct1+2:   {result['pct_len1']+result['pct_len2']:.2f}")
        print(f"  max_len:  {result['max_col_len']}")
        print(f"  reg:      {result['regularity']:.1f}")
        print()
