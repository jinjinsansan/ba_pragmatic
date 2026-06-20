"""Dual-Line Bet 資金管理モジュール

BetManager:
  - フラット ベット（単一ユニット）
  - SMALL SEQ シリーズ (0.2/0.6/1/3/6/10/30 start)
  - 1-2-3打法 (bet123: 1回目1単位→2回目2単位→同結果連続なら3回目3単位→リセット)
  - 純粋マーチンゲール
  - 利確 / 損切
  - 利確/損切後の動作: STOP or RESTART

Usage:
  from dual_line_money import BetManager
  money = BetManager(mode="flat", unit=100.0, profit_stop=500.0, loss_cut=1000.0,
                     on_limit="stop")
  amount = money.next_bet(side="P")
  money.apply_result(won=True)
  if money.limit_reached:
      print(f"LIMIT: {money.limit_reason}")
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from marubatsu_strategy import MaruBatsuTracker, SetData

logger = logging.getLogger("dual_line.money")

# ── SEQ 定義 ─────────────────────────────────────────────────────────

SEQ_SMALL3 = [
    3, 5, 7, 12, 17, 23, 30,
    38, 47, 57, 68, 83, 97,
    112, 128, 145, 165, 190, 220, 255, 300, 360,
    420, 500, 600, 700, 800, 900, 1000,
]

SEQ_SMALL02 = [
    0.2, 0.4, 0.6, 0.8, 1.2, 1.6, 2.0,
    2.6, 3.2, 3.8, 4.6, 5.6, 6.4,
    7.4, 8.6, 9.6, 11.0, 12.6, 14.6, 17.0, 20.0, 24.0,
    28.0, 33.4, 40.0, 46.6, 53.4, 60.0, 66.6,
]

SEQ_SMALL1 = [
    1, 2, 4, 6, 8, 10, 13, 16, 19, 23, 28, 32, 37, 43,
    48, 55, 63, 73, 85, 100, 120, 140, 167, 200, 233, 267, 300, 333,
]

# SMALL1 のちょうど2倍($2 start・天井666x)。全段が偶数=チップ妥当額。
SEQ_SMALL2 = [
    2, 4, 8, 12, 16, 20, 26, 32, 38, 46, 56, 64, 74, 86,
    96, 110, 126, 146, 170, 200, 240, 280, 334, 400, 466, 534, 600, 666,
]

# SMALL02 のちょうど3倍(全段がチップ最小単位 $0.2 の倍数)
SEQ_SMALL06 = [
    0.6, 1.2, 1.8, 2.4, 3.6, 4.8, 6.0,
    7.8, 9.6, 11.4, 13.8, 16.8, 19.2,
    22.2, 25.8, 28.8, 33.0, 37.8, 43.8, 51.0, 60.0, 72.0,
    84.0, 100.2, 120.0, 139.8, 160.2, 180.0, 199.8,
]

SEQ_SMALL6 = [
    6, 10, 14, 24, 34, 46, 60, 76, 94, 114, 136, 166, 194, 224, 256,
    290, 330, 380, 440, 510, 600, 720, 840, 1000, 1200, 1400, 1600, 1800, 2000,
]

SEQ_SMALL10 = [
    10, 20, 30,
    50, 70, 90, 110, 130,
    160, 200, 240, 280, 320,
    370, 420, 470, 520, 570, 620,
    680, 740, 800, 860, 920, 980, 1050,
]

# 旧 NewSEQ30 (bacopy_executor_pragmatic_ws_live.py SEQ_NEW[1:]) 由来・投資家向け
SEQ_SMALL30 = [
    30, 50, 70,
    110, 150, 200, 250, 300,
    370, 450, 530, 610, 700, 810, 940,
    1090, 1260, 1440, 1640, 1870, 2150, 2450, 2800,
]

# ── SEQ 型(shape) — 階段の“並び”を $1 基準で定義し開始額で比例展開する ──────
# 攻撃型(attack) = 上記 SEQ_SMALL* をそのまま使う(従来=ゼロ回帰)。
# バランス型(balance)=CAND_A / 守備型(defense)=CAND_B は $1 基準の整数配列で、
# 開始額(各versionの先頭額=0.2/0.6/1/3/6/10/30)を掛けて版を生成する。
# 整数×(0.2の倍数)=0.2の倍数 なので全段がチップ妥当額になる(丸め不要)。
# 2026-06-16 のリスク分析(`SEQ_STAIRCASE_ANALYSIS_2026-06-16.md`)に基づく:
#   守備型は必要元本ほぼ半減・破滅テール46%減(利益は約56%維持)。
SEQ_SHAPE_BALANCE = [  # CAND_A: 序盤緩め・天井250x(利益77%維持)
    1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 18, 22, 27, 33, 40, 48, 58, 70, 84,
    100, 115, 130, 145, 160, 175, 190, 205, 220, 235, 250,
]
SEQ_SHAPE_DEFENSE = [  # CAND_B: 序盤最緩・天井200x(生存最優先)
    1, 1, 1, 2, 2, 3, 4, 5, 6, 8, 10, 12, 15, 18, 22, 26, 31, 37, 44, 52,
    62, 74, 88, 104, 122, 140, 158, 176, 194, 200,
]
SMALL_SEQ_MODES = ("small02", "small06", "small1", "small2", "small3", "small6", "small10", "small30")

BET_MODES = {
    "flat": "1 unit flat",
    "small02": "SMALL SEQ $0.20 start",
    "small06": "SMALL SEQ $0.60 start",
    "small1": "SMALL SEQ $1 start",
    "small2": "SMALL SEQ $2 start",
    "small3": "SMALL SEQ $3 start",
    "small6": "SMALL SEQ $6 start",
    "small10": "SMALL SEQ $10 start",
    "small30": "SMALL SEQ $30 start (ex-NewSEQ30)",
    "martingale": "pure Martingale",
    "dalembert": "D'Alembert (+/-1 unit)",
    "bet123": "1-2-3 method (1/2/3 units cycle)",
    "kelly": "Kelly proportional (bet = f x bankroll)",
}
ALLOWED_MODES = set(BET_MODES.keys())

BANKER_COMMISSION = 0.95

# ── Kelly(比例)モード ─────────────────────────────────────────────────
# bet = type係数 × (edge/payout) × 現在残高。残高に比例するのでゼロにならない
# (破滅しない)・複利で伸びる。SEQの真逆=負けで増やさず、エッジと残高に比例。
# 型(BACOPY_SEQ_SHAPE流用): 攻撃=フルKelly / バランス=ハーフ / 守備=クォーター。
# edge=1手あたりのEV(既定=v3実測+0.61%)。payout=0.97(60%Banker*0.95+40%Player)。
# 2026-06-20 徹底バックテスト(`_seq_pause_test/kelly_research.py`)で確定:
#   守備=~2x/年・DD中央27%・エッジ誤差に頑健。攻撃=フルはDD78%・エッジ過信で全損注意。
KELLY_PAYOUT = 0.97
KELLY_CHIP = 0.2
KELLY_SHAPE_MULT = {"attack": 1.0, "balance": 0.5, "defense": 0.25}


# ── BetManager ────────────────────────────────────────────────────────


class BetManager:
    """Dual-Line 用 資金管理。

    状態管理:
      - session_pnl: セッション開始時からの損益 ($)
      - seq_level: SEQ モード時の現在のインデックス
      - loss_count: Martingale 時の連続負け数
      - limit_reached: 利確 or 損切に到達
    """

    def __init__(
        self,
        *,
        mode: str = "flat",
        unit: float = 100.0,
        profit_stop: float = 0.0,
        loss_cut: float = 0.0,
        on_limit: Literal["stop", "restart"] = "stop",
        state_path: Optional[Path] = None,
        seq_set_size: int = 7,
    ):
        self.mode = str(mode or "flat").strip().lower()
        if self.mode not in ALLOWED_MODES:
            logger.warning(f"unknown mode '{self.mode}', fallback to 'flat'")
            self.mode = "flat"

        self.unit = float(unit)
        self.profit_stop = float(profit_stop)
        self.loss_cut = float(loss_cut)
        self.on_limit = "stop" if str(on_limit).lower() not in ("restart",) else "restart"
        self.state_path = state_path

        # セッション管理
        self.session_pnl: float = 0.0
        self.total_bets: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_ties: int = 0
        self.limit_reached: bool = False
        self.limit_reason: str = ""  # "profit" or "loss"

        # SEQ モード状態
        self.seq_level: int = 0  # SEQ 配列の index
        self.current_seq = self._resolve_seq()
        # セット長(7=標準 / 5=5ターン制)。全SMALL SEQで選択可。
        self.seq_set_size: int = 5 if int(seq_set_size or 7) == 5 else 7
        self._seq7_tracker: MaruBatsuTracker | None = None
        if self.mode in SMALL_SEQ_MODES:
            self._seq7_tracker = MaruBatsuTracker(
                chip_base=1.0,
                seq=list(self.current_seq),
                set_size=self.seq_set_size,
            )
            # 検証/運用用: どの型(階段)で動いているかをログで確認できるようにする。
            _shape = (os.getenv("BACOPY_SEQ_SHAPE", "") or "attack").strip().lower() or "attack"
            try:
                logger.info(
                    f"[SEQ-SHAPE] mode={self.mode} shape={_shape} set_size={self.seq_set_size} "
                    f"start={self.current_seq[0]} top={self.current_seq[-1]} steps={len(self.current_seq)}"
                )
            except Exception:
                pass

        # Martingale 状態
        self.loss_count: int = 0
        self.martingale_max_bet: float = float(
            os.getenv("BACOPY_MARTINGALE_MAX_BET", "0") or 0
        )

        # 1-2-3打法 状態 (0=1単位, 1=2単位, 2=3単位)
        self.b123_step: int = 0
        self.b123_prev_won: Optional[bool] = None  # 1回目の勝敗(2回目の分岐判定用)

        # Kelly(比例) 状態
        self.kelly_edge: float = float(os.getenv("BACOPY_KELLY_EDGE", "0.0061") or 0.0061)
        # 型は seq_shape を流用 (attack/balance/defense)
        self.kelly_shape: str = (os.getenv("BACOPY_SEQ_SHAPE", "") or "defense").strip().lower()
        if self.kelly_shape not in KELLY_SHAPE_MULT:
            self.kelly_shape = "defense"
        # フォールバック元本(ライブ残高未取得時): GUIが BACOPY_KELLY_BANKROLL で渡す
        self.kelly_bankroll_init: float = float(os.getenv("BACOPY_KELLY_BANKROLL", "1000") or 1000)
        self._kelly_live_bankroll: float = 0.0  # bot が set_bankroll() で更新
        # 安全上限: 残高の何割を超えてBETしないか(edge過大設定の暴走防止)
        self.kelly_max_frac: float = float(os.getenv("BACOPY_KELLY_MAX_FRAC", "0.05") or 0.05)
        if self.mode == "kelly":
            try:
                logger.info(
                    f"[KELLY] shape={self.kelly_shape} mult={KELLY_SHAPE_MULT[self.kelly_shape]} "
                    f"edge={self.kelly_edge*100:.3f}% f={KELLY_SHAPE_MULT[self.kelly_shape]*self.kelly_edge/KELLY_PAYOUT*100:.3f}%/残高 "
                    f"bankroll_init=${self.kelly_bankroll_init:.0f} max_frac={self.kelly_max_frac}"
                )
            except Exception:
                pass

        # 前回のベット額（結果反映まで保持）
        self._last_bet_amount: float = 0.0

        if self.state_path:
            self._load_state()

    def _resolve_seq(self) -> list[float]:
        m = self.mode
        # 攻撃型(=従来)の基準配列
        attack = {
            "small02": SEQ_SMALL02, "small06": SEQ_SMALL06, "small1": SEQ_SMALL1,
            "small2": SEQ_SMALL2, "small3": SEQ_SMALL3, "small6": SEQ_SMALL6,
            "small10": SEQ_SMALL10, "small30": SEQ_SMALL30,
        }.get(m)
        if attack is None:
            return [1.0]
        attack = list(attack)
        # ── 型(shape)切替: balance=CAND_A / defense=CAND_B を開始額で比例展開 ──
        # 既定 attack は従来挙動のまま(ゼロ回帰)。env `BACOPY_SEQ_SHAPE` で切替。
        shape = (os.getenv("BACOPY_SEQ_SHAPE", "") or "").strip().lower()
        if m in SMALL_SEQ_MODES and shape in ("balance", "a", "bal", "cand_a"):
            start = attack[0]
            return [round(x * start, 4) for x in SEQ_SHAPE_BALANCE]
        if m in SMALL_SEQ_MODES and shape in ("defense", "defence", "b", "def", "cand_b"):
            start = attack[0]
            return [round(x * start, 4) for x in SEQ_SHAPE_DEFENSE]
        return attack

    # ── ベット計算 ──────────────────────────────────────────────

    def _compute_next_bet(self) -> float:
        """次のベット額を計算する（状態を変更しない純粋な計算）。"""
        if self.limit_reached:
            return 0.0
        if self.mode == "flat":
            return self.unit
        elif self.mode == "martingale":
            amount = self.unit * (2 ** self.loss_count)
            if self.martingale_max_bet > 0:
                amount = min(amount, self.martingale_max_bet)
            if self.loss_cut > 0:
                # 損切り残額を超えるベットは行わない
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "dalembert":
            # D'Alembert: 負けで +1 unit, 勝ちで -1 unit。loss_count を段数に使う。
            # 例 unit=2: 2 -> 4 -> 6 -> 8 -> (勝) 6 -> 4 ...
            amount = self.unit * (self.loss_count + 1)
            if self.loss_cut > 0:
                # 損切り残額を超えるベットは行わない
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "bet123":
            # 1-2-3打法: 段数は apply_result の状態機械が管理
            amount = self.unit * (self.b123_step + 1)
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "kelly":
            # Kelly(比例): bet = 型係数 × (edge/payout) × 現在残高。
            # 残高 = ライブ残高(bot が set_bankroll)優先, 無ければ 設定元本+session_pnl。
            bank = self._kelly_live_bankroll if self._kelly_live_bankroll > 0 else (
                self.kelly_bankroll_init + self.session_pnl
            )
            if bank <= 0:
                return 0.0
            mult = KELLY_SHAPE_MULT.get(self.kelly_shape, 0.25)
            f = mult * (self.kelly_edge / KELLY_PAYOUT)
            amount = f * bank
            amount = min(amount, self.kelly_max_frac * bank)  # 安全上限
            # チップ最小単位($0.2)に丸める
            amount = round(amount / KELLY_CHIP) * KELLY_CHIP
            if amount < KELLY_CHIP:
                amount = KELLY_CHIP
            if amount > bank:
                amount = bank
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        else:
            seq = self.current_seq
            if self._seq7_tracker is not None:
                level = min(self._seq7_tracker.current_unit_idx, len(seq) - 1)
                self.seq_level = level
            else:
                level = min(self.seq_level, len(seq) - 1)
            return seq[level]

    def set_bankroll(self, balance) -> None:
        """Kelly用: bot が読んだライブ残高を渡す。0以下/不正は無視(フォールバック維持)。"""
        try:
            b = float(balance)
            if b > 0:
                self._kelly_live_bankroll = b
        except Exception:
            pass

    def next_bet(self, side: str = "P") -> float:
        """次のベット額を計算し _last_bet_amount を更新する。"""
        amount = self._compute_next_bet()
        if amount > 0:
            self._last_bet_amount = amount
        return amount

    def apply_result(self, won: bool | None, side: str = "P") -> None:
        """ベット結果を反映する。

        Args:
            won: True=勝ち, False=負け, None=TIE
            side: "P" or "B" — BANKER 勝利時は 5% コミッションを適用
        """
        if self.limit_reached:
            return

        amount = self._last_bet_amount
        self.total_bets += 1

        if won is None:
            # TIE → PnL 変動なし、SEQ/Martingale は現状維持
            self.total_ties += 1
            return

        if won:
            commission = BANKER_COMMISSION if str(side).upper() in ("B", "BANKER") else 1.0
            self.total_wins += 1
            self.session_pnl += amount * commission
            # SMALL系は受け子GUIと同じ7ターン管理で進行
            if self._seq7_tracker is not None:
                self._seq7_tracker.add_result("player")
                self.seq_level = self._seq7_tracker.current_unit_idx
            # 従来SEQ: 勝ったら先頭に戻る
            elif self.mode not in ("flat", "martingale", "dalembert", "bet123", "kelly"):
                self.seq_level = 0
            # Martingale: リセット / D'Alembert: 1段下げる(下限0)
            if self.mode == "dalembert":
                self.loss_count = max(0, self.loss_count - 1)
            else:
                self.loss_count = 0
        else:
            self.total_losses += 1
            self.session_pnl -= amount
            # SMALL系は受け子GUIと同じ7ターン管理で進行
            if self._seq7_tracker is not None:
                self._seq7_tracker.add_result("banker")
                self.seq_level = self._seq7_tracker.current_unit_idx
            # 従来SEQ: レベル進行
            elif self.mode not in ("flat", "martingale", "dalembert", "bet123", "kelly"):
                self.seq_level = min(self.seq_level + 1, len(self.current_seq) - 1)
            # Martingale / D'Alembert: 1段上げる
            if self.mode in ("martingale", "dalembert"):
                self.loss_count += 1

        # 1-2-3打法: 1回目→必ず2単位 / 2回目→1回目と同結果なら3単位・割れたらリセット / 3回目→必ずリセット
        if self.mode == "bet123":
            if self.b123_step == 0:
                self.b123_prev_won = won
                self.b123_step = 1
            elif self.b123_step == 1:
                self.b123_step = 2 if self.b123_prev_won == won else 0
            else:
                self.b123_step = 0
            self.seq_level = self.b123_step  # 表示用ミラー

        # 利確 / 損切判定
        self._check_limits()

        # state 保存
        if self.state_path:
            self._save_state()

    def _check_limits(self) -> None:
        if self.profit_stop > 0 and self.session_pnl >= self.profit_stop:
            self.limit_reached = True
            self.limit_reason = "profit"
            logger.info(
                f"[money] profit_stop ${self.profit_stop} reached "
                f"(session_pnl=${self.session_pnl:+.2f})"
            )
            if self.on_limit == "restart":
                self._reset_session()

        elif self.loss_cut > 0 and self.session_pnl <= -self.loss_cut:
            self.limit_reached = True
            self.limit_reason = "loss"
            logger.info(
                f"[money] loss_cut ${self.loss_cut} reached "
                f"(session_pnl=${self.session_pnl:+.2f})"
            )
            if self.on_limit == "restart":
                self._reset_session()

    def _reset_session(self) -> None:
        """セッションリセット（on_limit="restart" 時）。"""
        self.session_pnl = 0.0
        self.seq_level = 0
        self.loss_count = 0
        self.b123_step = 0
        self.b123_prev_won = None
        self.limit_reached = False
        self.limit_reason = ""
        logger.info("[money] session reset (restart mode)")

    # ── 状態取得 ────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        nt = self.total_wins + self.total_losses
        return self.total_wins / nt * 100 if nt else 0.0

    def status_dict(self) -> dict:
        seq_turn = (len(self._seq7_tracker.current_turns) + 1) if self._seq7_tracker else None
        seq_overshoot = self._seq7_tracker.prev_overshoot if self._seq7_tracker else None
        seq7_sets = [s.__dict__ for s in self._seq7_tracker.sets] if self._seq7_tracker else []
        seq7_current_turns = list(self._seq7_tracker.current_turns) if self._seq7_tracker else []
        return {
            "mode": self.mode,
            "unit": self.unit,
            "profit_stop": self.profit_stop,
            "loss_cut": self.loss_cut,
            "on_limit": self.on_limit,
            "session_pnl": round(self.session_pnl, 2),
            "total_bets": self.total_bets,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_ties": self.total_ties,
            "win_rate": round(self.win_rate, 1),
            "seq_level": self.seq_level,
            "seq_turn": seq_turn,
            "seq_set_size": self.seq_set_size,
            "seq_overshoot": seq_overshoot,
            "seq7_sets": seq7_sets,
            "seq7_current_turns": seq7_current_turns,
            "loss_count": self.loss_count,
            "martingale_max_bet": self.martingale_max_bet,
            "limit_reached": self.limit_reached,
            "limit_reason": self.limit_reason,
            "next_bet": round(self._compute_next_bet(), 2),
            "kelly_shape": self.kelly_shape if self.mode == "kelly" else None,
            "kelly_bankroll": (round(self._kelly_live_bankroll, 2) if self._kelly_live_bankroll > 0
                               else round(self.kelly_bankroll_init + self.session_pnl, 2)) if self.mode == "kelly" else None,
            "kelly_edge": self.kelly_edge if self.mode == "kelly" else None,
        }

    # ── 状態保存 ────────────────────────────────────────────────

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.write_text(
                json.dumps(
                    {
                        "mode": self.mode,
                        "unit": self.unit,
                        "profit_stop": self.profit_stop,
                        "loss_cut": self.loss_cut,
                        "on_limit": self.on_limit,
                        "session_pnl": round(self.session_pnl, 2),
                        "total_bets": self.total_bets,
                        "total_wins": self.total_wins,
                        "total_losses": self.total_losses,
                        "total_ties": self.total_ties,
                        "seq_level": self.seq_level,
                        "seq_set_size": self.seq_set_size,
                        "seq7_sets": [s.__dict__ for s in (self._seq7_tracker.sets if self._seq7_tracker else [])],
                        "seq7_current_turns": list(self._seq7_tracker.current_turns) if self._seq7_tracker else [],
                        "loss_count": self.loss_count,
                        "b123_step": self.b123_step,
                        "b123_prev_won": self.b123_prev_won,
                        "limit_reached": self.limit_reached,
                        "limit_reason": self.limit_reason,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"money state save failed: {e}")

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            self.session_pnl = float(s.get("session_pnl", 0.0))
            self.total_bets = int(s.get("total_bets", 0))
            self.total_wins = int(s.get("total_wins", 0))
            self.total_losses = int(s.get("total_losses", 0))
            self.total_ties = int(s.get("total_ties", 0))
            self.seq_level = int(s.get("seq_level", 0))
            if self._seq7_tracker is not None:
                raw_sets = s.get("seq7_sets", []) or []
                parsed_sets: list[SetData] = []
                if isinstance(raw_sets, list):
                    for item in raw_sets:
                        if isinstance(item, dict):
                            try:
                                parsed_sets.append(SetData(**item))
                            except Exception:
                                continue
                self._seq7_tracker.sets = parsed_sets
                # セット長が前回保存時と違う場合は進行中セットを破棄して仕切り直し
                # (確定済みセット=overshoot/段位置は数値で保存されているため引き継がれる)
                saved_set_size = int(s.get("seq_set_size", 7) or 7)
                raw_turns = s.get("seq7_current_turns", []) or []
                if saved_set_size == self.seq_set_size and isinstance(raw_turns, list):
                    self._seq7_tracker.current_turns = [t for t in raw_turns if t in ("O", "X")]
                elif raw_turns:
                    logger.info(
                        f"[money] seq set size changed {saved_set_size}->{self.seq_set_size}: "
                        f"discard in-progress turns ({len(raw_turns)})"
                    )
                self.seq_level = self._seq7_tracker.current_unit_idx
            self.loss_count = int(s.get("loss_count", 0))
            self.b123_step = max(0, min(2, int(s.get("b123_step", 0))))
            _bpw = s.get("b123_prev_won", None)
            self.b123_prev_won = _bpw if isinstance(_bpw, bool) else None
            self.limit_reached = bool(s.get("limit_reached", False))
            self.limit_reason = str(s.get("limit_reason", ""))
            # 設定は復元しない（GUI の値が正）
        except Exception as e:
            logger.debug(f"money state load failed: {e}")
