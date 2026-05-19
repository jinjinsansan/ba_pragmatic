"""
dual_line_live_executor.py  —  Passive game WS executor

設計思想:
  - テーブル選択・入場はユーザーが手動で行う
  - Bot は game WS が開いた瞬間を自動検出 (page.on("websocket"))
  - betsopen イベントを WS メッセージから受動的に検知
  - シグナルが来たら次の betsopen でBETを送信
  - テーブル切替なし・自動入場なし → シンプルで確実

WS Bridge:
  context.add_init_script() で全フレームに事前注入。
  ゲームWSはブリッジ経由で __bacopy_ws_send() から送信。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

logger = logging.getLogger("dual_line.live")

# ── WS Bridge JS ──────────────────────────────────────────────────────
# context.add_init_script() で全フレームに注入される。
# ゲームWSを捕捉し、__bacopy_ws_send() で送信できるようにする。

_WS_BRIDGE_INIT = r"""
(() => {
  if (window.__bacopy_ws_bridge_installed) return;
  window.__bacopy_ws_bridge_installed = true;
  window.__bacopy_sockets = [];

  const OrigWS = window.WebSocket;
  window.WebSocket = function(url, protocols) {
    const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
    window.__bacopy_sockets.push(ws);
    return ws;
  };
  window.WebSocket.prototype = OrigWS.prototype;
  window.WebSocket.CONNECTING = OrigWS.CONNECTING;
  window.WebSocket.OPEN = OrigWS.OPEN;
  window.WebSocket.CLOSING = OrigWS.CLOSING;
  window.WebSocket.CLOSED = OrigWS.CLOSED;

  window.__bacopy_ws_send = (match, payload) => {
    for (const ws of window.__bacopy_sockets) {
      try {
        if (ws.readyState === 1 && ws.url && ws.url.includes(match)) {
          ws.send(payload);
          return { ok: true, url: ws.url.slice(-60) };
        }
      } catch(e) {}
    }
    return { ok: false, sockets: window.__bacopy_sockets.length };
  };

  window.__bacopy_ws_urls = () =>
    window.__bacopy_sockets.map(ws => ({
      url: ws.url, readyState: ws.readyState
    }));
})();
"""

# ── ヘルパー ──────────────────────────────────────────────────────────

def _side_to_bc(side: str) -> str:
    return "B" if side.upper() in ("B", "BANKER") else "P"


def _build_lpbet_xml(*, table_id: str, game_id: str, user_id: str,
                     bc: str, amount: float) -> str:
    import hashlib
    ck_src = f"{game_id}{user_id}{bc}{amount:.2f}"
    ck = hashlib.md5(ck_src.encode()).hexdigest()[:8]
    return (
        f'<command channel="table-{table_id}">'
        f'<lpbet gm="baccarat_desktop" gId="{game_id}" uId="{user_id}" ck="{ck}">'
        f'<bet type="{bc}" amount="{amount:.2f}"/>'
        f'</lpbet></command>'
    )


def _extract_ck(xml: str) -> str:
    m = re.search(r'ck="([^"]+)"', xml)
    return m.group(1) if m else ""


# ── LiveBetExecutor ───────────────────────────────────────────────────

class LiveBetExecutor:
    """
    ユーザーが手動でテーブルに入場し、game WS を自動検出してBETする。

    ライフサイクル:
      waiting  → ユーザーのテーブル入場待ち
      ready    → game WS 確立・betsopen 待ち
      betting  → BET 送信中
    """

    is_live: bool = True  # BetExecutor Protocol 互換

    def __init__(self, notify_fn=None):
        self._notify = notify_fn or (lambda text: None)
        self._context: Any = None
        self._lobby_page: Any = None
        self._bet_page: Any = None
        self._profile_dir: str = ""          # set by bot.run() for persistence
        self._attached_page_ids: set[int] = set()

        # game WS 状態
        self._game_ws_url: str = ""          # 検出した game WS URL
        self._table_id: str = ""             # tableId (operator numeric)
        self._table_name: str = ""           # テーブル名

        # keep-alive / inactivity / WS silence 対策
        self._last_keep_alive_at: float = 0.0
        self._last_game_ws_recv_at: float = 0.0
        self._last_inactivity_check_at: float = 0.0
        self._user_id: str = ""              # userId (for lpbet)
        self._game_id: str = ""              # current game id
        self._phase: str = "waiting"         # waiting | ready | betting

        # betsopen 状態
        self._bets_open_game_id: str = ""
        self._bets_closed_game_id: str = ""
        self._last_bets_open_at: float = 0.0

        # BET 予約
        self._pending_bet: dict | None = None
        self._switch_request: dict | None = None
        self._switch_in_progress: bool = False
        self._sent_bet_ids: set[str] = set()
        self._lock = threading.Lock()

        # 統計
        self._consecutive_failures: int = 0

        # 定期ブリッジ注入
        self._last_bridge_inject: float = 0.0

    # ── setup ────────────────────────────────────────────────────────

    def setup(self, context: Any, lobby_page: Any, bet_page: Any | None = None) -> None:
        self._context = context
        self._lobby_page = lobby_page
        self._bet_page = bet_page or lobby_page

        # 既存ページすべてを監視対象にする（lobby + bet）
        try:
            for p in (context.pages or []):
                self._attach_page(p)
        except Exception:
            self._attach_page(lobby_page)

        # 新しいポップアップページも監視
        def _on_new_page(page: Any) -> None:
            logger.info(f"[LIVE] new page detected: {page.url[:60]}")
            self._attach_page(page)
        context.on("page", _on_new_page)

        logger.info("[LIVE] executor ready — waiting for user to enter table")
        self._notify(
            "⏳ テーブル入場待機中\n"
            "Pragmaticロビーで好きなテーブルを\n"
            "クリックして入場してください"
        )

    def _attach_page(self, page: Any) -> None:
        """既存/新規ページへ bridge + websocket hook を1回だけ設定。"""
        try:
            pid = id(page)
            if pid in self._attached_page_ids:
                return
            self._attached_page_ids.add(pid)
        except Exception:
            pass
        self._inject_all(page)
        try:
            page.on("websocket", self._on_ws_event)
        except Exception:
            pass

    def _inject_all(self, page: Any) -> None:
        """ページと全フレームにブリッジを注入。"""
        try:
            page.evaluate(_WS_BRIDGE_INIT)
        except Exception:
            pass
        try:
            for fr in page.frames:
                try:
                    fr.evaluate(_WS_BRIDGE_INIT)
                except Exception:
                    pass
        except Exception:
            pass

    # ── game WS 検出 ─────────────────────────────────────────────────

    def _on_ws_event(self, ws: Any) -> None:
        url = str(ws.url or "")
        # Pragmatic の game WS か確認
        if "pragmaticplaylive.net" not in url and "qpidreoxcc.net" not in url:
            return
        # dga (lobby 監視) WS は除外
        if "/dga" in url or "dga." in url:
            return

        ws_table_id = ""
        m = re.search(r'[?&]tableId=([^&]+)', url)
        if m:
            ws_table_id = m.group(1)

        # ready/betting 中は現在テーブルにロック（switch 中は waiting に戻して解除）
        if (
            self._phase in ("ready", "betting")
            and self._table_id
            and ws_table_id
            and ws_table_id != self._table_id
        ):
            logger.info(
                f"[LIVE] ignore secondary game WS: {ws_table_id} (active={self._table_id})"
            )
            return

        logger.info(f"[LIVE] game WS detected: {url[-100:]}")
        self._game_ws_url = url
        if ws_table_id and not self._table_id:
            self._table_id = ws_table_id
            logger.info(f"[LIVE] tableId from URL: {self._table_id}")
            self._save_last_table()
        self._last_game_ws_recv_at = time.time()

        # WS メッセージを受動的に取得
        # Playwright のバージョンにより f が str の場合と FrameData の場合がある
        def _to_data(f) -> str:
            if isinstance(f, str):
                return f
            try:
                return f.body or ""
            except Exception:
                return str(f) if f else ""

        ws.on("framereceived", lambda f, tid=ws_table_id: self._on_ws_message(_to_data(f), tid))
        ws.on("framesent",     lambda f, tid=ws_table_id: self._on_ws_sent(_to_data(f), tid))

        # phase = ready
        if self._phase == "waiting":
            self._phase = "ready"
            tname = self._table_name or self._table_id or "?"
            self._notify(
                f"🔗 game WS 確立\n"
                f"table: {tname}\n"
                f"BET待機中 (betsopen を待っています)"
            )

    def _on_ws_message(self, data: str, ws_table_id: str = "") -> None:
        """ゲームWSからの受信メッセージを解析。"""
        if not data:
            return
        if self._table_id and ws_table_id and ws_table_id != self._table_id:
            return

        # JSON
        if data.startswith("{") or data.startswith("["):
            try:
                obj = json.loads(data)
            except Exception:
                return
            if not isinstance(obj, dict):
                return

            # betsopen
            bo = obj.get("betsopen")
            if isinstance(bo, dict):
                gid = str(bo.get("game") or "")
                if gid and gid != self._bets_open_game_id:
                    self._bets_open_game_id = gid
                    self._last_bets_open_at = time.time()
                    logger.info(f"[LIVE] betsopen: game={gid} table={self._table_id}")
                    # 通知はBET予約がある時だけ（毎ハンド通知は不要）
                    if self._pending_bet:
                        self._notify(
                            f"🟢 BET WINDOW OPEN\n"
                            f"table: {self._table_name or self._table_id}\n"
                            f"game: {gid}"
                        )
                    self._try_execute_bet(gid)
                return

            # betsclosed
            bc_obj = obj.get("betsclosed")
            if isinstance(bc_obj, dict):
                gid = str(bc_obj.get("game") or "")
                if gid:
                    self._bets_closed_game_id = gid
                return

            # game id
            g = obj.get("game")
            if isinstance(g, dict):
                gid = str(g.get("id") or "")
                if gid:
                    self._game_id = gid
                return

            # user_id from ALERT_JOINED
            u = obj.get("user") if isinstance(obj.get("user"), dict) else {}
            uid = str(u.get("userId") or "")
            if uid and not self._user_id:
                self._user_id = uid
                logger.info(f"[LIVE] user_id: ...{uid[-8:]}")
            self._last_game_ws_recv_at = time.time()
            return

        # XML
        if data.startswith("<"):
            m = re.search(r'userId="([^"]+)"', data)
            if m and not self._user_id:
                self._user_id = m.group(1)
                logger.info(f"[LIVE] user_id from XML: ...{self._user_id[-8:]}")

    def _on_ws_sent(self, data: str, ws_table_id: str = "") -> None:
        """送信メッセージから tableId / userId を補完。"""
        if not data:
            return
        if self._table_id and ws_table_id and ws_table_id != self._table_id:
            return
        # JSON SUBSCRIBE
        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                uid = str(obj.get("userId") or "")
                if uid and not self._user_id:
                    self._user_id = uid
                    logger.info(f"[LIVE] user_id from SUBSCRIBE: ...{uid[-8:]}")
                tid = str(obj.get("tableId") or "")
                if tid and not self._table_id:
                    self._table_id = tid
                    logger.info(f"[LIVE] tableId from SUBSCRIBE: {tid}")
                    if self._phase == "waiting":
                        self._phase = "ready"
                        self._notify(f"🔗 game WS 確立\ntable: {tid}\nBET待機中")
        except Exception:
            pass
        # XML SUBSCRIBE
        m = re.search(r'userId="([^"]+)"', data)
        if m and not self._user_id:
            self._user_id = m.group(1)
        m = re.search(r'tableId="([^"]+)"', data)
        if m and not self._table_id:
            self._table_id = m.group(1)

    # ── tick (bot.run() から定期呼出) ────────────────────────────────

    def tick(self) -> None:
        now = time.time()
        page = self._lobby_page

        # switch 要求があれば先に処理（テーブル入場）
        if self._switch_request and not self._switch_in_progress:
            req = self._switch_request
            self._switch_request = None
            self._switch_in_progress = True
            try:
                self._perform_switch(req)
            except Exception as e:
                logger.warning(f"[LIVE] switch failed: {e}")
            finally:
                self._switch_in_progress = False

        # 5秒ごとにブリッジを再注入（フレーム遷移・新フレームに備える）
        if now - self._last_bridge_inject > 5.0:
            self._last_bridge_inject = now
            if page:
                self._inject_all(page)
            try:
                for p in (self._context.pages or []):
                    if p != page:
                        self._inject_all(p)
            except Exception:
                pass

        # betsopen タイムアウト監視
        if self._phase == "ready" and self._last_bets_open_at > 0:
            max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "17") or 17)
            if now - self._last_bets_open_at > max_age + 2:
                if self._bets_open_game_id != self._bets_closed_game_id:
                    self._bets_closed_game_id = self._bets_open_game_id

        if page is None:
            return

        # ── 受け子モードから移植: keep-alive / inactivity / WS silence ──

        # keep-alive: 90秒ごとにマウス微動 (Stake 非アクティブモーダル予防)
        if now - self._last_keep_alive_at >= 90.0:
            self._last_keep_alive_at = now
            try:
                from bacopy_executor_pragmatic_ws_live import _send_keep_alive
                _send_keep_alive(page, None)
            except Exception as e:
                logger.debug(f"[LIVE] keep_alive error: {e}")

        # inactivity モーダル検出・解除 (10秒ごと)
        if now - self._last_inactivity_check_at >= 10.0:
            self._last_inactivity_check_at = now
            try:
                from bacopy_executor_pragmatic_ws_live import _dismiss_inactivity_modal
                _dismiss_inactivity_modal(page, None)
            except Exception as e:
                logger.debug(f"[LIVE] inactivity check error: {e}")

        # game WS 沈黙 150s → 動画クリックで強制復活
        if (
            self._phase == "ready"
            and self._last_game_ws_recv_at > 0
            and now - self._last_game_ws_recv_at >= 150.0
        ):
            logger.warning(
                f"[LIVE] game WS silent {int(now - self._last_game_ws_recv_at)}s — video click"
            )
            self._last_game_ws_recv_at = now  # 連打防止
            try:
                from bacopy_executor_pragmatic_ws_live import _click_live_video_center
                _click_live_video_center(page)
            except Exception as e:
                logger.debug(f"[LIVE] video click error: {e}")

    def _perform_switch(self, req: dict) -> None:
        """受け子モードの _join_table を使って bet_page を対象卓へ入場させる。"""
        try:
            from bacopy_executor_pragmatic_ws_live import _join_table
        except Exception as e:
            logger.warning(f"[LIVE] switch import failed: {e}")
            return

        table_id = str(req.get("table_id") or "").strip()
        table_name = str(req.get("table_name") or "").strip()
        qpid = str(req.get("qpid") or "").strip()
        if not table_id and not qpid:
            return

        # 新卓へ切り替える前に現在セッション情報をクリア
        self._phase = "waiting"
        self._table_id = ""
        self._game_ws_url = ""
        self._game_id = ""
        self._user_id = ""
        self._bets_open_game_id = ""
        self._bets_closed_game_id = ""
        self._last_bets_open_at = 0.0
        if table_name:
            self._table_name = table_name

        page = self._bet_page or self._lobby_page
        if page is None:
            return

        wait_sec = int(os.getenv("BACOPY_AUTO_CLICK_WAIT_SEC", "90") or "90")
        logger.info(f"[LIVE] switching to table={table_name or table_id} qpid={qpid or '-'}")
        _join_table(
            page,
            table_substr=(table_name or table_id),
            auto_click_wait_sec=wait_sec,
            state=None,
            on_tick=None,
            is_initial=False,
            interrupt_check=None,
            qpid_table_id=(qpid or table_id),
        )

    # ── BET API ──────────────────────────────────────────────────────

    def send_bet(self, side: str, amount: float, table_id: str = "", bet_id: str = "") -> bool:
        """BET を予約する。次の betsopen で送信。"""
        target_table = str(table_id or self._table_id or "").strip()
        if not target_table:
            logger.info("[LIVE] send_bet ignored: empty target table")
            return False

        # 別テーブルへのBET要求なら switch を予約
        if self._phase == "waiting" or (self._table_id and target_table != self._table_id):
            self._request_switch(target_table, self._table_name, target_table)

        with self._lock:
            self._pending_bet = {
                "side": side,
                "amount": amount,
                "table_id": target_table,
                "bet_id": str(bet_id or "").strip(),
                "queued_at": time.time(),
            }
        logger.info(f"[LIVE] bet queued: {side} ${amount:.2f} table={target_table}")

        # すでに betsopen 中なら即送信
        if self._phase != "waiting" and self._is_bet_window_open():
            self._try_execute_bet(self._bets_open_game_id)

        return True

    def _is_bet_window_open(self) -> bool:
        if not self._bets_open_game_id:
            return False
        if self._bets_open_game_id == self._bets_closed_game_id:
            return False
        age = time.time() - self._last_bets_open_at
        max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "17") or 17)
        return 0 < age < max_age

    def _try_execute_bet(self, game_id: str) -> None:
        with self._lock:
            bet = self._pending_bet
            if not bet:
                return
            self._pending_bet = None

        queued_at = float(bet.get("queued_at") or 0.0)
        if queued_at:
            max_age = float(os.getenv("BACOPY_MAX_BET_SIGNAL_AGE_SEC", "25") or 25)
            if (time.time() - queued_at) > max_age:
                logger.warning("[LIVE] drop stale pending bet (signal too old)")
                self._notify("⚠️ BET SKIP\nsignal too old")
                return

        side = bet["side"]
        amount = bet["amount"]
        table_id = bet.get("table_id") or self._table_id
        bet_id = str(bet.get("bet_id") or "").strip()
        user_id = self._user_id or os.getenv("BACOPY_USER_ID", "").strip()

        if not user_id:
            logger.warning("[LIVE] user_id unknown — bet deferred")
            with self._lock:
                self._pending_bet = bet  # 戻す
            return

        bc = _side_to_bc(side)
        xml = _build_lpbet_xml(
            table_id=table_id,
            game_id=game_id,
            user_id=user_id,
            bc=bc,
            amount=amount,
        )
        ck = _extract_ck(xml)
        side_name = "BANKER" if bc == "B" else "PLAYER"

        logger.info(f"[LIVE] executing bet: {side_name} ${amount:.2f} game={game_id}")
        self._phase = "betting"
        self._notify(f"💰 BET 送信\n{side_name} ${amount:.2f}\ntable: {table_id}\ngame: {game_id}")

        result = self._ws_send(table_id, xml)
        ok = isinstance(result, dict) and result.get("ok")

        if ok:
            logger.info(f"[LIVE] bet sent OK ck={ck}")
            self._consecutive_failures = 0
            if bet_id:
                self._sent_bet_ids.add(bet_id)
            self._notify(f"✅ BET OK\n{side_name} ${amount:.2f} ck={ck}")
        else:
            logger.error(f"[LIVE] bet send FAILED: {result}")
            self._consecutive_failures += 1
            self._notify(f"❌ BET FAILED\n{side_name} ${amount:.2f}\n{result}")

        # 単一 bet_page 運用: 送信後は about:blank に戻して次シグナル待機
        try:
            page = self._bet_page
            if page is not None:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        self._phase = "waiting"
        self._table_id = ""
        self._game_ws_url = ""
        self._game_id = ""
        self._user_id = ""

    def _ws_send(self, table_id: str, payload: str) -> dict:
        """game WS にメッセージを送信。"""
        match = f"tableId={table_id}"
        pages = [self._lobby_page] if self._lobby_page else []
        try:
            for p in (self._context.pages or []):
                if p not in pages:
                    pages.append(p)
        except Exception:
            pass

        for page in pages:
            all_frames = [page]
            try:
                all_frames += list(page.frames)
            except Exception:
                pass
            for fr in all_frames:
                try:
                    res = fr.evaluate(
                        "(args) => window.__bacopy_ws_send "
                        "? window.__bacopy_ws_send(args.match, args.payload) "
                        ": {ok:false, reason:'no_bridge'}",
                        {"match": match, "payload": payload},
                    )
                    if isinstance(res, dict) and res.get("ok"):
                        return res
                except Exception:
                    pass
        return {"ok": False, "reason": "not_sent"}

    # ── BetExecutor Protocol 互換 API ────────────────────────────────

    def place_bet(self, table_id: str, side: str, amount: float,
                  metadata: dict | None = None) -> str:
        """bot から呼ばれる BetExecutor 互換メソッド。send_bet() に委譲。"""
        import uuid
        md = metadata or {}
        table_name = str(md.get("table_name") or "").strip()
        qpid = str(md.get("qpid_table_id") or "").strip()
        if table_name:
            self._table_name = table_name
        self._request_switch(str(table_id or ""), table_name, qpid)
        bet_id = f"dl_{uuid.uuid4().hex[:12]}"
        self.send_bet(side=side, amount=amount, table_id=(qpid or table_id), bet_id=bet_id)
        return bet_id

    def consume_sent_bet(self, bet_id: str) -> bool:
        bid = str(bet_id or "").strip()
        if not bid:
            return False
        if bid in self._sent_bet_ids:
            self._sent_bet_ids.remove(bid)
            return True
        return False

    def _request_switch(self, table_id: str = "", table_name: str = "", qpid: str = "") -> None:
        """対象卓への switch 要求をキューに積む。"""
        tid = str(table_id or "").strip()
        if not tid and not qpid:
            return
        self._switch_request = {
            "table_id": tid or str(qpid or "").strip(),
            "table_name": str(table_name or "").strip(),
            "qpid": str(qpid or "").strip(),
        }

    def set_table_name(self, table_id: str, table_name: str) -> None:
        """bot から table_name を補完する。"""
        if table_id == self._table_id and not self._table_name:
            self._table_name = table_name
            self._save_last_table()  # name が判明したタイミングで上書き保存

    def set_profile_dir(self, path: str) -> None:
        """プロファイルディレクトリを設定 (bot.run() から呼ばれる)。"""
        self._profile_dir = path

    def _save_last_table(self) -> None:
        """最後のテーブルIDをファイルに保存 (再起動後の自動再入場用)。"""
        if not self._profile_dir or not self._table_id:
            return
        try:
            import os as _os
            path = _os.path.join(self._profile_dir, "last_table.json")
            data = json.dumps({"table_id": self._table_id, "table_name": self._table_name})
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            logger.info(f"[LIVE] last_table saved: {self._table_id} ({self._table_name})")
        except Exception as e:
            logger.warning(f"[LIVE] last_table save failed: {e}")

    @property
    def current_table_id(self) -> str:
        return self._table_id

    @property
    def has_pending_bet(self) -> bool:
        return bool(self._pending_bet) or self._switch_in_progress or bool(self._switch_request)

    @property
    def is_ready(self) -> bool:
        return self._phase in ("ready", "betting")
