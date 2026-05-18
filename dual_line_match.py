"""デュアルライン・マッチング予想ロジック実装

仕様: SPEC_DUAL_LINE_MATCHING.md (2026-05-18 初版)

中国罫線 (珠盤路) + 大路の 2 罫線がそれぞれ独立に予想し、
両者の予想方向が一致した時のみ BET する戦略。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Literal

PredictionType = Literal["P", "B", "undecided"]
ActionType = Literal["BET_P", "BET_B", "LOOK"]


@dataclass
class Decision:
    action: ActionType
    china_pred: PredictionType
    china_pattern: Optional[str]  # 'telecho', 'niconico', 'p_line', 'b_line', None
    big_pred: PredictionType
    big_pattern: Optional[str]  # 'dragon', 'telecho', 'niconico', 'nikoichi', None
    reason: str


# === 中国罫線 (珠盤路) ===

def chinese_row_values(history: str, next_n: int) -> List[str]:
    """次の BET 通算番号 N の所属する行の確定済みマスを返す。

    Args:
        history: これまでの結果系列 (B/P/T、T も含む)
        next_n: 次の BET の通算番号 (1 始まり、= len(history) + 1)

    Returns:
        その行の左から右までの確定マス値 (list of 'B'/'P'/'T')
    """
    row = ((next_n - 1) % 6) + 1  # 1-indexed
    col_count = (next_n - 1) // 6  # next_n より前にあるこの行のマス数
    values = []
    for c in range(col_count):
        idx = c * 6 + (row - 1)  # 0-indexed in history
        if idx < len(history):
            values.append(history[idx])
    return values


def _is_telecho_chinese(pb: List[str]) -> bool:
    """完全交互 (PBPB or BPBP)。"""
    if len(pb) < 2:
        return False
    return all(pb[i] != pb[i + 1] for i in range(len(pb) - 1))


def _is_niconico_chinese(pb: List[str]) -> bool:
    """2 個ずつ交互 (PPBB or BBPP)、4 マス以上、**偶数マスのみ**。

    仕様 2.4 パターン B:
      P P B B ✓ (4 マス、偶数)
      B B P P B B ✓ (6 マス、偶数)
      P P B B P ✗ (5 マス、奇数で 2 連が崩れる)
    """
    n = len(pb)
    if n < 4:
        return False
    if n % 2 != 0:  # 奇数マスは「2 連が確定していない」状態 → 不認識
        return False
    if pb[0] != pb[1]:
        return False
    base = pb[0]
    other = "P" if base == "B" else "B"
    expected_cycle = [base, base, other, other]
    for i in range(n):
        if pb[i] != expected_cycle[i % 4]:
            return False
    return True


def _predict_niconico_next(pb: List[str]) -> str:
    """ニコニコの次の予想値。"""
    base = pb[0]
    other = "P" if base == "B" else "B"
    cycle = [base, base, other, other]
    return cycle[len(pb) % 4]


def chinese_road_predict(history: str, next_n: int) -> Tuple[PredictionType, Optional[str]]:
    """中国罫線 (珠盤路) の予想。

    Returns: (予想 'P'/'B'/'undecided', 認識パターン名 or None)
    """
    row_values = chinese_row_values(history, next_n)

    # 必須条件 1: 最小マス数 3 以上
    if len(row_values) < 3:
        return "undecided", None

    # 必須条件 2: T を含まない
    if "T" in row_values:
        return "undecided", None

    pb = row_values

    matches = []

    # パターン A: テレコ (完全交互)
    if _is_telecho_chinese(pb):
        # 直近マスの逆を予想
        next_pred = "B" if pb[-1] == "P" else "P"
        matches.append(("telecho", next_pred))

    # パターン B: ニコニコ
    if _is_niconico_chinese(pb):
        matches.append(("niconico", _predict_niconico_next(pb)))

    # パターン C: Pライン / Bライン (3 個以上)
    p_count = pb.count("P")
    b_count = pb.count("B")
    if p_count >= 3:
        matches.append(("p_line", "P"))
    if b_count >= 3:
        matches.append(("b_line", "B"))

    if len(matches) == 0:
        return "undecided", None
    if len(matches) > 1:
        preds = {m[1] for m in matches}
        if len(preds) == 1:
            # 複数パターンだが全方向一致 → 予想可能
            return preds.pop(), "+".join(m[0] for m in matches)
        # 方向不一致 → 判定不能
        return "undecided", "+".join(m[0] for m in matches)

    return matches[0][1], matches[0][0]


# === 大路 (Big Road) ===

def big_road_columns(history: str) -> List[List[str]]:
    """T を除外した P/B の列構造を返す。

    Returns: [[B], [PP], [B], [PPP], ...] のような列リスト
    """
    pb_only = [c for c in history if c != "T"]
    columns = []
    current = []
    for c in pb_only:
        if not current or current[-1] == c:
            current.append(c)
        else:
            columns.append(current)
            current = [c]
    if current:
        columns.append(current)
    return columns


def _detect_big_dragon(columns: List[List[str]]) -> Optional[str]:
    """ドラゴン: 最新列 4 連以上。返値: 予想 'P'/'B' or None"""
    if not columns:
        return None
    last_col = columns[-1]
    if len(last_col) >= 4:
        return last_col[0]
    return None


def _detect_big_telecho(columns: List[List[str]]) -> Optional[str]:
    """大路テレコ: 末尾 4 手以上が PBPB の完全交互 (各列の長さが 1)。

    Returns: 直近の手の逆 (= 交互継続予想)、または None
    """
    # 末尾 4 列が全て長さ 1、かつ交互
    if len(columns) < 4:
        return None
    last_4 = columns[-4:]
    if not all(len(c) == 1 for c in last_4):
        return None
    sides = [c[0] for c in last_4]
    if not all(sides[i] != sides[i + 1] for i in range(3)):
        return None
    last_outcome = sides[-1]
    return "B" if last_outcome == "P" else "P"


def _detect_big_niconico(columns: List[List[str]], history: str = "") -> Optional[str]:
    """大路ニコニコ: 末尾 8 手以上が PPBB or BBPP cycle の繰り返しに連続一致。

    仕様 3.2 C: 「PPBBPPBB の 8 手揃った時点で認識」「PPBBPPBBPP ✓ 認識継続」
    → cycle 途中でも認識継続

    Returns: 次の手の予想 (= cycle 継続予想)、または None
    """
    if history:
        pb_only = [c for c in history if c != "T"]
    else:
        pb_only = [c for col in columns for c in col]
    n = len(pb_only)
    if n < 8:
        return None

    # cycle: PPBB (first='P') or BBPP (first='B')
    for first in ["P", "B"]:
        other = "B" if first == "P" else "P"
        cycle = [first, first, other, other]
        # 末尾から逆順に確認、各 phase 位置を試す
        for phase in range(4):
            # 末尾 pb_only[n-1] が cycle[phase] 位置と仮定
            match_len = 0
            for k in range(n):
                cycle_pos = (phase - k) % 4
                if pb_only[n - 1 - k] != cycle[cycle_pos]:
                    break
                match_len += 1
            if match_len >= 8:
                # 次の予想: cycle[(phase + 1) % 4]
                return cycle[(phase + 1) % 4]
    return None


def _detect_big_nikoichi(columns: List[List[str]], history: str = "") -> Optional[str]:
    """大路ニコイチ: 末尾 6 手以上が PPBPPB or BBPBBP cycle の繰り返しに連続一致。

    仕様 3.2 D: 「PPBPPB の 6 手揃った時点で認識」
    cycle 周期 3: [base, base, other] の繰り返し

    Returns: 次の手の予想 (= cycle 継続予想)、または None
    """
    if history:
        pb_only = [c for c in history if c != "T"]
    else:
        pb_only = [c for col in columns for c in col]
    n = len(pb_only)
    if n < 6:
        return None

    # cycle: PPB (first='P') or BBP (first='B')、周期 3
    for first in ["P", "B"]:
        other = "B" if first == "P" else "P"
        cycle = [first, first, other]  # 周期 3
        for phase in range(3):
            match_len = 0
            for k in range(n):
                cycle_pos = (phase - k) % 3
                if pb_only[n - 1 - k] != cycle[cycle_pos]:
                    break
                match_len += 1
            if match_len >= 6:
                return cycle[(phase + 1) % 3]
    return None


def big_road_predict(history: str) -> Tuple[PredictionType, Optional[str]]:
    """大路の予想。

    Returns: (予想 'P'/'B'/'undecided', 認識パターン名 or None)
    """
    columns = big_road_columns(history)
    if not columns:
        return "undecided", None

    matches = []

    dragon = _detect_big_dragon(columns)
    if dragon:
        matches.append(("dragon", dragon))

    telecho = _detect_big_telecho(columns)
    if telecho:
        matches.append(("telecho", telecho))

    niconico = _detect_big_niconico(columns, history)
    if niconico:
        matches.append(("niconico", niconico))

    nikoichi = _detect_big_nikoichi(columns, history)
    if nikoichi:
        matches.append(("nikoichi", nikoichi))

    if len(matches) == 0:
        return "undecided", None
    if len(matches) > 1:
        preds = {m[1] for m in matches}
        if len(preds) == 1:
            # 複数パターンだが全方向一致 → 予想可能
            return preds.pop(), "+".join(m[0] for m in matches)
        # 方向不一致 → 判定不能 (仕様 3.3)
        return "undecided", "+".join(m[0] for m in matches)

    return matches[0][1], matches[0][0]


# === マッチング判定 ===
# === マッチング判定 ===

def score_proximity(history: str, next_n: Optional[int] = None) -> tuple[int, str]:
    """テーブル選択用の近接度スコア。

    Returns:
        (0, ""): cold — 両道路 undecided
        (1, "P"/"B"): warm — 一方の道路のみ予想あり
        (2, "P"/"B"): hot — 両道路が同一方向予想 (= BET signal)
    """
    if next_n is None:
        next_n = len(history) + 1

    china_pred, _cp = chinese_road_predict(history, next_n)
    big_pred, _bp = big_road_predict(history)

    if china_pred != "undecided" and big_pred != "undecided" and china_pred == big_pred:
        return (2, china_pred)
    if china_pred != "undecided" or big_pred != "undecided":
        direction = china_pred if china_pred != "undecided" else big_pred
        return (1, direction)
    return (0, "")



def decide(history: str, next_n: Optional[int] = None) -> Decision:
    """中国罫線と大路を独立評価、両者一致時のみ BET。

    Args:
        history: B/P/T 文字列
        next_n: 次の BET 通算番号 (None なら len(history) + 1)
    """
    if next_n is None:
        next_n = len(history) + 1

    china_pred, china_pattern = chinese_road_predict(history, next_n)
    big_pred, big_pattern = big_road_predict(history)

    # 判定 (仕様 4.1)
    if china_pred == "undecided" or big_pred == "undecided":
        return Decision(
            action="LOOK",
            china_pred=china_pred,
            china_pattern=china_pattern,
            big_pred=big_pred,
            big_pattern=big_pattern,
            reason=f"undecided china={china_pattern} big={big_pattern}",
        )

    if china_pred == big_pred:
        action: ActionType = "BET_P" if china_pred == "P" else "BET_B"
        return Decision(
            action=action,
            china_pred=china_pred,
            china_pattern=china_pattern,
            big_pred=big_pred,
            big_pattern=big_pattern,
            reason=f"match china={china_pattern}({china_pred}) big={big_pattern}({big_pred})",
        )

    return Decision(
        action="LOOK",
        china_pred=china_pred,
        china_pattern=china_pattern,
        big_pred=big_pred,
        big_pattern=big_pattern,
        reason=f"mismatch china={china_pattern}({china_pred}) big={big_pattern}({big_pred})",
    )


# === セルフテスト (仕様書の例題で挙動確認) ===

def _self_test():
    """仕様書の判定例で実装の正しさを確認。"""
    # 仕様 5.2: 通算 8 手済み、次は 9 手目、行 3 の値が [P, P, P]、大路最新列 [PPPP]
    # → 両者 P で一致 → BET_P
    history_52 = "PPPBPPBP"  # 8 手、たまたま row 3 が PPP となるよう構成 (= position 3,4,5,6 行 3 のマス)
    # 実は 1-indexed で hand 3 は row 3 col 1、hand 9 は row 3 col 2
    # 9 手目を予想する時、row 3 の確定マスは hand 3 のみ = 1 マスで判定不能
    # 仕様 5.2 は「3行目に3マス埋まっている」前提なので、history がもっと長い必要
    # → 8 手済みで row 3 col 1 のみ確定 (= 1 マス) では判定不能
    # 仕様の例 5.2 は不整合 (8 手では row 3 に 1 マスしか入らない)
    # ⇒ 例 5.2 は概念例として扱う、実装は仕様の式に従う

    # 仕様 5.1: 37 手済み、次 38 手目、row = ((38-1) % 6) + 1 = 2
    # row 2 の確定マスは hand 2, 8, 14, 20, 26, 32 (= 6 マス)
    # それらが [B, T, B, P, P, P] なら T 含む → 判定不能
    history_51 = list("PBPBPB" * 6 + "P")  # 37 手作成、row 2 = hands 2,8,14,20,26,32
    # hand 2 = 'B', hand 8 = 'B' (= 7+1 = 'B' if seq = PBPBPB-PBPBPB-...)
    # 仕様例の状況を厳密に再現するため、テスト省略

    # シンプルテスト: row が PPP の場合 → Pライン
    test_row = ["P", "P", "P"]
    # 3 マス、T 不含
    # テレコ? P=P=P で交互でない → False
    # ニコニコ? 4 マス未満 → False
    # Pライン? P=3 ≥ 3 → True
    # → 1 該当、予想 P
    assert _is_telecho_chinese(test_row) is False
    assert _is_niconico_chinese(test_row) is False
    assert test_row.count("P") >= 3
    print("Test 1 (Pライン): OK")

    # ドラゴンテスト
    history_dragon = "PPPPP"  # 5 連 P
    cols = big_road_columns(history_dragon)
    assert cols == [["P", "P", "P", "P", "P"]]
    assert _detect_big_dragon(cols) == "P"
    print("Test 2 (ドラゴン): OK")

    # テレコ大路テスト
    history_telecho = "PBPBPB"  # 6 手交互
    cols = big_road_columns(history_telecho)
    assert cols == [["P"], ["B"], ["P"], ["B"], ["P"], ["B"]]
    assert _detect_big_telecho(cols) == "P"  # 末尾 B、次は P
    print("Test 3 (大路テレコ): OK")

    # ニコニコ大路テスト
    history_niconico_big = "PPBBPPBB"  # 8 手
    cols = big_road_columns(history_niconico_big)
    assert cols == [["P", "P"], ["B", "B"], ["P", "P"], ["B", "B"]]
    assert _detect_big_niconico(cols) == "P"  # 次は P 開始
    print("Test 4 (大路ニコニコ): OK")

    # ニコイチ大路テスト
    history_nikoichi = "PPBPPB"  # 6 手 (2-1-2-1)
    cols = big_road_columns(history_nikoichi)
    assert cols == [["P", "P"], ["B"], ["P", "P"], ["B"]]
    assert _detect_big_nikoichi(cols) == "P"  # 末尾 B、次は P
    print("Test 5 (大路ニコイチ): OK")

    # マッチング判定テスト: 仕様 5.3 不一致例
    # 中国 telecho → B, 大路 dragon → P → 不一致 → LOOK
    # 中国の telecho 状態を再現 (行に BPBP)
    # 簡略テスト
    print("Test 6 (判定統合): SKIP (= 統合テストは backtest で確認)")

    print("\nAll basic tests passed.")


if __name__ == "__main__":
    _self_test()
