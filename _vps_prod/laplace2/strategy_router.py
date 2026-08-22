"""戦略ルーター — パターンに応じて BET 判定を切り替える

backtest (Step 3) で発見したルーティングテーブルに基づき、
現在のシュー状態から「今 BET すべきか」「BET なら P/B どちらか」を返す。

戦略は全て Player BET 専用 (現バカラ bot の制約)。

ルーティング:
  テレコ+ニコ混合  → Strategy A  (B→P chase, P→skip, 3連B観戦)  ★ROI +12〜15%
  テレコ崩れ       → Strategy D  (2連P後にPに乗る)             ROI +0〜+7%
  縦流れ           → Strategy D
  ブリッジ         → BET禁止
  不規則           → BET禁止
  偏在             → BET禁止
  不明             → BET禁止 (シュー序盤)
  ※ B-lead (B−P) による死亡ゾーン/戦略切替は decide_bet_blead を参照

詳細: PATTERN_STRATEGY_FINDINGS.md
"""
from __future__ import annotations
from typing import Optional


# ─────────────────────────────────────────────
# パターン → 戦略 ルーティングテーブル
# ─────────────────────────────────────────────
ROUTING_TABLE = {
    "テレコ+ニコ混合": "A_b2_obs3",  # BB後のみBET / BBB観戦
    "テレコ崩れ":      "A_b2_obs4",  # BB後のみBET / BBBB観戦
    "縦流れ":          "D",          # 縦流れは Strategy D を適用
    "ブリッジ":        None,         # BET禁止
    "不規則":          None,         # BET禁止
    "偏在":            None,         # BET禁止
    "不明":            None,         # シュー序盤
    "ニコニコ・ニコイチ": None,       # 全戦略負け
}


def get_strategy_for_pattern(pattern: str) -> Optional[str]:
    """パターン名から戦略名を取得 (None なら BET 禁止)"""
    return ROUTING_TABLE.get(pattern)


def compute_b_lead(seq: str) -> tuple[int, int, int]:
    """B-lead (B - P) を計算"""
    p = sum(1 for ch in seq if ch == 'P')
    b = sum(1 for ch in seq if ch == 'B')
    return b - p, p, b


# ─────────────────────────────────────────────
# 戦略ロジック (各戦略の BET 判定)
# ─────────────────────────────────────────────
def decide_bet_strategy_a(
    seq: str,
    observe_b: int = 3,
    bet_on_b_min: int = 1,
) -> tuple[Optional[str], str]:
    """Strategy A: B→P chase / P→skip / 連続Bで観戦

    現在の seq から「次の手」を BET するか判定。
    Returns:
      (bet_side, reason)
        bet_side: 'P' なら BET P、None なら SKIP
        reason: 判定理由 (ログ用)
    """
    # タイ無視で直近の状態を計算
    last_nt = None
    consec_b = 0
    observing = False

    for ch in seq:
        if ch == 'B':
            consec_b += 1
            last_nt = 'B'
            if consec_b >= 3:
                observing = True
        elif ch == 'P':
            consec_b = 0
            last_nt = 'P'
            if observing:
                observing = False  # P出現で観戦解除

    # 次の手の判定
    if observing:
        return None, f"観戦中 (連続B={consec_b})"
    if last_nt == 'B' and consec_b >= bet_on_b_min:
        return 'P', f"連続B={consec_b} → P狙い"
    if last_nt == 'P':
        return None, f"前手P → SKIP"
    return None, "判定不能 (空 or T のみ)"


def decide_bet_strategy_d(seq: str) -> tuple[Optional[str], str]:
    """Strategy D: 2連P後にPに乗る (P streak rider)

    Returns:
      (bet_side, reason)
    """
    consec_p = 0
    for ch in seq:
        if ch == 'P':
            consec_p += 1
        elif ch == 'B':
            consec_p = 0
        # T は維持

    if consec_p >= 2:
        return 'P', f"連続P={consec_p} → P rider"
    return None, f"連続P={consec_p} (<2) → SKIP"


# ─────────────────────────────────────────────
# 統合 API
# ─────────────────────────────────────────────
def decide_bet(pattern: str, seq: str) -> tuple[Optional[str], str, str]:
    """パターンと現在の seq から BET 判定を返す

    Args:
      pattern: classify_pattern() の結果
      seq: 現在の bead road (P/B/T 文字列)

    Returns:
      (bet_side, strategy_name, reason)
        bet_side: 'P' / 'B' / None
        strategy_name: 'A' / 'D' / None
        reason: 判定理由
    """
    strategy = get_strategy_for_pattern(pattern)
    if strategy is None:
        return None, "none", f"パターン '{pattern}' は BET 禁止"

    if strategy == "A":
        side, reason = decide_bet_strategy_a(seq)
        return side, "A", reason
    if strategy == "A_b2_obs3":
        side, reason = decide_bet_strategy_a(seq, observe_b=3, bet_on_b_min=2)
        return side, "A_b2_obs3", reason
    if strategy == "A_b2_obs4":
        side, reason = decide_bet_strategy_a(seq, observe_b=4, bet_on_b_min=2)
        return side, "A_b2_obs4", reason
    if strategy == "D":
        side, reason = decide_bet_strategy_d(seq)
        return side, "D", reason

    return None, strategy, f"戦略 '{strategy}' は未実装"


def _select_strategy_by_blead(pattern: str, b_lead: int) -> tuple[Optional[str], str]:
    """B-lead による戦略選択 (Banker BET は行わない)"""
    if pattern in ("ブリッジ", "ニコニコ・ニコイチ", "不規則", "偏在", "不明"):
        return None, f"パターン '{pattern}' は BET 禁止"

    if b_lead >= 0:
        if b_lead <= 5:
            return None, f"B-lead={b_lead} (死亡ゾーン)"
        return None, f"B-lead={b_lead} (Banker dominant)"

    # P-dominant: B-lead < 0
    # ★ 縦流れは B-lead に関係なく常に D (Strategy D = 2連P後 P rider)
    # 旧コード: pattern == "縦流れ" and b_lead <= -10 → b_lead -9〜0 で A_b2_obs3 にフォールバックするバグ
    if pattern == "縦流れ":
        return "D", f"B-lead={b_lead} 縦流れ → D"
    if pattern == "テレコ+ニコ混合":
        return "A_b2_obs3", f"B-lead={b_lead} テレコ+ニコ混合"
    if b_lead <= -20:
        return "A", f"B-lead={b_lead} 極端P優勢 → A"
    if b_lead <= -15:
        return "A_b2_obs3", f"B-lead={b_lead} P優勢 → A_b2_obs3"
    if b_lead <= -10:
        return "A", f"B-lead={b_lead} P優勢 → A"
    return "A_b2_obs3", f"B-lead={b_lead} P優勢 → A_b2_obs3"


def decide_bet_blead(pattern: str, seq: str) -> tuple[Optional[str], str, str]:
    """パターン + B-lead から BET 判定を返す"""
    b_lead, _, _ = compute_b_lead(seq)
    strategy, reason = _select_strategy_by_blead(pattern, b_lead)
    if strategy is None:
        return None, "none", reason

    if strategy == "A":
        side, detail = decide_bet_strategy_a(seq)
        return side, "A", detail
    if strategy == "A_b2_obs3":
        side, detail = decide_bet_strategy_a(seq, observe_b=3, bet_on_b_min=2)
        return side, "A_b2_obs3", detail
    if strategy == "A_b2_obs4":
        side, detail = decide_bet_strategy_a(seq, observe_b=4, bet_on_b_min=2)
        return side, "A_b2_obs4", detail
    if strategy == "D":
        side, detail = decide_bet_strategy_d(seq)
        return side, "D", detail

    return None, strategy, f"戦略 '{strategy}' は未実装"


# ─────────────────────────────────────────────
# テスト用 main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    test_cases = [
        ("テレコ+ニコ混合", "BPBPBPBPBP",       "前手P → SKIP"),
        ("テレコ+ニコ混合", "BPBPBPBPBPB",      "前手B → P狙い"),
        ("テレコ+ニコ混合", "BPBPBPBPBBB",      "観戦中 (3連B)"),
        ("テレコ崩れ",      "PPBPPB",          "連続P=0 → SKIP"),
        ("テレコ崩れ",      "BBPPB",           "連続P=0 → SKIP"),
        ("テレコ崩れ",      "BBPP",            "連続P=2 → P rider"),
        ("縦流れ",         "BBBBBBPP",        "連続P=0 → SKIP"),
        ("不明",           "BPB",             "BET禁止"),
    ]

    for pat, seq, expected in test_cases:
        side, strat, reason = decide_bet(pat, seq)
        print(f"pattern={pat:<15} seq={seq:<15} → bet={side} strat={strat} reason={reason}")
        print(f"  expected: {expected}")
        print()
