"""ゲーム内WebSocket状態管理

テーブル入場後のEvolution WSメッセージ (framesent) を傍受し、
ラウンド状態・BET受理・結果・残高を提供する。

状態遷移:
  Idle → Betting → (BETクリック) → Accepted → Settled → Idle → Betting → ...

WSメッセージ (CLIENT_BET_ACCEPTED の status フィールド):
  "Idle"     = ディーリング中 / ラウンド間
  "Betting"  = BETフェーズ (チップ配置可能)
  "Accepted" = BET受理完了
  "Settled"  = 結果確定
"""
import json
import threading
import time
import logging

logger = logging.getLogger("baccarat.game_ws")


class GameWSMonitor:
    """ゲーム内WS状態を追跡"""

    def __init__(self):
        self._lock = threading.Lock()
        self._status = "unknown"       # Idle / Betting / Accepted / Settled
        self._balance = 0.0
        self._last_confirmed = {}      # {"Player": 1, "PlayerFee": 0.2}
        self._last_result_multiplier = None  # {"betSpot": "Player", "multiplier": 2}
        self._multiplier_received_at = 0.0   # multiplier受信時刻 (stale検出用)
        self._bet_placed_at = 0.0            # BET実行時刻 (stale検出用)
        self._settled_balance = None   # Settled時の残高
        self._settled_seen_since_bet = False  # BET後にSettledを見たか (real Tie判定用)
        self._status_changed = threading.Event()
        self._connected = False
        self._last_message_at = time.time()

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def balance(self) -> float:
        with self._lock:
            return self._balance

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> float:
        return self._last_message_at

    def seconds_since_last_message(self) -> float:
        return time.time() - self._last_message_at

    def reset(self):
        with self._lock:
            self._status = "unknown"
            self._last_confirmed = {}
            self._last_result_multiplier = None
            self._multiplier_received_at = 0.0
            self._bet_placed_at = 0.0
            self._settled_balance = None
            self._settled_seen_since_bet = False  # BET後にSettledを見たか
            self._connected = False
            # ウォッチドッグの WS silent タイマーをリセット
            # （reset() はテーブル退出/再入場/フルリカバリで呼ばれるため、
            #  この時点でWS silent タイマーがゼロから始まるべき）
            self._last_message_at = time.time()
        self._status_changed.clear()

    def mark_bet_placed(self):
        """BET実行時に呼び出し。stale multiplier検出用のタイムスタンプを記録"""
        with self._lock:
            self._bet_placed_at = time.time()
            self._last_result_multiplier = None
            self._last_confirmed = {}
            self._settled_seen_since_bet = False  # BET 開始時にリセット
        logger.debug("BET実行記録 — multiplier/confirmed リセット")

    def on_ws_message(self, raw: str):
        """WSメッセージ (framesent/framereceived) を処理"""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return

        if not isinstance(data, dict):
            return

        self._connected = True
        self._last_message_at = time.time()

        log_entry = data.get("log", {})
        if not log_entry:
            # framereceived形式: {"type":"...", "args":{...}} の可能性
            msg_type = data.get("type", "")
            args = data.get("args", {})
            if msg_type and args:
                self._handle_server_message(msg_type, args)
            return

        msg_type = log_entry.get("type", "")
        value = log_entry.get("value", {})

        if "BET" in msg_type or "BALANCE" in msg_type or "MULTIPLIER" in msg_type or "SETTLED" in msg_type or "RESULT" in msg_type:
            status = value.get("status", "")
            logger.info(f"WS: {msg_type} status={status}")

        if msg_type == "CLIENT_BET_ACCEPTED":
            self._handle_bet_accepted(value)
        elif msg_type == "CLIENT_BALANCE_UPDATED":
            self._handle_balance_updated(value)
        elif msg_type == "CLIENT_BACCARAT_TOTAL_MULTIPLIER_DISPLAYED":
            self._handle_multiplier(value)
        elif msg_type == "CLIENT_RECEIVED_BET_RESPONSE":
            self._handle_bet_response(value)

    def _handle_bet_accepted(self, value: dict):
        new_status = value.get("status", "")
        if not new_status:
            return

        with self._lock:
            old = self._status
            self._status = new_status

            bal = value.get("balance")
            if bal is not None:
                self._balance = float(bal)

            confirmed = value.get("confirmed", {})
            if confirmed:
                self._last_confirmed = confirmed

            if new_status == "Settled":
                self._settled_balance = self._balance
                self._settled_seen_since_bet = True  # real Tie判定用フラグ
                logger.info(f"Settled confirmed={confirmed} balance={self._balance}")

        if old != new_status:
            logger.debug(f"ラウンド状態: {old} → {new_status}")
            self._status_changed.set()

    def _handle_balance_updated(self, value: dict):
        bal = value.get("balance")
        if bal is not None:
            with self._lock:
                self._balance = float(bal)

    def _handle_multiplier(self, value: dict):
        with self._lock:
            self._last_result_multiplier = {
                "betSpot": value.get("betSpot", ""),
                "multiplier": value.get("multiplier", 0),
            }
            self._multiplier_received_at = time.time()
        logger.info(f"Multiplier受信: betSpot={value.get('betSpot')}, multiplier={value.get('multiplier')}")

    def _handle_bet_response(self, value: dict):
        state = value.get("state", {})
        total = state.get("totalAmount", 0)
        if total > 0:
            logger.debug(f"BET応答: totalAmount={total}")

    def _handle_server_message(self, msg_type: str, args: dict):
        """サーバー→クライアントのメッセージ処理 (framereceived)"""
        # ラウンド結果通知
        if "result" in msg_type.lower() or "settled" in msg_type.lower():
            logger.info(f"サーバーメッセージ: {msg_type} args_keys={list(args.keys())[:5]}")
        # ゲーム状態変化
        if "gameState" in args or "status" in args:
            status = args.get("status") or args.get("gameState", {}).get("status")
            if status:
                with self._lock:
                    old = self._status
                    self._status = status
                if old != status:
                    logger.debug(f"サーバー状態: {old} → {status}")
                    self._status_changed.set()

    # === 待機メソッド ===

    def wait_for_status(self, target: str, timeout: float = 60) -> bool:
        """指定ステータスになるまで待機。成功=True"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.status == target:
                return True
            self._status_changed.clear()
            remaining = deadline - time.time()
            if remaining > 0:
                self._status_changed.wait(timeout=min(remaining, 1.0))
        return self.status == target

    def wait_for_betting_phase(self, timeout: float = 120, dom_checker=None, skip_round: bool = True, error_checker=None) -> bool:
        """BETフェーズを待機 (DOM + WS デュアルチェック)。

        skip_round=True: 1ラウンド見送り後に次のBETフェーズを待つ (テーブル入場直後)
        skip_round=False: 次のBETフェーズだけ待つ (1-2-3打法の連続BET時)

        【WSフォールバック】
        ネットワーク遅延で Evolution が低画質モードに切り替わると、
        inner iframe が一時的に detach/再構築され、DOM ベースの timerCircleContainer
        検出が機能不全になることがある（クラウドPC等で頻発）。
        WS (CLIENT_BET_ACCEPTED status=Betting) は network レイヤで受信するため
        iframe DOM とは独立に動作し、影響を受けない。これをフォールバックに使う。

        シャッフル/ディーラー交代で長時間待つこともあるため、
        タイムアウトは十分長くとる。
        """
        if not dom_checker:
            logger.warning("dom_checker未設定")
            return False

        deadline = time.time() + timeout

        if skip_round:
            logger.info("1ラウンド見送り中...")
            # Step 1: BETフェーズが終わるのを待つ
            #  - DOM: タイマー消失
            #  - WS: status が Betting 以外（Settled/Idle/Dealing 等）
            # どちらかが満たされたら次へ進む
            while time.time() < deadline:
                try:
                    dom_active = dom_checker()
                except Exception:
                    dom_active = False
                ws_status = self.status
                if (not dom_active) or (ws_status and ws_status != "Betting"):
                    logger.info(f"ディーリング中 (dom_active={dom_active}, ws={ws_status})")
                    break
                if error_checker and not error_checker():
                    logger.warning("BET待機中にエラーダイアログ検出")
                    return False
                time.sleep(0.5)
            else:
                logger.warning("タイマー消失待ちタイムアウト")
                return False

        # 次のBETフェーズ開始を待つ (DOM + WS どちらかで検出)
        logger.info("BETフェーズを待機中...")
        _dom_error_logged = False
        while time.time() < deadline:
            # WS フォールバック: status=Betting を即検出
            if self.status == "Betting":
                logger.info("BETフェーズ開始 (WS検出)")
                return True
            # DOM チェック (失敗してもWSがあるので例外は握り潰す)
            try:
                if dom_checker():
                    logger.info("BETフェーズ開始 (DOM検出)")
                    return True
            except Exception as _e:
                if not _dom_error_logged:
                    logger.warning(f"DOM checker例外（WSフォールバック使用中）: {_e}")
                    _dom_error_logged = True
            if error_checker and not error_checker():
                logger.warning("BETフェーズ待機中にエラーダイアログ検出")
                return False
            time.sleep(0.5)

        logger.warning("BETフェーズ待機タイムアウト (シャッフル/ディーラー交代の可能性)")
        return False

    def wait_for_accepted(self, timeout: float = 30) -> bool:
        """BET受理 (status=Accepted) を待機"""
        return self.wait_for_status("Accepted", timeout)

    def wait_for_settled(self, timeout: float = 60) -> dict | None:
        """結果確定を待機。Settled または Idle (BET後の結果) を待つ"""
        logger.info("勝敗を待ちます...")

        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.status
            if s in ("Settled", "Idle"):
                logger.info(f"結果確定 (状態: {s})")
                with self._lock:
                    return {
                        "balance": self._balance,
                        "confirmed": dict(self._last_confirmed),
                        "multiplier": dict(self._last_result_multiplier) if self._last_result_multiplier else None,
                    }
            self._status_changed.clear()
            self._status_changed.wait(timeout=1.0)

        logger.warning("結果待ちタイムアウト")
        return None

    def get_result_side(self) -> str | None:
        """直近の結果 (player/banker/tie) を返す。

        multiplier値のみで判定 (CLIENT_BACCARAT_TOTAL_MULTIPLIER_DISPLAYED)。
        このメッセージが来ないテーブルではNoneを返し、executor側の
        WS残高diff判定にフォールバックさせる。

        multiplier判定 (常にPlayerに賭けている前提):
          multiplier > 1.0  → Player勝ち ("player")
          multiplier == 1.0 → タイ ("tie") — BET返却
          multiplier == 0   → Banker勝ち ("banker") — BET没収

        NOTE: confirmed dict は BET内容(何に賭けたか)であり、
        ゲーム結果ではないため判定に使わない。
        """
        with self._lock:
            mult = self._last_result_multiplier
            if not mult or not mult.get("betSpot"):
                return None

            # stale検出
            if self._bet_placed_at > 0 and self._multiplier_received_at < self._bet_placed_at:
                logger.debug(f"Stale multiplier検出 (bet={self._bet_placed_at:.1f} > mult={self._multiplier_received_at:.1f})")
                return None

            multiplier = float(mult.get("multiplier", 0))
            if multiplier > 1.0:
                return "player"
            elif abs(multiplier - 1.0) < 0.01:
                return "tie"
            else:
                return "banker"
