"""Dual-Line Bet 資金管理モジュール

BetManager:
  - フラット ベット（単一ユニット）
  - SMALL SEQ シリーズ (0.2/1/3/6 start)
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

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

SEQ_SMALL6 = [
    6, 10, 14, 24, 34, 46, 60, 76, 94, 114, 136, 166, 194, 224, 256,
    290, 330, 380, 440, 510, 600, 720, 840, 1000, 1200, 1400, 1600, 1800, 2000,
]

BET_MODES = {
    "flat": "1 unit flat",
    "small02": "SMALL SEQ $0.20 start",
    "small1": "SMALL SEQ $1 start",
    "small3": "SMALL SEQ $3 start",
    "small6": "SMALL SEQ $6 start",
    "martingale": "pure Martingale",
}
ALLOWED_MODES = set(BET_MODES.keys())

BANKER_COMMISSION = 0.95


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

        # Martingale 状態
        self.loss_count: int = 0

        # 前回のベット額（結果反映まで保持）
        self._last_bet_amount: float = 0.0

        if self.state_path:
            self._load_state()

    def _resolve_seq(self) -> list[float]:
        m = self.mode
        if m == "small02":
            return list(SEQ_SMALL02)
        if m == "small1":
            return list(SEQ_SMALL1)
        if m == "small3":
            return list(SEQ_SMALL3)
        if m == "small6":
            return list(SEQ_SMALL6)
        return [1.0]

    # ── ベット計算 ──────────────────────────────────────────────

    def _compute_next_bet(self) -> float:
        """次のベット額を計算する（状態を変更しない純粋な計算）。"""
        if self.limit_reached:
            return 0.0
        if self.mode == "flat":
            return self.unit
        elif self.mode == "martingale":
            return self.unit * (2 ** self.loss_count)
        else:
            seq = self.current_seq
            level = min(self.seq_level, len(seq) - 1)
            return seq[level]

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
            # SEQ: レベルリセット（勝ったら先頭に戻る）
            self.seq_level = 0
            # Martingale: リセット
            self.loss_count = 0
        else:
            self.total_losses += 1
            self.session_pnl -= amount
            # SEQ: レベル進行
            if self.mode != "flat":
                self.seq_level = min(self.seq_level + 1, len(self.current_seq) - 1)
            # Martingale: 進行
            if self.mode == "martingale":
                self.loss_count += 1

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
        self.limit_reached = False
        self.limit_reason = ""
        logger.info("[money] session reset (restart mode)")

    # ── 状態取得 ────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        nt = self.total_wins + self.total_losses
        return self.total_wins / nt * 100 if nt else 0.0

    def status_dict(self) -> dict:
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
            "loss_count": self.loss_count,
            "limit_reached": self.limit_reached,
            "limit_reason": self.limit_reason,
            "next_bet": round(self._compute_next_bet(), 2),
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
                        "loss_count": self.loss_count,
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
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.session_pnl = float(s.get("session_pnl", 0.0))
            self.total_bets = int(s.get("total_bets", 0))
            self.total_wins = int(s.get("total_wins", 0))
            self.total_losses = int(s.get("total_losses", 0))
            self.total_ties = int(s.get("total_ties", 0))
            self.seq_level = int(s.get("seq_level", 0))
            self.loss_count = int(s.get("loss_count", 0))
            self.limit_reached = bool(s.get("limit_reached", False))
            self.limit_reason = str(s.get("limit_reason", ""))
            # 設定は復元しない（GUI の値が正）
        except Exception as e:
            logger.debug(f"money state load failed: {e}")
