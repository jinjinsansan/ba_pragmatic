"""〇❌パーティゲーム ロジック (Python移植)

⚠️  SERVER-ONLY — DO NOT SHIP TO CLIENT ⚠️
このモジュールは SEQ 配列 (core martingale sequence) と
finalize_set/calc_slashed/calc_next_unit_idx の機密ロジックを含みます。
VPS の laplace_api (logic engine) と marubatsu monitor のみから
import され、client distribution には含めてはいけません
(.dist_excludes 参照)。

maru プロジェクトの gameLogic.ts を忠実にPythonへ移植。
バカラ結果を Player=〇, Banker=✕ にマッピングし、
7ハンド=1セットとして overshoot / 斜線 / bet単位 / 損益を管理する。
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("baccarat.marubatsu")

SEQ = [
    1, 2, 3, 5, 7, 9, 11, 13,
    16, 19, 22, 25, 28, 31,
    35, 39, 43, 47, 51, 55,
    60, 65, 70, 75, 80, 85, 90, 95, 100,
    106, 112, 118, 124, 130, 136, 142, 148, 154, 160,
    170, 180, 190, 200, 210, 220, 230, 240, 250,
]

# Counter モード専用 SEQ (バックテストで+67%の利益改善)
SEQ_COUNTER = [
    1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
    60, 70, 80, 90, 100, 110, 120, 130,
    145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
    300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500,
]

# Counter モード専用セットサイズ
SET_SIZE_COUNTER = 5
SET_SIZE_DEFAULT = 7


@dataclass
class SetData:
    set_index: int
    results: str            # "OOXOXXO" (7文字)
    wins: int
    losses: int
    overshoot: int
    slashed: bool
    used_unit_idx: int
    next_unit_idx: int
    set_profit: int
    cumulative_profit: int


def calc_wins_losses(results: str, set_size: int = 7) -> tuple[int, int, int]:
    wins = results.count("O")
    losses = set_size - wins
    diff = wins - losses
    return wins, losses, diff


def calc_overshoot(prev_overshoot: int, diff: int) -> int:
    new_os = prev_overshoot - diff
    return max(new_os, 0)


def calc_slashed(sets: list[SetData], new_overshoot: int, diff: int) -> list[int]:
    if diff <= 0:
        return []
    slashed_indices: list[int] = []
    for i, s in enumerate(sets):
        if not s.slashed and s.overshoot > new_overshoot:
            s.slashed = True
            slashed_indices.append(i)
    return slashed_indices


def calc_next_unit_idx(
    sets: list[SetData],
    used_unit_idx: int,
    diff: int,
    new_overshoot: int,
    seq_len: int = 0,
) -> int:
    _max = (seq_len or len(SEQ)) - 1
    if diff < 0:
        return min(used_unit_idx + 1, _max)

    # 優先①: 同じovershootの非斜線セットを後ろから探す
    found = -1
    for fi in range(len(sets) - 1, -1, -1):
        if not sets[fi].slashed and sets[fi].overshoot == new_overshoot:
            found = sets[fi].next_unit_idx
            break
    if found >= 0:
        return found

    # 優先②③: 非斜線セットからovershootが近いものを探す
    best_above_idx = -1
    best_above_diff = float("inf")
    best_below_idx = -1
    best_below_diff = float("inf")

    for fk in range(len(sets)):
        if not sets[fk].slashed:
            dd = sets[fk].overshoot - new_overshoot
            if dd > 0 and dd < best_above_diff:
                best_above_diff = dd
                best_above_idx = sets[fk].next_unit_idx
            if dd < 0 and (-dd) < best_below_diff:
                best_below_diff = -dd
                best_below_idx = sets[fk].next_unit_idx

    # 優先②: 大きい方で最も近い
    if best_above_idx >= 0:
        return best_above_idx
    # 優先③: 大きい方がない → 小さい方で最も近いの next_unit_idx + 1
    if best_below_idx >= 0:
        return min(best_below_idx + 1, _max)

    return 0


def finalize_set(
    results: str,
    sets: list[SetData],
    current_unit_idx: int,
    prev_cumulative_profit: int,
    prev_overshoot: int,
    seq: list[int] | None = None,
    set_size: int = 7,
) -> SetData:
    _seq = seq or SEQ
    wins, losses, diff = calc_wins_losses(results, set_size)
    new_overshoot = calc_overshoot(prev_overshoot, diff)
    calc_slashed(sets, new_overshoot, diff)
    next_unit_idx = calc_next_unit_idx(sets, current_unit_idx, diff, new_overshoot, seq_len=len(_seq))
    set_profit = diff * _seq[current_unit_idx] if current_unit_idx < len(_seq) else diff * _seq[-1]
    cumulative_profit = prev_cumulative_profit + set_profit

    return SetData(
        set_index=len(sets) + 1,
        results=results,
        wins=wins,
        losses=losses,
        overshoot=new_overshoot,
        slashed=False,
        used_unit_idx=current_unit_idx,
        next_unit_idx=next_unit_idx,
        set_profit=set_profit,
        cumulative_profit=cumulative_profit,
    )


class MaruBatsuTracker:
    """バカラ結果を〇❌に変換し、Nハンド=1セットで管理"""

    def __init__(self, chip_base: float = 1.0, seq: list[int] | None = None, set_size: int = 7):
        self.chip_base = chip_base
        self.seq = seq or SEQ
        self.set_size = set_size
        self.sets: list[SetData] = []
        self.current_turns: list[str] = []  # "O" or "X"
        self.total_o = 0
        self.total_x = 0

    @property
    def current_unit_idx(self) -> int:
        if self.sets:
            return self.sets[-1].next_unit_idx
        return 0

    @property
    def cumulative_profit(self) -> int:
        if self.sets:
            return self.sets[-1].cumulative_profit
        return 0

    @property
    def prev_overshoot(self) -> int:
        if self.sets:
            return self.sets[-1].overshoot
        return 0

    @property
    def current_set_index(self) -> int:
        return len(self.sets) + 1

    @property
    def current_turn_number(self) -> int:
        return len(self.current_turns) + 1

    def add_result(self, baccarat_result: str) -> SetData | None:
        """バカラ結果を追加。Tie はスキップ。
        7ターン溜まったらセット確定して SetData を返す。
        """
        if baccarat_result == "tie":
            return None

        mark = "O" if baccarat_result == "player" else "X"
        self.current_turns.append(mark)

        if mark == "O":
            self.total_o += 1
        else:
            self.total_x += 1

        if len(self.current_turns) == self.set_size:
            results_str = "".join(self.current_turns)
            new_set = finalize_set(
                results_str,
                self.sets,
                self.current_unit_idx,
                self.cumulative_profit,
                self.prev_overshoot,
                seq=self.seq,
                set_size=self.set_size,
            )
            self.sets.append(new_set)
            self.current_turns.clear()
            return new_set

        return None

    def format_set_line(self, s: SetData) -> str:
        """1セットを1行にフォーマット"""
        marks = s.results.replace("O", "〇").replace("X", "✕")
        wl = f"{s.wins}/{s.losses}"
        slash = " ✂️" if s.slashed else ""
        return (
            f"#{s.set_index:>2}  {marks}  "
            f"{wl}  OS:{s.overshoot}  "
            f"U:{self.seq[min(s.next_unit_idx, len(self.seq)-1)]}  "
            f"P/L:{s.cumulative_profit:+d}{slash}"
        )

    def format_telegram_set_complete(self, new_set: SetData) -> str:
        """セット確定時のTelegram通知メッセージを生成"""
        marks = new_set.results.replace("O", "〇").replace("X", "✕")
        diff = new_set.wins - new_set.losses
        outcome = "勝ち越し" if diff > 0 else "負け越し"

        money_set = new_set.set_profit * self.chip_base
        money_cum = new_set.cumulative_profit * self.chip_base

        msg = (
            f"━━ Set #{new_set.set_index} 確定 ━━\n"
            f"\n"
            f"{marks}\n"
            f"勝敗: {new_set.wins}/{new_set.losses} ({outcome})\n"
            f"負け越し: {new_set.overshoot}\n"
            f"BET単位: {self.seq[min(new_set.used_unit_idx, len(self.seq)-1)]} (SEQ[{new_set.used_unit_idx}])\n"
            f"セット損益: {new_set.set_profit:+d} chip ({money_set:+.0f}円)\n"
            f"累計損益: {new_set.cumulative_profit:+d} chip ({money_cum:+.0f}円)\n"
        )

        # 次セット情報
        msg += (
            f"次BET単位: {self.seq[min(new_set.next_unit_idx, len(self.seq)-1)]} (SEQ[{new_set.next_unit_idx}])\n"
        )

        # 〇❌統計
        total = self.total_o + self.total_x
        if total > 0:
            o_pct = round(self.total_o / total * 100)
            x_pct = round(self.total_x / total * 100)
            msg += f"\n〇{o_pct}%({self.total_o}) ✕{x_pct}%({self.total_x})\n"

        # 管理表 (直近セット一覧)
        display_sets = self.sets[-10:]
        if display_sets:
            msg += f"\n── 管理表 ──\n"
            for s in display_sets:
                msg += self.format_set_line(s) + "\n"

        msg += f"━━━━━━━━━━━━━━━"
        return msg

    def format_telegram_turn_update(self) -> str:
        """現在進行中のターン状態を返す (ログ用)"""
        if not self.current_turns:
            return f"Set #{self.current_set_index} — 待機中"

        marks = "".join("〇" if t == "O" else "✕" for t in self.current_turns)
        remaining = 7 - len(self.current_turns)
        unit = self.seq[min(self.current_unit_idx, len(self.seq)-1)]
        return (
            f"Set #{self.current_set_index} Turn {len(self.current_turns)}/7: "
            f"{marks}{'_' * remaining} | BET単位:{unit} | 累計:{self.cumulative_profit:+d}"
        )

    def get_status(self) -> dict:
        return {
            "set_count": len(self.sets),
            "current_turn": len(self.current_turns),
            "current_unit_idx": self.current_unit_idx,
            "current_unit": self.seq[min(self.current_unit_idx, len(self.seq)-1)],
            "cumulative_profit": self.cumulative_profit,
            "cumulative_money": self.cumulative_profit * self.chip_base,
            "total_o": self.total_o,
            "total_x": self.total_x,
        }
