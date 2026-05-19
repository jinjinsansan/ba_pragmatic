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

    def __init__(self, notify_fn=None):
        self._notify = notify_fn or (lambda text: None)
        self._context: Any = None
        self._lobby_page: Any = None

        # game WS 状態
        self._game_ws_url: str = ""          # 検出した game WS URL
        self._table_id: str = ""             # tableId (operator numeric)
        self._table_name: str = ""           # テーブル名
        self._user_id: str = ""              # userId (for lpbet)
        self._game_id: str = ""              # current game id
        self._phase: str = "waiting"         # waiting | ready | betting

        # betsopen 状態
        self._bets_open_game_id: str = ""
        self._bets_closed_game_id: str = ""
        self._last_bets_open_at: float = 0.0

        # BET 予約
        self._pending_bet: dict | None = None
        self._lock = threading.Lock()

        # 統計
        self._consecutive_failures: int = 0

        # 定期ブリッジ注入
        self._last_bridge_inject: float = 0.0

    # ── setup ────────────────────────────────────────────────────────

    def setup(self, context: Any, lobby_page: Any) -> None:
        self._context = context
        self._lobby_page = lobby_page

        # 全フレームにブリッジを事前注入（新しいフレームにも自動適用）
        try:
            context.add_init_script(_WS_BRIDGE_INIT)
            logger.info("[LIVE] add_init_script OK")
        except Exception as e:
            logger.warning(f"[LIVE] add_init_script failed: {e}")

        # 既存フレームに手動注入
        self._inject_all(lobby_page)

        # ロビーページの game WS を監視
        lobby_page.on("websocket", self._on_ws_event)

        # 新しいポップアップページも監視
        def _on_new_page(page: Any) -> None:
            logger.info(f"[LIVE] new page detected: {page.url[:60]}")
            self._inject_all(page)
            page.on("websocket", self._on_ws_event)
        context.on("page", _on_new_page)

        logger.info("[LIVE] executor ready — waiting for user to enter table")
        self._notify(
            "⏳ テーブル入場待機中\n"
            "Pragmaticロビーで好きなテーブルを\n"
            "クリックして入場してください"
        )

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

        logger.info(f"[LIVE] game WS detected: {url[-100:]}")
        self._game_ws_url = url

        # URL から tableId を抽出
        m = re.search(r'[?&]tableId=([^&]+)', url)
        if m:
            self._table_id = m.group(1)
            logger.info(f"[LIVE] tableId from URL: {self._table_id}")

        # WS メッセージを受動的に取得
        ws.on("framereceived", lambda f: self._on_ws_message(f.body or ""))
        ws.on("framesent",     lambda f: self._on_ws_sent(f.body or ""))

        # phase = ready
        if self._phase == "waiting":
            self._phase = "ready"
            tname = self._table_name or self._table_id or "?"
            self._notify(
                f"🔗 game WS 確立\n"
                f"table: {tname}\n"
                f"BET待機中 (betsopen を待っています)"
            )

    def _on_ws_message(self, data: str) -> None:
        """ゲームWSからの受信メッセージを解析。"""
        if not data:
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
            return

        # XML
        if data.startswith("<"):
            m = re.search(r'userId="([^"]+)"', data)
            if m and not self._user_id:
                self._user_id = m.group(1)
                logger.info(f"[LIVE] user_id from XML: ...{self._user_id[-8:]}")

    def _on_ws_sent(self, data: str) -> None:
        """送信メッセージから tableId / userId を補完。"""
        if not data:
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

        # 10秒ごとにブリッジを再注入（フレーム遷移に備える）
        if now - self._last_bridge_inject > 10.0:
            self._last_bridge_inject = now
            if self._lobby_page:
                self._inject_all(self._lobby_page)
            try:
                for page in (self._context.pages or []):
                    if page != self._lobby_page:
                        self._inject_all(page)
            except Exception:
                pass

        # betsopen タイムアウト監視
        if self._phase == "ready" and self._last_bets_open_at > 0:
            max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "17") or 17)
            if now - self._last_bets_open_at > max_age + 2:
                if self._bets_open_game_id != self._bets_closed_game_id:
                    self._bets_closed_game_id = self._bets_open_game_id

    # ── BET API ──────────────────────────────────────────────────────

    def send_bet(self, side: str, amount: float, table_id: str = "") -> bool:
        """BET を予約する。次の betsopen で送信。"""
        if self._phase == "waiting":
            logger.info(f"[LIVE] send_bet ignored: no table connected yet")
            return False
        if table_id and table_id != self._table_id:
            logger.info(
                f"[LIVE] send_bet ignored: table mismatch "
                f"signal={table_id} current={self._table_id}"
            )
            return False

        with self._lock:
            self._pending_bet = {
                "side": side,
                "amount": amount,
                "table_id": self._table_id,
                "queued_at": time.time(),
            }
        logger.info(f"[LIVE] bet queued: {side} ${amount:.2f} table={self._table_id}")

        # すでに betsopen 中なら即送信
        if self._is_bet_window_open():
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

        side = bet["side"]
        amount = bet["amount"]
        table_id = bet.get("table_id") or self._table_id
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
        self._notify(f"💰 BET 送信\n{side_name} ${amount:.2f}\ntable: {table_id}\ngame: {game_id}")

        result = self._ws_send(table_id, xml)
        ok = isinstance(result, dict) and result.get("ok")

        if ok:
            logger.info(f"[LIVE] bet sent OK ck={ck}")
            self._consecutive_failures = 0
            self._notify(f"✅ BET OK\n{side_name} ${amount:.2f} ck={ck}")
        else:
            logger.error(f"[LIVE] bet send FAILED: {result}")
            self._consecutive_failures += 1
            self._notify(f"❌ BET FAILED\n{side_name} ${amount:.2f}\n{result}")

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

    # ── 互換 API (bot 側から呼ばれる) ────────────────────────────────

    def _request_switch(self, *args, **kwargs) -> None:
        """旧設計の互換。新設計では何もしない。"""
        pass

    def set_table_name(self, table_id: str, table_name: str) -> None:
        """bot から table_name を補完する。"""
        if table_id == self._table_id and not self._table_name:
            self._table_name = table_name

    @property
    def current_table_id(self) -> str:
        return self._table_id

    @property
    def is_ready(self) -> bool:
        return self._phase in ("ready", "betting")
