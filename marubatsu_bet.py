"""〇❌ロジック × 自動BETセッション

⚠️  SERVER-ONLY — DO NOT SHIP TO CLIENT ⚠️
このモジュールは local-fallback 用 MaruBatsuBetSession で、
VPS に到達できない開発用途のみ使用します。marubatsu_strategy (SEQ 含む)
に依存するため client distribution には含めません (.dist_excludes 参照)。

Japanese Speed Baccarat A テーブルに入場し、
〇❌ロジック (SEQ, OS, slashed, next_unit_idx) に従って
Player側にBETする。

設計:
  - 常にPlayer=〇にBET (Banker=✕はBETしない、結果のみ記録)
  - 7ハンド=1セット、セット確定でSEQ昇降
  - +50 chip到達で利確リセット
  - 損切りchip到達で損切りリセット
  - Tieは〇❌に影響なし (BETは返還)
  - シュー交換時: 途中ターンは破棄 (BET済み分は損益確定済み)
"""
import time
import json
import logging
from pathlib import Path

from marubatsu_strategy import MaruBatsuTracker, SEQ, SEQ_COUNTER, SET_SIZE_COUNTER, SetData
from notify import TelegramNotifier

logger = logging.getLogger("baccarat.marubatsu_bet")

PROFIT_STOP = 50  # default, overridable per session
DEFAULT_LOSS_CUT = 200


class MaruBatsuBetSession:
    """テーブル内で〇❌ロジックに従いBETを実行するセッション"""

    def __init__(
        self,
        executor,
        notifier: TelegramNotifier,
        chip_base: float = 1.0,
        loss_cut: int = DEFAULT_LOSS_CUT,
        dry_run: bool = False,
        profit_stop: int = PROFIT_STOP,
        resume: bool = True,
        counter_mode: bool = False,
        counter_set_size: int | None = None,
    ):
        self.executor = executor
        self.notifier = notifier
        self.chip_base = chip_base
        self.loss_cut = loss_cut
        self.profit_stop = profit_stop
        self.dry_run = dry_run
        self.resume = resume
        self.counter_mode = counter_mode

        if counter_mode:
            set_size = counter_set_size or SET_SIZE_COUNTER
            self.tracker = MaruBatsuTracker(chip_base=chip_base, seq=SEQ_COUNTER, set_size=set_size)
            self._active_seq = SEQ_COUNTER
        else:
            self.tracker = MaruBatsuTracker(chip_base=chip_base)
            self._active_seq = SEQ
        self.session_count = 0

        self.total_bets = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_ties = 0

        # --- 残高スナップショット方式のPNL追跡 ---
        # session_open_balance: START時 or 利確/損切直後の残高を記録。
        #   セッションPNL = current_balance - session_open_balance
        # daily_open: { "date": "YYYY-MM-DD", "balance": float } 形式。
        #   その日の最初に観測した残高を記録。日付変更で更新。
        #   デイリーPNL = current_balance - daily_open["balance"]
        # current_balance + last_balance_at: Vercel cron による日次 settle で
        #   GUI 停止中でも現在残高を参照できるよう保存。
        self.session_open_balance: float | None = None
        self.daily_open: dict = {"date": None, "balance": None}
        self.current_balance: float | None = None
        self.last_balance_at: str | None = None  # ISO8601 UTC

        # Separate state files for dry run vs live
        if dry_run:
            self.state_path = Path(__file__).parent / "state_marubatsu_bet_dry.json"
        else:
            self.state_path = Path(__file__).parent / "state_marubatsu_bet.json"

        # Resume behavior controlled by user choice (Continue/Reset dialog)
        if self.resume:
            self._load_state()
        else:
            try:
                if self.state_path.exists():
                    self.state_path.unlink()
                    logger.info(f"状態リセット: {self.state_path.name} を削除")
            except Exception as e:
                logger.warning(f"状態ファイル削除失敗: {e}")

    # === 状態保存/復元 ===

    def to_state_dict(self) -> dict:
        return {
            "chip_base": self.chip_base,
            "profit_stop": self.profit_stop,
            "loss_cut": self.loss_cut,
            "session_open_balance": self.session_open_balance,
            "daily_open": dict(self.daily_open) if self.daily_open else {"date": None, "balance": None},
            "current_balance": self.current_balance,
            "last_balance_at": self.last_balance_at,
            "sets": [
                {
                    "set_index": s.set_index,
                    "results": s.results,
                    "wins": s.wins,
                    "losses": s.losses,
                    "overshoot": s.overshoot,
                    "slashed": s.slashed,
                    "used_unit_idx": s.used_unit_idx,
                    "next_unit_idx": s.next_unit_idx,
                    "set_profit": s.set_profit,
                    "cumulative_profit": s.cumulative_profit,
                }
                for s in self.tracker.sets
            ],
            "current_turns": self.tracker.current_turns,
            "total_o": self.tracker.total_o,
            "total_x": self.tracker.total_x,
            "session_count": self.session_count,
            "total_bets": self.total_bets,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_ties": self.total_ties,
        }

    def apply_state_dict(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        chip_base = state.get("chip_base")
        if isinstance(chip_base, (int, float)) and chip_base > 0:
            self.chip_base = float(chip_base)
            self.tracker.chip_base = float(chip_base)
        # profit_stop / loss_cut はGUI設定が常に優先。
        # Supabase復元で上書きしない。
        self.tracker.sets.clear()
        for sd in state.get("sets", []):
            try:
                self.tracker.sets.append(SetData(**sd))
            except Exception:
                pass
        turns = state.get("current_turns")
        if turns is None:
            turns_display = state.get("turns_display", "")
            if isinstance(turns_display, str):
                turns = list(turns_display)
        restored_turns = list(turns) if isinstance(turns, list) else []
        # set_size を超えるターンは前回のset_sizeが異なる場合のゴミ → クリア
        if len(restored_turns) >= self.tracker.set_size:
            logger.warning(f"Restored turns ({len(restored_turns)}) >= set_size ({self.tracker.set_size}) — clearing")
            restored_turns = []
        self.tracker.current_turns = restored_turns
        self.tracker.total_o = state.get("total_o", 0) or 0
        self.tracker.total_x = state.get("total_x", 0) or 0
        self.session_count = state.get("session_count", 0) or 0
        self.total_bets = state.get("total_bets", 0) or 0
        self.total_wins = state.get("total_wins", 0) or 0
        self.total_losses = state.get("total_losses", 0) or 0
        self.total_ties = state.get("total_ties", 0) or 0
        # --- 残高スナップショット復元 ---
        sob = state.get("session_open_balance")
        if isinstance(sob, (int, float)) and sob > 0:
            self.session_open_balance = float(sob)
        do = state.get("daily_open")
        if isinstance(do, dict):
            d_date = do.get("date")
            d_bal = do.get("balance")
            if isinstance(d_date, str) and isinstance(d_bal, (int, float)) and d_bal > 0:
                self.daily_open = {"date": d_date, "balance": float(d_bal)}
        cb = state.get("current_balance")
        if isinstance(cb, (int, float)) and cb > 0:
            self.current_balance = float(cb)
        lba = state.get("last_balance_at")
        if isinstance(lba, str) and lba:
            self.last_balance_at = lba
        self._save_state()

    def _save_state(self):
        state = self.to_state_dict()
        self.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            for sd in state.get("sets", []):
                self.tracker.sets.append(SetData(**sd))
            _lt = state.get("current_turns", [])
            if len(_lt) >= self.tracker.set_size:
                logger.warning(f"Loaded turns ({len(_lt)}) >= set_size ({self.tracker.set_size}) — clearing")
                _lt = []
            self.tracker.current_turns = _lt
            self.tracker.total_o = state.get("total_o", 0)
            self.tracker.total_x = state.get("total_x", 0)
            self.session_count = state.get("session_count", 0)
            self.total_bets = state.get("total_bets", 0)
            self.total_wins = state.get("total_wins", 0)
            self.total_losses = state.get("total_losses", 0)
            self.total_ties = state.get("total_ties", 0)
            # --- 残高スナップショット復元 (ローカル state ファイル) ---
            sob = state.get("session_open_balance")
            if isinstance(sob, (int, float)) and sob > 0:
                self.session_open_balance = float(sob)
            do = state.get("daily_open")
            if isinstance(do, dict):
                d_date = do.get("date")
                d_bal = do.get("balance")
                if isinstance(d_date, str) and isinstance(d_bal, (int, float)) and d_bal > 0:
                    self.daily_open = {"date": d_date, "balance": float(d_bal)}
            logger.info(
                f"状態復元: {len(self.tracker.sets)}セット, "
                f"ターン{len(self.tracker.current_turns)}/{self.tracker.set_size}, "
                f"累計{self.tracker.cumulative_profit:+d}chip, "
                f"session_open=${self.session_open_balance} daily_open={self.daily_open}"
            )
        except Exception as e:
            logger.warning(f"状態復元失敗: {e}")

    # === BET額計算 ===

    def get_bet_amount(self) -> float:
        """現在のSEQ[idx] × chip_base で実際のBET額($)を算出"""
        unit = self._active_seq[self.tracker.current_unit_idx] if self.tracker.current_unit_idx < len(self._active_seq) else self._active_seq[-1]
        return unit * self.chip_base

    # === セッション制御 ===

    def effective_profit(self) -> int:
        """セット完了損益 + 進行中セットの暫定損益 (chip単位)"""
        cp = self.tracker.cumulative_profit
        # Add provisional profit from in-progress set
        turns = self.tracker.current_turns
        if turns:
            wins = turns.count("O")
            losses = turns.count("X")
            unit = self._active_seq[self.tracker.current_unit_idx] if self.tracker.current_unit_idx < len(self._active_seq) else self._active_seq[-1]
            cp += (wins - losses) * unit
        return cp

    def should_reset(self) -> bool:
        """利確 or 損切り条件をチェック (進行中セット含む)"""
        cp = self.effective_profit()
        if cp >= self.profit_stop:
            logger.info(f"利確条件到達: effective_profit={cp} >= profit_stop={self.profit_stop}")
            return True
        if cp <= -self.loss_cut:
            logger.info(f"損切条件到達: effective_profit={cp} <= -loss_cut={-self.loss_cut}")
            return True
        return False

    def reset_session(self, reason: str, actual_amount: float | None = None, balance: float | None = None):
        """セッションリセット (利確/損切り)"""
        cp = self.effective_profit()
        self.session_count += 1
        money = cp * self.chip_base
        if balance is None:
            balance = self.executor.get_balance() if not self.dry_run else 0

        display_amount = actual_amount if actual_amount is not None else money

        is_profit = reason in ("利確", "profit")
        emoji = "🎉" if is_profit else "🛑"
        label = "TARGET REACHED" if is_profit else "LIMIT REACHED"
        msg = (
            f"{emoji} {label} #{self.session_count}\n"
            f"${display_amount:+.2f} | {self.total_wins}W/{self.total_losses}L | ${balance:.2f}"
        )
        logger.info(msg)
        self.notifier.send(msg)

        self.tracker.sets.clear()
        self.tracker.current_turns.clear()
        self._telegram_pnl = 0.0
        self._save_state()

    # === メインBETループ ===

    def run_round(self, running_flag, side: str = "player") -> dict:
        """1ラウンド分のBET→結果取得→〇❌記録を実行。

        Returns: {
            "action": "bet" | "observe" | "exit",
            "result": "player" | "banker" | "tie" | None,
            "won": bool | None,
            "bet_amount": float,
            "completed_set": SetData | None,
            "should_reset": bool,
        }
        """
        if not running_flag():
            return {"action": "exit", "reason": "stop_requested"}

        if not self.executor.check_and_dismiss_error():
            logger.warning("エラーダイアログ検出 → セッション中断")
            return {"action": "exit", "reason": "error_dialog"}

        # BETフェーズ待ち
        is_first = (self.total_bets == 0 and len(self.tracker.current_turns) == 0)
        _phase_timeout = 180 if is_first else 120
        _phase_t0 = time.time()
        if not self.executor.wait_for_betting_phase(timeout=_phase_timeout, skip_round=is_first):
            if not self.executor.check_and_dismiss_error():
                return {"action": "exit", "reason": "error_dialog_after_phase"}
            _elapsed = time.time() - _phase_t0
            logger.warning(f"BETフェーズ待ちタイムアウト ({_elapsed:.1f}s/{_phase_timeout}s)")
            return {"action": "exit", "reason": "phase_timeout", "elapsed": _elapsed, "timeout": _phase_timeout}

        # 残高チェック
        bet_amount = self.get_bet_amount()
        if not self.dry_run:
            balance = self.executor.get_balance()
            if balance < bet_amount:
                logger.error(f"残高不足: ${balance:.2f} < ${bet_amount:.2f}")
                self.notifier.send(
                    f"⚠️ 残高不足!\n"
                    f"必要: ${bet_amount:.2f} (SEQ[{self.tracker.current_unit_idx}]={self._active_seq[min(self.tracker.current_unit_idx, len(self._active_seq)-1)]})\n"
                    f"残高: ${balance:.2f}"
                )
                return {"action": "exit", "reason": "insufficient_balance"}

        # BET実行 (デフォルトはPlayer)
        if side not in ("player", "banker"):
            side = "player"
        unit_idx = self.tracker.current_unit_idx
        unit = self._active_seq[min(unit_idx, len(self._active_seq)-1)]
        turn_num = len(self.tracker.current_turns) + 1
        set_size = self.tracker.set_size

        logger.info(
            f"BET: ${bet_amount:.0f} {side.upper()} "
            f"(SEQ[{unit_idx}]={unit}, Set#{self.tracker.current_set_index} Turn{turn_num}/{set_size})"
        )

        if not self.executor.place_bet(side, bet_amount):
            actual_total = self.executor._get_total_bet()
            if actual_total > 0:
                # 部分BET: 置かれた額で続行
                logger.warning(f"部分BET検出: 計画${bet_amount:.0f} → 実際${actual_total:.2f}")
                bet_amount = actual_total
            else:
                # BET完全失敗 — 観戦モード（Player固定のロジックのみシーケンス維持）
                logger.warning("BET完全失敗 — 観戦モード")
                result_info = self.executor.wait_for_result(timeout=90, bet_amount=0)
                if result_info and result_info.get("result") not in (None, "unknown"):
                    obs_result = result_info["result"]
                    if side == "player" and obs_result != "tie":
                        completed_set = self.tracker.add_result(obs_result)
                        self._save_state()
                        logger.info(f"観戦記録: {obs_result.upper()} (BET $0)")
                        if completed_set:
                            self._notify_set_complete(completed_set, result_info.get("balance", 0))
                return {"action": "bet", "result": None, "won": None, "bet_amount": 0,
                        "completed_set": None, "should_reset": self.should_reset()}
        else:
            # 部分BETが発生した可能性 → 実際にテーブルに置かれた額で上書き
            actual_total = self.executor._get_total_bet()
            if actual_total > 0 and abs(actual_total - bet_amount) > 0.5:
                logger.warning(f"部分BET検出: 計画${bet_amount:.0f} → 実際${actual_total:.2f}")
                bet_amount = actual_total

        self.total_bets += 1

        # 結果待ち
        _result_timeout = 90
        _result_t0 = time.time()
        result_info = self.executor.wait_for_result(timeout=_result_timeout, bet_amount=bet_amount)
        _result_elapsed = time.time() - _result_t0
        if not result_info or not result_info.get("result"):
            logger.error(f"結果取得失敗 ({_result_elapsed:.1f}s/{_result_timeout}s)")
            return {"action": "exit", "reason": "result_timeout", "elapsed": _result_elapsed, "timeout": _result_timeout}

        result = result_info["result"]
        balance = result_info.get("balance", 0)

        # Tie処理
        if result == "tie":
            self.total_ties += 1
            logger.info(f"Tie — BET返還、〇❌に影響なし (残高${balance:.2f})")
            self._save_state()
            return {
                "action": "bet",
                "result": "tie",
                "won": None,
                "bet_amount": bet_amount,
                "completed_set": None,
                "should_reset": False,
            }

        # 〇❌に記録 (勝敗ベース)
        won = (result == side)
        if won:
            self.total_wins += 1
        else:
            self.total_losses += 1

        # ★ add_result前にターン情報を取得 (completeでクリアされるため)
        pre_turns = list(self.tracker.current_turns) + ["O" if won else "X"]
        pre_turn_count = len(pre_turns)
        pre_wins = sum(1 for t in pre_turns if t == "O")
        pre_losses = pre_turn_count - pre_wins

        # NOTE: Tracker は Player=〇 / Banker=✕ のマッピングだが、counter用途では
        # 〇=勝ち / ✕=負け として使いたい。そのため勝敗で player/banker を擬似入力する。
        completed_set = self.tracker.add_result("player" if won else "banker")

        mark = "〇" if won else "✕"
        logger.info(
            f"結果: {result.upper()} → {mark} "
            f"(残高${balance:.2f})"
        )

        import random as _rnd
        # Encrypted status: Turn=cycle(prefix+letter), W:L=prefix+nums, OS=prefix+num
        _cp = _rnd.choice("CDEFG")
        _turn_letter = chr(ord('A') + pre_turn_count - 1)  # 1→A, 2→B, 3→C, 4→D, 5→E
        _wp = _rnd.choice("QRSTM")
        _lp = _rnd.choice("QRSTM")
        _vp = _rnd.choice("UVWXY")
        _os = self.tracker.prev_overshoot
        # セッションPNL: 残高スナップショット方式（GUIのmoney_pnl_actualと完全一致）
        # session_open_balance が未設定なら BET結果のみで集計するフォールバック
        if self.session_open_balance is not None and balance is not None:
            _spnl = balance - self.session_open_balance
        else:
            if won:
                _round_pnl = bet_amount * (0.95 if side == "banker" else 1.0)
            else:
                _round_pnl = -bet_amount
            if not hasattr(self, '_telegram_pnl'):
                self._telegram_pnl = 0.0
            self._telegram_pnl += _round_pnl
            _spnl = self._telegram_pnl
        _spnl_sign = "+" if _spnl >= 0 else ""
        self.notifier.send(
            f"{'WIN' if won else 'LOSE'} | {result.upper()} | ${bet_amount:.2f}\n"
            f"{_cp}{_turn_letter} {_wp}{pre_wins}{_lp}{pre_losses} {_vp}{_os} | {_spnl_sign}${_spnl:.2f} | ${balance:.2f}"
        )

        need_reset = self.should_reset()
        self._save_state()

        return {
            "action": "bet",
            "result": result,
            "won": won,
            "bet_amount": bet_amount,
            "completed_set": completed_set,
            "should_reset": need_reset,
            "pre_turn_count": pre_turn_count,
            "pre_wins": pre_wins,
            "pre_losses": pre_losses,
        }

    def _notify_set_complete(self, new_set: SetData, balance: float):
        """セット確定時のTelegram通知"""
        marks = new_set.results.replace("O", "〇").replace("X", "✕")
        diff = new_set.wins - new_set.losses
        outcome = "勝ち越し 📈" if diff > 0 else "負け越し 📉"
        money_set = new_set.set_profit * self.chip_base
        money_cum = new_set.cumulative_profit * self.chip_base

        # Telegram通知は送らない (ログのみ)
        logger.info(
            f"Set #{new_set.set_index} 確定: {new_set.results} "
            f"{new_set.wins}/{new_set.losses} "
            f"P/L:{new_set.cumulative_profit:+d}"
        )

    def handle_shoe_change(self):
        """シュー交換時の処理 — 途中ターンを破棄"""
        if self.tracker.current_turns:
            partial = "".join("〇" if t == "O" else "✕" for t in self.tracker.current_turns)
            logger.info(f"シュー交換 — 途中ターン破棄: {partial}")
            self.notifier.send(
                f"⚠️ シュー交換\n"
                f"途中ターン破棄: {partial} ({len(self.tracker.current_turns)}/{self.tracker.set_size})\n"
                f"累計損益: {self.tracker.cumulative_profit:+d} chip"
            )
            self.tracker.current_turns.clear()
            self._save_state()

    def get_summary(self) -> dict:
        """セッションサマリー"""
        return {
            "session_count": self.session_count,
            "sets": len(self.tracker.sets),
            "current_turn": len(self.tracker.current_turns),
            "cumulative_profit": self.tracker.cumulative_profit,
            "cumulative_money": self.tracker.cumulative_profit * self.chip_base,
            "total_bets": self.total_bets,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_ties": self.total_ties,
            "current_unit": self._active_seq[min(self.tracker.current_unit_idx, len(self._active_seq)-1)],
            "current_unit_idx": self.tracker.current_unit_idx,
        }
