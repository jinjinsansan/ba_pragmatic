"""dual_line_logic.py — デュアルライン予想ロジック

SPEC_DUAL_LINE_MATCHING.md 準拠。

公開 API:
    decide(history: str, next_n: int) -> Decision
        history: これまでの確定結果（'B'/'P'/'T'の文字列、next_n 番より前のみ）
        next_n : 次にBETする手の通算番号（1始まり）
        戻り値: Decision（action='BET_B'|'BET_P'|'LOOK', 各パターン名 等）

第II部 バックテスト用ループ:
    for i, outcome in enumerate(shoe):   # i=0..N-1
        next_n = i + 1
        history = shoe[:i]               # i手目より前だけ渡す ← 絶対ルール
        d = decide(history, next_n)
        if d.action != 'LOOK':
            answer_check(outcome, d)     # ここで初めて i手目の結果を見る
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# データクラス
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Decision:
    action: str           # 'BET_B' | 'BET_P' | 'LOOK'
    bet_side: str         # 'B' | 'P' | ''
    china_pattern: str    # 'telecho'|'niconico'|'sansan'|'pline'|'bline'|''
    china_pred: str       # 'B' | 'P' | ''
    big_pattern: str      # 'dragon'|'telecho'|'niconico'|'nikoichi'|''
    big_pred: str         # 'B' | 'P' | ''
    reason: str           # デバッグ用メモ


LOOK = Decision(
    action='LOOK', bet_side='',
    china_pattern='', china_pred='',
    big_pattern='', big_pred='',
    reason='',
)


# ─────────────────────────────────────────────────────────────────────────────
# 中国罫線（珠盤路）
# ─────────────────────────────────────────────────────────────────────────────

def _bead_row_cells(history: str, next_n: int) -> List[str]:
    """next_n 番が入る行の、確定済みセルを左→右で返す。

    - 行番号 = ((next_n - 1) mod 6) + 1
    - 各セルの history インデックス = c * 6 + (row - 1)  for c = 0, 1, 2, ...
    - T もそのまま含める（T 含み行は呼び出し元で判定不能扱い）
    """
    row = ((next_n - 1) % 6) + 1           # 1〜6
    col_count = (next_n - 1) // 6          # この行に既にある列数
    cells = []
    for c in range(col_count):
        idx = c * 6 + (row - 1)
        if idx < len(history):
            cells.append(history[idx])
    return cells


def _is_telecho(pb: List[str]) -> bool:
    """行全体が完全1個交互（PBPB... / BPBP...）かどうか。"""
    if len(pb) < 2:
        return False
    return all(pb[i] != pb[i + 1] for i in range(len(pb) - 1))


def _predict_telecho(pb: List[str]) -> str:
    """テレコの次の予想: 直近マスの逆。"""
    return 'B' if pb[-1] == 'P' else 'P'


def _is_niconico(pb: List[str]) -> bool:
    """行全体が 2個ずつ交互（PPBB の繰り返し）、偶数マス・4マス以上。"""
    n = len(pb)
    if n < 4 or n % 2 != 0:
        return False
    if pb[0] != pb[1]:          # 先頭 2 個が同じでなければ PPBB パターンではない
        return False
    base = pb[0]
    other = 'B' if base == 'P' else 'P'
    cycle = [base, base, other, other]
    return all(pb[i] == cycle[i % 4] for i in range(n))


def _predict_niconico(pb: List[str]) -> str:
    """ニコニコの次の予想: cycle[(n) % 4]。"""
    base = pb[0]
    other = 'B' if base == 'P' else 'P'
    cycle = [base, base, other, other]
    return cycle[len(pb) % 4]


def _is_sansan(pb: List[str]) -> bool:
    """サンサン: ちょうど5マス、最初の3マスが同側・次の2マスが逆側。

    PPPBB または BBBPP のみ。4連以上（PPPP...）は対象外。
    """
    if len(pb) != 5:
        return False
    # 最初の3マスが全て同じ
    if not (pb[0] == pb[1] == pb[2]):
        return False
    # 最後の2マスが全て同じ
    if pb[3] != pb[4]:
        return False
    # 2グループは異なる側
    return pb[0] != pb[3]


def _predict_sansan(pb: List[str]) -> str:
    """サンサンの次の予想: 2連目の側を継続（3連3連を完成させる）。

    PPPBB → B（BBB を完成させる）
    BBBPP → P（PPP を完成させる）
    """
    return pb[3]        # 後半グループの側


def predict_bead(row_cells: List[str]) -> tuple[str, str]:
    """珠盤路の行から予想を返す。

    SPEC 第I部 2.4〜3.1 に従い、優先順位を厳守。

    Returns:
        (pred, pattern_name)
        pred: 'P' | 'B' | '' (空文字 = 判定不能)
        pattern_name: 'telecho'|'niconico'|'sansan'|'pline'|'bline'|''
    """
    # 前提条件チェック
    if len(row_cells) < 3:
        return '', ''

    # ── Step 1: 形状パターン（テレコ / ニコニコ / サンサン）──
    # 形状は従来通り T を含む行は判定不能（T無し行のみ評価）。
    if 'T' not in row_cells:
        pb = row_cells   # T 不含は保証済み
        shape_matches: List[tuple[str, str]] = []  # (pattern_name, pred)

        if _is_telecho(pb):
            shape_matches.append(('telecho', _predict_telecho(pb)))

        if _is_niconico(pb):
            shape_matches.append(('niconico', _predict_niconico(pb)))

        if _is_sansan(pb):
            shape_matches.append(('sansan', _predict_sansan(pb)))

        if len(shape_matches) == 1:
            # ちょうど1つ → 確定（Pライン/Bラインは見ない）
            return shape_matches[0][1], shape_matches[0][0]

        if len(shape_matches) >= 2:
            # 2つ以上 → 判定不能（見送り）
            return '', ''

    # ── Step 2: 形状0個のときのみ Pライン / Bライン【新定義 2026-06-07】──
    # T を無視し P/B だけで数える。P/B が 5 個以上 かつ 逆側が累積 1 個まで
    # （2個目が出たら線終了）→ 多数側を順張り予想。線が続く限り継続。
    # （旧定義は「P/B が3個以上」の多数決で、PPBBP のような散らばりも誤って線扱い）
    pb_only = [c for c in row_cells if c != 'T']
    if len(pb_only) >= 5:
        p_cnt = pb_only.count('P')
        b_cnt = pb_only.count('B')
        if b_cnt <= 1:
            return 'P', 'pline'   # 逆(B)が1個以下 → Pライン → 次P
        if p_cnt <= 1:
            return 'B', 'bline'   # 逆(P)が1個以下 → Bライン → 次B

    # 線にならない → 判定不能
    return '', ''


# ─────────────────────────────────────────────────────────────────────────────
# 大路（Big Road）— SPEC 第I部 4章
# ─────────────────────────────────────────────────────────────────────────────

def _build_big_road_columns(history: str) -> List[List[str]]:
    """T を除外した P/B の列構造を返す。"""
    pb_only = [c for c in history if c != 'T']
    columns: List[List[str]] = []
    current: List[str] = []
    for c in pb_only:
        if not current or current[-1] == c:
            current.append(c)
        else:
            columns.append(current)
            current = [c]
    if current:
        columns.append(current)
    return columns


def _predict_dragon(columns: List[List[str]]) -> Optional[str]:
    """ドラゴン: 最新列が4連以上 → その側を継続予想。"""
    if not columns:
        return None
    last = columns[-1]
    return last[0] if len(last) >= 4 else None


def _predict_big_telecho(columns: List[List[str]]) -> Optional[str]:
    """大路テレコ: 末尾4列以上が全て長さ1かつ完全交互。"""
    if len(columns) < 4:
        return None
    last4 = columns[-4:]
    if not all(len(c) == 1 for c in last4):
        return None
    sides = [c[0] for c in last4]
    if not all(sides[i] != sides[i + 1] for i in range(3)):
        return None
    return 'B' if sides[-1] == 'P' else 'P'


def _scan_back(pb_only: List[str], period: int, min_match: int,
               cycle_templates: List[List[str]]) -> Optional[str]:
    """末尾スキャン: cycle_templates のどれかが period 周期で min_match 手以上続けば次を返す。"""
    n = len(pb_only)
    if n < min_match:
        return None
    for cycle in cycle_templates:
        for phase in range(period):
            ml = 0
            for k in range(n):
                if pb_only[n - 1 - k] != cycle[(phase - k) % period]:
                    break
                ml += 1
            if ml >= min_match:
                return cycle[(phase + 1) % period]
    return None


def _predict_big_niconico(history: str) -> Optional[str]:
    """大路ニコニコ: 末尾8手以上が PPBB または BBPP cycle。"""
    pb = [c for c in history if c != 'T']
    return _scan_back(pb, period=4, min_match=8, cycle_templates=[
        ['P', 'P', 'B', 'B'],
        ['B', 'B', 'P', 'P'],
    ])


def _predict_big_nikoichi(history: str) -> Optional[str]:
    """大路ニコイチ: 末尾6手以上が PPB または BBP cycle。"""
    pb = [c for c in history if c != 'T']
    return _scan_back(pb, period=3, min_match=6, cycle_templates=[
        ['P', 'P', 'B'],
        ['B', 'B', 'P'],
    ])


def predict_big_road(history: str) -> tuple[str, str]:
    """大路の予想を返す。

    Returns:
        (pred, pattern_name)
        pred: 'P' | 'B' | '' (空文字 = 判定不能)
    """
    columns = _build_big_road_columns(history)

    matches: List[tuple[str, str]] = []

    d = _predict_dragon(columns)
    if d:
        matches.append(('dragon', d))

    t = _predict_big_telecho(columns)
    if t:
        matches.append(('telecho', t))

    ni = _predict_big_niconico(history)
    if ni:
        matches.append(('niconico', ni))

    ko = _predict_big_nikoichi(history)
    if ko:
        matches.append(('nikoichi', ko))

    if len(matches) == 0:
        return '', ''
    if len(matches) == 1:
        return matches[0][1], matches[0][0]

    # 複数該当: 全方向が一致すれば有効、不一致は判定不能
    preds = {m[1] for m in matches}
    if len(preds) == 1:
        return preds.pop(), matches[0][0]
    return '', ''


# ─────────────────────────────────────────────────────────────────────────────
# マッチング判定
# ─────────────────────────────────────────────────────────────────────────────

def decide(history: str, next_n: int) -> Decision:
    """中国罫線と大路を独立評価し、両者一致時のみ BET を返す。

    Args:
        history : 確定済み結果の文字列（next_n 番より前のもの**だけ**渡すこと）
        next_n  : 次にBETする手の通算番号（1始まり）

    Returns:
        Decision
    """
    # 中国罫線
    row_cells = _bead_row_cells(history, next_n)
    china_pred, china_pat = predict_bead(row_cells)

    # 大路
    big_pred, big_pat = predict_big_road(history)

    # どちらかが判定不能 → LOOK
    if not china_pred or not big_pred:
        return Decision(
            action='LOOK', bet_side='',
            china_pattern=china_pat, china_pred=china_pred,
            big_pattern=big_pat, big_pred=big_pred,
            reason=f'undecided china={china_pat!r} big={big_pat!r}',
        )

    # 予想が一致 → BET
    if china_pred == big_pred:
        side = china_pred
        action = 'BET_B' if side == 'B' else 'BET_P'
        return Decision(
            action=action, bet_side=side,
            china_pattern=china_pat, china_pred=china_pred,
            big_pattern=big_pat, big_pred=big_pred,
            reason=f'MATCH {china_pat}|{big_pat}|{side}',
        )

    # 不一致 → LOOK
    return Decision(
        action='LOOK', bet_side='',
        china_pattern=china_pat, china_pred=china_pred,
        big_pattern=big_pat, big_pred=big_pred,
        reason=f'conflict china={china_pred}({china_pat}) big={big_pred}({big_pat})',
    )
