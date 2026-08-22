"""Realtime Regularity Monitor

バカラシューの規則性をリアルタイムで計算。
shoe.py の規則性計算ロジックを独立モジュールとして抽出。
Sync モード（find_sync_table + 動的監視）で使用。

閾値:
  ENTRY_THRESHOLD = 70  -- 入場判断（20ハンド以上で reg>=70）
  EXIT_THRESHOLD = 65   -- 退避判断（reg<65 で即退避）
"""
import statistics
import logging

logger = logging.getLogger("baccarat.regularity")

ENTRY_THRESHOLD = 70
EXIT_THRESHOLD = 65
MIN_HANDS_FOR_ENTRY = 35  # シュー約50%経過、精度87.7%
MAX_HANDS_FOR_ENTRY = 55  # シュー終盤すぎを排除 (残り5-15ハンド = BET時間不足)
                          # 通常シュー60-75ハンド → 入場後最低10ハンドの BET 時間確保
CHECK_INTERVAL = 5  # BET中に5ハンドごとに再計算

# P/B バランス閾値 — Banker寄り過ぎるシューを除外
# Player をBETする前提なので、P/(P+B) が一定以上必要
MIN_P_RATIO_FOR_ENTRY = 0.42   # 入場時: P比率42%以上 (B58%以下)
MIN_P_RATIO_FOR_STAY = 0.38    # 滞在時: P比率38%未満で退避 (B62%以上)


def compute_streaks(results) -> list[dict]:
    """結果列からP/B連続を抽出(タイ除外)
    Input: "PPBBPBT..." or ['P','P','B',...]
    Returns: [{"type": "P", "len": 2}, {"type": "B", "len": 2}, ...]
    """
    if isinstance(results, str):
        results = list(results)
    streaks = []
    current_type = ""
    current_count = 0
    for r in results:
        if r == "T":
            continue
        if r == current_type:
            current_count += 1
        else:
            if current_type and current_count > 0:
                streaks.append({"type": current_type, "len": current_count})
            current_type = r
            current_count = 1
    if current_type and current_count > 0:
        streaks.append({"type": current_type, "len": current_count})
    return streaks


def detect_repeating_pattern(lengths: list[int]) -> float:
    """連続長の繰り返しパターン検出 (1,2,1,2 etc.)"""
    if len(lengths) < 6:
        return 0
    best = 0
    for period in (2, 3, 4):
        if len(lengths) < period * 2:
            continue
        matches = 0
        total = 0
        for i in range(period, len(lengths)):
            total += 1
            if lengths[i] == lengths[i - period]:
                matches += 1
        ratio = matches / total if total > 0 else 0
        if ratio > 0.6:
            best = max(best, 15)
        elif ratio > 0.4:
            best = max(best, 8)
    return best


def compute_regularity(results) -> float:
    """規則性スコア計算 (0-100)
    高い = 規則的 = 〇✖ロジックに有利
    """
    streaks = compute_streaks(results)
    if len(streaks) < 5:
        return 0.0

    lengths = [s["len"] for s in streaks]
    score = 50.0

    # 1. 連続長の分散 - 低いほど規則的
    var = statistics.variance(lengths) if len(lengths) > 1 else 0
    if var < 0.5:
        score += 25
    elif var < 1.5:
        score += 15
    elif var < 3.0:
        score += 5
    elif var > 6.0:
        score -= 15
    elif var > 4.0:
        score -= 5

    # 2. 支配的パターン割合
    len_counts = {}
    for ln in lengths:
        bucket = min(ln, 5)
        len_counts[bucket] = len_counts.get(bucket, 0) + 1
    max_ratio = max(len_counts.values()) / len(lengths)
    if max_ratio > 0.6:
        score += 20
    elif max_ratio > 0.45:
        score += 10
    elif max_ratio < 0.25:
        score -= 10

    # 3. 繰り返しパターン検出
    score += detect_repeating_pattern(lengths)

    # 4. 3落ライン一貫性
    below3 = sum(1 for ln in lengths if ln <= 3)
    ratio = below3 / len(lengths)
    if ratio > 0.85 or ratio < 0.15:
        score += 10
    elif ratio > 0.7 or ratio < 0.3:
        score += 5

    return max(0.0, min(100.0, score))


def count_non_tie(results) -> int:
    """タイ除外のハンド数"""
    if isinstance(results, str):
        return sum(1 for c in results if c in ('P', 'B'))
    return sum(1 for c in results if c in ('P', 'B'))


def should_enter_table(results, threshold: float = ENTRY_THRESHOLD) -> bool:
    """入場判断: ハンド数>=20 かつ reg>=70"""
    if count_non_tie(results) < MIN_HANDS_FOR_ENTRY:
        return False
    return compute_regularity(results) >= threshold


def should_exit_table(results, threshold: float = EXIT_THRESHOLD) -> bool:
    """退避判断: reg<65 なら退避"""
    if count_non_tie(results) < MIN_HANDS_FOR_ENTRY:
        return False  # データ不足時は継続
    return compute_regularity(results) < threshold


def count_pb(results) -> tuple[int, int]:
    """Player/Banker をカウント (Tie除外)"""
    if isinstance(results, str):
        results = list(results)
    p = sum(1 for r in results if r == 'P')
    b = sum(1 for r in results if r == 'B')
    return p, b


def evaluate_table(results) -> dict:
    """テーブル評価の統合関数
    Returns: {
        'regularity': float,
        'hands': int,
        'p_ratio': float,
        'p_count': int,
        'b_count': int,
        'can_enter': bool,
        'should_exit': bool,
        'exit_reason': str,
    }

    入場条件 (can_enter):
      1. ハンド数 >= MIN_HANDS_FOR_ENTRY
      2. 規則性 >= ENTRY_THRESHOLD
      3. P比率 >= MIN_P_RATIO_FOR_ENTRY (Banker dominant shoe を除外)

    退避条件 (should_exit):
      1. ハンド数 < MIN_HANDS_FOR_ENTRY → シュー切替直後、即退避
      2. 規則性 < EXIT_THRESHOLD → 規則性崩壊、即退避
      3. P比率 < MIN_P_RATIO_FOR_STAY → Banker dominant化、即退避
    """
    hands = count_non_tie(results)
    reg = compute_regularity(results) if hands >= 5 else 0.0
    p, b = count_pb(results)
    p_ratio = p / (p + b) if (p + b) > 0 else 0.5

    can_enter = (
        hands >= MIN_HANDS_FOR_ENTRY
        and reg >= ENTRY_THRESHOLD
        and p_ratio >= MIN_P_RATIO_FOR_ENTRY
    )

    exit_reason = ""
    should_exit = False
    if hands < MIN_HANDS_FOR_ENTRY:
        should_exit = True
        exit_reason = "シュー切替"
    elif reg < EXIT_THRESHOLD:
        should_exit = True
        exit_reason = "規則性崩壊"
    elif p_ratio < MIN_P_RATIO_FOR_STAY:
        should_exit = True
        exit_reason = f"Banker dominant (P={p_ratio:.0%})"

    return {
        'regularity': reg,
        'hands': hands,
        'p_ratio': p_ratio,
        'p_count': p,
        'b_count': b,
        'can_enter': can_enter,
        'should_exit': should_exit,
        'exit_reason': exit_reason,
    }


def raw_history_to_results(raw: list) -> list[str]:
    """scraper._evo_table_raw_histories 形式を P/B/T 配列に変換"""
    results = []
    for entry in raw:
        if isinstance(entry, dict):
            c = entry.get("c", "")
            ties = entry.get("ties", 0)
            if c == "R":
                results.append("P")
            elif c == "B":
                results.append("B")
            if ties:
                results.extend(["T"] * ties)
        elif isinstance(entry, str):
            if entry in ('P', 'B', 'T'):
                results.append(entry)
    return results
