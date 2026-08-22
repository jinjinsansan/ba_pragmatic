"""LAPLACE Logic API HTTP client.

Provides a drop-in replacement for MaruBatsuBetSession that delegates
ALL logic decisions and state management to the VPS-hosted API server.

The local side only handles:
  - Camoufox browser / Stake navigation (scraper.py)
  - BetExecutor physical BET placement
  - Telegram notifications (via CompositeNotifier)

This class exposes the same interface as MaruBatsuBetSession so that
agent_api.py can swap implementations with a single flag.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from notify import TelegramNotifier


# =========================================================================
# ClientSetData — minimal data holder that mirrors the server-side
# completed-set payload. Storage only, no logic. All decisions about what
# a set looks like are made by the VPS; the client just receives values.
# =========================================================================


@dataclass
class ClientSetData:
    set_index: int
    results: str
    wins: int
    losses: int
    overshoot: int
    slashed: bool = False
    used_unit_idx: int = 0
    next_unit_idx: int = 0
    used_unit_chips: int = 0
    next_unit_chips: int = 0
    set_profit: int = 0
    cumulative_profit: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "ClientSetData":
        """Construct from a server response dict, tolerating missing fields."""
        return cls(
            set_index=d.get("set_index", 0),
            results=d.get("results", ""),
            wins=d.get("wins", 0),
            losses=d.get("losses", 0),
            overshoot=d.get("overshoot", 0),
            slashed=d.get("slashed", False),
            used_unit_idx=d.get("used_unit_idx", 0),
            next_unit_idx=d.get("next_unit_idx", 0),
            used_unit_chips=d.get("used_unit_chips", 0),
            next_unit_chips=d.get("next_unit_chips", 0),
            set_profit=d.get("set_profit", 0),
            cumulative_profit=d.get("cumulative_profit", 0),
        )

logger = logging.getLogger("baccarat.laplace_client")

DEFAULT_TIMEOUT = 10.0


class LaplaceApiError(RuntimeError):
    """Raised when the VPS API returns an error or is unreachable."""


# === BUILD_FINGERPRINT_START ===
# The dict below is rewritten per-user by scripts/build_client_dist.py at
# build time. Do not edit the values manually or the fingerprint audit
# will fail. Leaving the default "unbranded" markers is fine for dev.
_BUILD_INFO: dict = {
    "user_id": "unbranded",
    "build_id": "unbranded",
    "built_at": "unbranded",
    "channel": "dev",
}
# === BUILD_FINGERPRINT_END ===


class LaplaceClient:
    """Low-level HTTP client for the LAPLACE Logic API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        if api_key:
            self._session.headers.update({"Authorization": f"Bearer {api_key}"})
        # Fingerprint headers — identify this build in VPS logs. The values
        # are rewritten per-user at package build time (see L.7).
        self._session.headers.update(
            {
                "X-Client-Build-Id": str(_BUILD_INFO.get("build_id", "unbranded")),
                "X-Client-User": str(_BUILD_INFO.get("user_id", "unbranded")),
                "X-Client-Channel": str(_BUILD_INFO.get("channel", "dev")),
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, _retries: int = 1, **kwargs) -> dict:
        url = self._url(path)
        last_err = None
        for attempt in range(_retries + 1):
            try:
                resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                last_err = e
                if attempt < _retries:
                    logger.warning(f"API retry {attempt+1}/{_retries}: {method} {path}: {e}")
                    time.sleep(0.5)
                    continue
                raise LaplaceApiError(f"{method} {path}: {e}") from e
            if not resp.ok:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                if resp.status_code >= 500 and attempt < _retries:
                    logger.warning(f"API retry {attempt+1}/{_retries}: {method} {path} -> {resp.status_code}")
                    time.sleep(0.5)
                    continue
                raise LaplaceApiError(f"{method} {path} -> {resp.status_code}: {body}")
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        raise LaplaceApiError(f"{method} {path}: exhausted retries — {last_err}")

    # --- Endpoints ---

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def create_session(
        self,
        user_id: str,
        chip_base: float,
        profit_stop: int,
        loss_cut: int,
        resume: bool = True,
    ) -> dict:
        body = {
            "user_id": user_id,
            "chip_base": chip_base,
            "profit_stop": profit_stop,
            "loss_cut": loss_cut,
            "resume": resume,
        }
        return self._request("POST", "/api/sessions", json=body)

    def get_session(self, user_id: str) -> dict:
        return self._request("GET", f"/api/sessions/{user_id}")

    def update_session(
        self,
        user_id: str,
        chip_base: Optional[float] = None,
        profit_stop: Optional[int] = None,
        loss_cut: Optional[int] = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if chip_base is not None:
            body["chip_base"] = chip_base
        if profit_stop is not None:
            body["profit_stop"] = profit_stop
        if loss_cut is not None:
            body["loss_cut"] = loss_cut
        return self._request("PATCH", f"/api/sessions/{user_id}", json=body)

    def restore_session(self, user_id: str, state: dict) -> dict:
        return self._request(
            "POST",
            f"/api/sessions/{user_id}/restore",
            json={"state": state},
        )

    def decide(self, user_id: str) -> dict:
        # BETウィンドウ内で完了させるため短いタイムアウト (5秒)
        saved = self.timeout
        self.timeout = min(self.timeout, 5.0)
        try:
            return self._request("POST", f"/api/sessions/{user_id}/decide")
        finally:
            self.timeout = saved

    def submit_result(self, user_id: str, result: str) -> dict:
        return self._request(
            "POST", f"/api/sessions/{user_id}/result", json={"result": result}
        )

    def reset(self, user_id: str) -> dict:
        return self._request("POST", f"/api/sessions/{user_id}/reset")

    def shoe_change(self, user_id: str) -> dict:
        return self._request("POST", f"/api/sessions/{user_id}/shoe-change")

    def delete(self, user_id: str) -> dict:
        return self._request("DELETE", f"/api/sessions/{user_id}")

    # --- Table selector endpoints (L.2: logic moved to VPS) ---

    def select_table(
        self,
        user_id: str,
        configs: dict,
        players: dict,
        histories: dict,
        excluded_ids: Optional[list[str]] = None,
        fixed_name: Optional[str] = None,
        selector_config: Optional[dict] = None,
    ) -> dict:
        body = {
            "user_id": user_id,
            "configs": configs,
            "players": players,
            "histories": histories,
            "excluded_ids": excluded_ids or [],
            "fixed_name": fixed_name,
            "selector_config": selector_config or {},
        }
        return self._request("POST", "/api/select-table", json=body)

    def exit_check(self, table_id: str, players_count: int, history: list) -> dict:
        body = {
            "table_id": table_id,
            "players": players_count,
            "history": history,
        }
        return self._request("POST", "/api/exit-check", json=body)


# ======== Local shim objects that mimic marubatsu_strategy types ========
# This lets the existing agent_api.py code continue to read
# session.tracker.current_turns, session.tracker.sets, etc.


class _RemoteTracker:
    """Read-only shim that exposes the same attributes as MaruBatsuTracker."""

    def __init__(self, chip_base: float):
        self.chip_base = chip_base
        self.sets: list[ClientSetData] = []
        self.current_turns: list[str] = []
        self.total_o = 0
        self.total_x = 0
        self.current_unit_idx = 0
        self.current_unit_chips = 0  # chip count for current unit (from server)
        self.cumulative_profit = 0
        self.prev_overshoot = 0
        self.current_set_index = 1
        self.current_turn_number = 1

    def apply_state(self, state: dict) -> None:
        self.chip_base = state.get("chip_base", self.chip_base)
        self.sets = [ClientSetData.from_dict(sd) for sd in state.get("sets", [])]
        # State returns turns_display but not raw array; reconstruct from total turns
        # by using turns_display char-by-char (single char O/X)
        turns_display = state.get("turns_display", "")
        self.current_turns = list(turns_display)
        self.total_o = state.get("total_o", 0)
        self.total_x = state.get("total_x", 0)
        self.current_unit_idx = state.get("current_unit_idx", 0)
        self.current_unit_chips = state.get("current_unit", 0)  # server-resolved chip count
        self.cumulative_profit = state.get("cumulative_profit", 0)
        self.prev_overshoot = state.get("overshoot", 0)
        self.current_turn_number = len(self.current_turns) + 1
        self.current_set_index = len(self.sets) + 1


# ======== High-level wrapper compatible with MaruBatsuBetSession ========


class RemoteLaplaceSession:
    """Drop-in replacement for MaruBatsuBetSession backed by the VPS API.

    Only the logic + persistence + stats live on the VPS. The local side
    is responsible for:
      - Camoufox scraper / live WebSocket monitoring
      - executor.place_bet / wait_for_result
      - Telegram notifications
    """

    def __init__(
        self,
        executor,
        notifier: TelegramNotifier,
        chip_base: float = 1.0,
        loss_cut: int = 200,
        dry_run: bool = False,
        profit_stop: int = 50,
        resume: bool = True,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.executor = executor
        self.notifier = notifier
        self.chip_base = chip_base
        self.loss_cut = loss_cut
        self.profit_stop = profit_stop
        self.dry_run = dry_run
        self.resume = resume

        self.user_id = user_id or os.getenv("LAPLACE_USER", "dev-machine")
        self.api_url = api_url or os.getenv(
            "LAPLACE_API_URL", "http://127.0.0.1:8000"
        )
        self.api_key = api_key or os.getenv("LAPLACE_API_KEY", "")

        self.client = LaplaceClient(self.api_url, self.api_key)
        self.tracker = _RemoteTracker(chip_base=chip_base)

        # Local mirrors of state (updated from API responses)
        self.session_count = 0
        self.total_bets = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_ties = 0
        self._last_state: dict = {}

        # Create / resume remote session
        try:
            health = self.client.health()
            logger.info(f"LAPLACE API health: {health}")
        except LaplaceApiError as e:
            raise LaplaceApiError(
                f"LAPLACE API unreachable at {self.api_url} (tunnel open?): {e}"
            )

        resp = self.client.create_session(
            user_id=self.user_id,
            chip_base=chip_base,
            profit_stop=profit_stop,
            loss_cut=loss_cut,
            resume=resume,
        )
        self._apply_state(resp["state"])
        logger.info(
            f"Remote session {'resumed' if resp.get('resumed') else 'created'}: "
            f"user={self.user_id} sets={len(self.tracker.sets)} "
            f"cp={self.tracker.cumulative_profit:+d}chip"
        )

    def _apply_state(self, state: dict) -> None:
        if isinstance(state, dict):
            self._last_state = dict(state)
        self.tracker.apply_state(state)
        self.session_count = state.get("session_count", 0)
        self.total_bets = state.get("total_bets", 0)
        self.total_wins = state.get("total_wins", 0)
        self.total_losses = state.get("total_losses", 0)
        self.total_ties = state.get("total_ties", 0)
        # Keep local profit_stop/loss_cut in sync with server
        self.profit_stop = state.get("profit_stop", self.profit_stop)
        self.loss_cut = state.get("loss_cut", self.loss_cut)
        self.chip_base = state.get("chip_base", self.chip_base)
        self.tracker.chip_base = self.chip_base

    def get_state_dict(self) -> dict:
        return dict(self._last_state) if isinstance(self._last_state, dict) else {}

    def restore_state(self, state: dict) -> None:
        resp = self.client.restore_session(self.user_id, state)
        self._apply_state(resp["state"])

    # --- Config live update ---

    def update_config(
        self,
        profit_stop: Optional[int] = None,
        loss_cut: Optional[int] = None,
        chip_base: Optional[float] = None,
    ) -> None:
        resp = self.client.update_session(
            self.user_id,
            profit_stop=profit_stop,
            loss_cut=loss_cut,
            chip_base=chip_base,
        )
        self._apply_state(resp["state"])

    # --- Compatibility helpers ---

    def get_bet_amount(self) -> float:
        return self.tracker.current_unit_chips * self.chip_base

    def effective_profit(self) -> int:
        cp = self.tracker.cumulative_profit
        turns = self.tracker.current_turns
        if turns:
            wins = turns.count("O")
            losses = turns.count("X")
            unit = self.tracker.current_unit_chips
            cp += (wins - losses) * unit
        return cp

    def should_reset(self) -> bool:
        cp = self.effective_profit()
        return cp >= self.profit_stop or cp <= -self.loss_cut

    # --- BET cycle (mirrors MaruBatsuBetSession.run_round) ---

    def _exit(self, reason: str) -> dict:
        logger.warning(f"run_round exit: {reason}")
        try:
            import json, sys
            sys.stdout.write(json.dumps({"type": "log", "message": f"[DEBUG EXIT] {reason}"}) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        return {"action": "exit"}

    def run_round(self, running_flag) -> dict:
        if not running_flag():
            return self._exit("running_flag=False (stop requested)")

        if not self.executor.check_and_dismiss_error():
            return self._exit("error_dialog detected")

        # skip_round=False: confirm_2nd_drop() (agent_api) が入場後の見送りを担当するため
        # ここでは常に即座にBETフェーズを待つ
        # 60s: 1ハンド ~30s なので 60s で BET phase を見つけられないなら
        # iframe か WS が壊れている → 早くリカバリした方がロス少ない
        if not self.executor.wait_for_betting_phase(
            timeout=60, skip_round=False
        ):
            if not self.executor.check_and_dismiss_error():
                return self._exit("error_dialog_after_bet_phase_wait")
            return self._exit("bet_phase_timeout")

        # Ask the server for the next BET parameters (always player side)
        try:
            decision = self.client.decide(self.user_id)
        except LaplaceApiError as e:
            return self._exit(f"API decide failed: {e}")

        self._apply_state(decision["state"])

        if decision["action"] == "reset":
            # Let the outer loop handle the reset branch
            return {
                "action": "bet",
                "result": None,
                "won": None,
                "bet_amount": 0.0,
                "completed_set": None,
                "should_reset": True,
            }

        bet_amount = float(decision["bet_amount"])
        unit = int(decision["unit_chips"])
        unit_idx = int(decision["unit_idx"])
        turn_num = int(decision["turn_number"])
        set_idx = int(decision["set_index"])

        # Balance check
        if not self.dry_run:
            balance = self.executor.get_balance()
            if balance < bet_amount:
                logger.error(f"残高不足: ${balance:.2f} < ${bet_amount:.2f}")
                self.notifier.send(
                    f"⚠️ 残高不足!\n"
                    f"必要: ${bet_amount:.2f} ({unit} chips)\n"
                    f"残高: ${balance:.2f}"
                )
                return self._exit(f"insufficient_balance: ${balance:.2f} < ${bet_amount:.2f}")

        side = "player"
        logger.info(
            f"BET: ${bet_amount:.0f} {side.upper()} "
            f"(unit={unit} chips, Set#{set_idx} Turn{turn_num}/7) [remote]"
        )

        if not self.executor.place_bet(side, bet_amount):
            actual_total = self.executor._get_total_bet()
            if actual_total > 0:
                # 部分BET: 置かれた額で続行
                logger.warning(f"部分BET検出: 計画${bet_amount:.0f} → 実際${actual_total:.2f}")
                bet_amount = actual_total
            else:
                # BET完全失敗 — 観戦モードでターンを記録してシーケンス維持
                # 90秒待機していた結果、次のBET phaseを2回取りこぼすことがあったため
                # 35秒に短縮（1ハンドの実時間≒30秒なので余裕を見て35秒）
                logger.warning("BET完全失敗 — 観戦モードで結果を記録してシーケンス維持")
                result_info = self.executor.wait_for_result(timeout=35, bet_amount=0)
                if result_info and result_info.get("result") not in (None, "unknown"):
                    obs_result = result_info["result"]
                    try:
                        self.client.submit_result(self.user_id, obs_result)
                    except Exception as e:
                        logger.error(f"観戦結果送信失敗: {e}")
                # iframe ヘルスチェック: 観戦後に bet spot が再アクセス可能か確認
                # 死んでいたら次のBET phaseを待たずに失敗扱いにして
                # 上位ループの連続失敗カウンタを早めに発動させる
                try:
                    evo = self.executor._get_evo_locator()
                    if not evo.locator('[data-betspot-destination]').first.is_visible(timeout=1500):
                        logger.warning("[health] iframe BET spot 不可視 — iframe劣化の可能性")
                except Exception as _hc_e:
                    logger.warning(f"[health] iframeヘルスチェック例外: {_hc_e}")
                return {"action": "bet", "result": None, "won": None, "bet_amount": 0,
                        "completed_set": None, "should_reset": self.should_reset()}
        else:
            # 部分BETが発生した可能性 → 実際にテーブルに置かれた額で上書き
            actual_total = self.executor._get_total_bet()
            if actual_total > 0 and abs(actual_total - bet_amount) > 0.5:
                logger.warning(f"部分BET検出: 計画${bet_amount:.0f} → 実際${actual_total:.2f}")
                bet_amount = actual_total
            elif actual_total == 0:
                # DOM反映遅延の可能性 — place_bet()がTrueを返した以上BETは通っている
                logger.info(f"DOM total=0だがplace_bet=True — BET${bet_amount:.0f}で続行")

        # wait for result
        result_info = self.executor.wait_for_result(timeout=90, bet_amount=bet_amount)
        if not result_info or not result_info.get("result"):
            return self._exit("wait_for_result failed or empty")

        result = result_info["result"]
        balance = result_info.get("balance", 0)

        # Submit to server
        try:
            resp = self.client.submit_result(self.user_id, result)
        except LaplaceApiError as e:
            return self._exit(f"API submit_result failed: {e}")

        self._apply_state(resp["state"])

        completed_dict = resp.get("completed_set")
        completed_set: Optional[ClientSetData] = None
        if completed_dict:
            completed_set = ClientSetData.from_dict(completed_dict)

        won = resp.get("won")
        need_reset = bool(resp.get("should_reset"))

        # Handle tie / notifications (same as local version)
        if result == "tie":
            logger.info(f"Tie — BET返還、〇❌に影響なし (残高${balance:.2f})")
            return {
                "action": "bet",
                "result": "tie",
                "won": None,
                "bet_amount": bet_amount,
                "completed_set": None,
                "should_reset": need_reset,
            }

        mark = "〇" if won else "✕"
        logger.info(f"結果: {result.upper()} → {mark} (残高${balance:.2f})")

        # Turn notification (same shape as local version)
        turns_display = "".join(
            "〇" if t == "O" else "✕" for t in self.tracker.current_turns
        )
        if not completed_set:
            remaining = 7 - len(self.tracker.current_turns)
            turns_display += "_" * remaining

        try:
            self.notifier.send(
                f"{'〇 的中!' if won else '✕ ハズレ'} Turn {turn_num}/7\n"
                f"結果: {result.upper()} (BET: Player ${bet_amount:.0f})\n"
                f"{turns_display}\n"
                f"残高: ${balance:.2f}"
            )
        except Exception as e:
            logger.warning(f"notify failed: {e}")

        if completed_set:
            self._notify_set_complete(completed_set, balance)

        return {
            "action": "bet",
            "result": result,
            "won": won,
            "bet_amount": bet_amount,
            "completed_set": completed_set,
            "should_reset": need_reset,
        }

    def _notify_set_complete(self, new_set: ClientSetData, balance: float) -> None:
        marks = new_set.results.replace("O", "〇").replace("X", "✕")
        diff = new_set.wins - new_set.losses
        outcome = "勝ち越し 📈" if diff > 0 else "負け越し 📉"
        money_set = new_set.set_profit * self.chip_base
        money_cum = new_set.cumulative_profit * self.chip_base
        next_unit = new_set.next_unit_chips or self.tracker.current_unit_chips

        msg = (
            f"📋 Set #{new_set.set_index} 確定\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{marks}\n"
            f"{outcome} ({new_set.wins}勝 {new_set.losses}敗)\n"
            f"\n"
            f"セット損益: {new_set.set_profit:+d} chip (${money_set:+.2f})\n"
            f"累計損益: {new_set.cumulative_profit:+d} chip (${money_cum:+.2f})\n"
            f"OS: {new_set.overshoot}\n"
            f"\n"
            f"次BET: {next_unit} chip (${next_unit * self.chip_base:.2f})\n"
            f"残高: ${balance:.2f}\n"
            f"━━━━━━━━━━━━━━━"
        )
        try:
            self.notifier.send(msg)
        except Exception as e:
            logger.warning(f"set-complete notify failed: {e}")
        logger.info(
            f"Set #{new_set.set_index} 確定: {new_set.results} "
            f"{new_set.wins}/{new_set.losses} "
            f"P/L:{new_set.cumulative_profit:+d} [remote]"
        )

    def reset_session(self, reason: str) -> None:
        try:
            resp = self.client.reset(self.user_id)
            self._apply_state(resp["state"])
        except LaplaceApiError as e:
            logger.error(f"API reset failed: {e}")

    def handle_shoe_change(self) -> None:
        if not self.tracker.current_turns:
            return
        partial = "".join(
            "〇" if t == "O" else "✕" for t in self.tracker.current_turns
        )
        logger.info(f"シュー交換 — 途中ターン破棄: {partial} [remote]")
        try:
            self.notifier.send(
                f"⚠️ シュー交換\n"
                f"途中ターン破棄: {partial} ({len(self.tracker.current_turns)}/7)\n"
                f"累計損益: {self.tracker.cumulative_profit:+d} chip"
            )
        except Exception:
            pass
        try:
            resp = self.client.shoe_change(self.user_id)
            self._apply_state(resp["state"])
        except LaplaceApiError as e:
            logger.error(f"API shoe_change failed: {e}")

    def get_summary(self) -> dict:
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
            "current_unit": self.tracker.current_unit_chips,
            "current_unit_idx": self.tracker.current_unit_idx,
        }


# =========================================================================
# RemoteTableSelector — client-side wrapper that delegates table selection
# to the VPS. Same public interface as local TableSelector.
# =========================================================================


@dataclass
class RemoteTableResult:
    """Minimal shim that mimics TableCandidate for agent_api.py consumers."""

    table_id: str
    title: str
    players: int
    hands: int
    p_count: int
    b_count: int
    tie_count: int = 0
    last_5: list = field(default_factory=list)
    score: float = 0.0


class RemoteTableSelector:
    """Drop-in replacement for TableSelector backed by the VPS API.

    The scoring formula, exclusion rules and thresholds all live on the VPS.
    The client only ships raw observations to the server and receives a verdict.
    """

    def __init__(self, scraper, client: "LaplaceClient", user_id: str):
        self.scraper = scraper
        self.client = client
        self.user_id = user_id
        self.excluded_table_ids: set[str] = set()

    def _gather_observations(self) -> tuple[dict, dict, dict]:
        """Collect current scraper state to send to the selector API."""
        try:
            configs = self.scraper.get_all_table_configs() or {}
        except Exception as e:
            logger.error(f"scraper.get_all_table_configs failed: {e}")
            configs = {}
        try:
            players = self.scraper.get_players_count() or {}
        except Exception as e:
            logger.error(f"scraper.get_players_count failed: {e}")
            players = {}
        histories: dict[str, list] = {}
        for tid in list(configs.keys()):
            try:
                histories[tid] = self.scraper.get_raw_history(tid) or []
            except Exception:
                histories[tid] = []
        return configs, players, histories

    def find_best_table(
        self, fixed_name: Optional[str] = None, selector_config: Optional[dict] = None
    ) -> Optional[RemoteTableResult]:
        configs, players, histories = self._gather_observations()
        if not configs:
            logger.info("[selector-remote] no configs yet — scraper still warming up")
            return None

        if selector_config:
            logger.debug(f"[selector-remote] selector_config={selector_config}")

        try:
            resp = self.client.select_table(
                user_id=self.user_id,
                configs=configs,
                players=players,
                histories=histories,
                excluded_ids=list(self.excluded_table_ids),
                fixed_name=fixed_name,
                selector_config=selector_config,
            )
        except LaplaceApiError as e:
            logger.error(f"select_table API failed: {e}")
            return None

        if not resp.get("found"):
            wait = resp.get("wait_status") or "unknown"
            debug = resp.get("debug") or {}
            logger.info(f"[selector-remote] no table — status={wait} debug={debug}")
            return None

        result = RemoteTableResult(
            table_id=resp["table_id"],
            title=resp["title"],
            players=resp["players"],
            hands=resp["hands"],
            p_count=resp["p_count"],
            b_count=resp["b_count"],
            tie_count=resp.get("tie_count", 0),
            last_5=resp.get("last_5", []),
            score=resp.get("score", 0.0),
        )

        # クライアント側フィルタ: VPS APIが selector_config を無視した場合の安全策
        if selector_config:
            min_h = selector_config.get("min_hands", 0)
            max_h = selector_config.get("max_hands", 999)
            min_p = selector_config.get("players_primary", 0)
            if result.hands < min_h or result.hands > max_h:
                logger.info(
                    f"[selector-remote] REJECT: {result.title} h={result.hands} "
                    f"(filter: {min_h}-{max_h})"
                )
                return None
            if result.players < min_p:
                logger.info(
                    f"[selector-remote] REJECT: {result.title} p={result.players} "
                    f"(filter: min_p={min_p})"
                )
                return None

        logger.info(
            f"[selector-remote] BEST: {result.title} "
            f"p={result.players} h={result.hands} "
            f"P={result.p_count} B={result.b_count} score={result.score:.1f}"
        )
        return result

    def should_exit_table(self, table_id: str, selector_config: Optional[dict] = None) -> Optional[str]:
        try:
            players_map = self.scraper.get_players_count() or {}
            p_count = players_map.get(table_id, 0)
            raw = self.scraper.get_raw_history(table_id) or []
        except Exception as e:
            logger.error(f"exit_check scraper fetch failed: {e}")
            return None
        try:
            resp = self.client.exit_check(table_id, p_count, raw)
        except LaplaceApiError as e:
            logger.error(f"exit_check API failed: {e}")
            return None
        return resp.get("exit_reason")
