"""LIVE 用 BetExecutor — ハイブリッド方式 game WS 管理 + ベット送信

アーキテクチャ:
  - Collector の lobby page で dga WS を監視（全テーブル hand 取得）
  - LiveBetExecutor は同じ BrowserContext 内で 1 つだけ table page を管理
  - テーブル入場: lobby DOM クリック → game WS 自動確立（10-20 秒）
  - game WS: betsopen/chat イベント監視 → <lpbet> 送信
  - dga WS: gameResult 受信 → resolve（bot 側で行う）

bet タイムライン:
  dga gameResult N (今の手が完了)
    → ~35-50 秒後
  game WS betsopen N+1 (bet window = ~17 秒)
    → <lpbet> 送信
    → ~20 秒後
  dga gameResult N+1 → resolve

  信号発火から betsopen まで 35-50 秒、テーブル入場に 10-20 秒 → 余裕あり。

Usage:
  executor = LiveBetExecutor()
  bot = DualLinePragmaticBot(..., bet_executor=executor)
  executor.tick()  # bot.run() ループから定期呼出
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

# ── 定数 ─────────────────────────────────────────────────────────────

PRAGMATIC_LOBBY_URL = (
    "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"
)
GAME_WS_PATTERN = "pragmaticplaylive.net/game"

# ── WS Bridge JS (page.evaluate で注入) ─────────────────────────────

_WS_BRIDGE_INIT = r"""
(() => {
  try {
    if (window.__bacopy_ws_bridge_installed) return;
    window.__bacopy_ws_bridge_installed = true;
    window.__bacopy_sockets = [];
    window.__bacopy_ws_events = window.__bacopy_ws_events || [];

    const pushEvent = (ev) => {
      try {
        window.__bacopy_ws_events.push(ev);
        if (window.__bacopy_ws_events.length > 2000) {
          window.__bacopy_ws_events.splice(0, window.__bacopy_ws_events.length - 1000);
        }
      } catch (e) {}
    };

    window.__bacopy_ws_drain = (maxItems) => {
      try {
        const n = (typeof maxItems === 'number' && maxItems > 0) ? maxItems : 300;
        const out = window.__bacopy_ws_events.slice(0, n);
        window.__bacopy_ws_events.splice(0, out.length);
        return out;
      } catch (e) { return []; }
    };

    window.__bacopy_ws_drain_by_url = (urlSubstr, maxItems) => {
      try {
        const n = (typeof maxItems === 'number' && maxItems > 0) ? maxItems : 300;
        const events = window.__bacopy_ws_events || [];
        const matched = [];
        const keep = [];
        for (const ev of events) {
          const u = ev.url || '';
          if (u.includes(urlSubstr) && matched.length < n) {
            matched.push(ev);
          } else {
            keep.push(ev);
          }
        }
        window.__bacopy_ws_events = keep;
        return matched;
      } catch (e) { return []; }
    };

    const OrigWS = window.WebSocket;

    const attachSpy = (ws) => {
      try {
        if (ws.__bacopy_spy_attached) return;
        ws.__bacopy_spy_attached = true;
        const u = ws.__bacopy_url || ws.url || "";
        ws.addEventListener('message', (evt) => {
          try {
            const data = (evt && evt.data !== undefined) ? evt.data : "";
            pushEvent({ts: Date.now(), dir: "recv", url: u, data});
          } catch (e) {}
        });
        const origSend = ws.send;
        ws.send = function(payload) {
          try { pushEvent({ts: Date.now(), dir: "send", url: u, data: payload}); } catch (e) {}
          return origSend.call(ws, payload);
        };
      } catch (e) {}
    };

    window.__bacopy_ws_open = (url) => {
      try {
        const u = String(url || "");
        if (!u) return { ok: false, error: "url_required" };
        for (const ws of (window.__bacopy_sockets || [])) {
          try {
            const wu = ws.__bacopy_url || ws.url || "";
            if (wu === u && ws.readyState !== OrigWS.CLOSED) {
              return { ok: true, existing: true, url: wu, readyState: ws.readyState };
            }
          } catch (e) {}
        }
        const ws = new OrigWS(u);
        ws.__bacopy_url = u;
        window.__bacopy_sockets.push(ws);
        attachSpy(ws);
        return { ok: true, existing: false, url: u, readyState: ws.readyState };
      } catch (e) {
        return { ok: false, error: "open_failed", detail: String(e) };
      }
    };

    function WrappedWebSocket(url, protocols) {
      const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
      try {
        ws.__bacopy_url = url;
        window.__bacopy_sockets.push(ws);
        attachSpy(ws);
      } catch (e) {}
      return ws;
    }
    WrappedWebSocket.prototype = OrigWS.prototype;
    WrappedWebSocket.OPEN = OrigWS.OPEN;
    WrappedWebSocket.CLOSED = OrigWS.CLOSED;
    WrappedWebSocket.CLOSING = OrigWS.CLOSING;
    WrappedWebSocket.CONNECTING = OrigWS.CONNECTING;
    window.WebSocket = WrappedWebSocket;

    window.__bacopy_ws_send = (match, payload) => {
      const urls = [];
      for (const ws of (window.__bacopy_sockets || [])) {
        try {
          const u = ws.__bacopy_url || ws.url || "";
          if (u) urls.push(u);
          if (ws.readyState === OrigWS.OPEN && u.includes(match)) {
            ws.send(payload);
            return { ok: true, url: u };
          }
        } catch (e) {}
      }
      return { ok: false, error: "ws_not_found", known: urls.slice(0, 50) };
    };

    window.__bacopy_ws_has = (urlSubstr) => {
      for (const ws of (window.__bacopy_sockets || [])) {
        try {
          const u = ws.__bacopy_url || ws.url || "";
          if (u.includes(urlSubstr) && ws.readyState === OrigWS.OPEN) return true;
        } catch (e) {}
      }
      return false;
    };
  } catch (e) {}
})();
"""

# ── ヘルパー ─────────────────────────────────────────────────────────


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _side_to_bc(side: str) -> str:
    s = str(side or "").upper().strip()
    if s in ("P", "PLAYER"):
        return (os.getenv("BACOPY_PRAGMATIC_BC_PLAYER", "") or "0").strip() or "0"
    if s in ("B", "BANKER"):
        return (os.getenv("BACOPY_PRAGMATIC_BC_BANKER", "") or "1").strip() or "1"
    return "0"


def _build_lpbet_xml(
    *, table_id: str, game_id: str, user_id: str, bc: str, amount: float
) -> str:
    ck = str(_epoch_ms())
    amt = str(int(amount)) if float(amount).is_integer() else str(amount)
    return (
        f'<command channel="table-{table_id}">'
        f'<lpbet gm="baccarat_desktop" gId="{game_id}" uId="{user_id}" ck="{ck}"  >'
        f'<bet amt="{amt}" bc="{bc}" ck="{ck}"/>'
        f"</lpbet></command>"
    )


def _extract_ck_from_lpbet_xml(xml_payload: str) -> str:
    try:
        m = re.search(r'\bck="(\d{8,})"', str(xml_payload or ""))
        return m.group(1) if m else ""
    except Exception:
        return ""


def _find_game_frame(page) -> Any:
    """Pragmatic game iframe を探す。"""
    for fr in page.frames:
        try:
            if "pragmaticplaylive.net/game" in (fr.url or ""):
                return fr
        except Exception:
            continue
    return None


def _find_lobby_frames(page) -> list:
    """Stake lobby iframe を探す。"""
    out = []
    for fr in page.frames:
        try:
            u = str(fr.url or "")
            if "pragmaticplaylive" in u and ("lobby" in u or "shell-app" in u):
                out.append(fr)
        except Exception:
            continue
    return out


# ── JS: lobby click ──────────────────────────────────────────────────

_LOBBY_CLICK_JS = r"""
(args) => {
  const qpid = String(args.qpid || '');
  const tableId = String(args.tableId || '');
  const candidates = (Array.isArray(args.candidates) ? args.candidates : [])
    .map(s => String(s || '').trim()).filter(Boolean);

  const tryClick = (el) => {
    try {
      if (el.click) el.click();
      else {
        const ev = new MouseEvent('click', {bubbles: true, cancelable: true});
        el.dispatchEvent(ev);
      }
      return true;
    } catch (e) { return false; }
  };

  const matches = (el) => {
    const ds = (el && el.dataset) || {};
    const attrs = [];
    try {
      for (const a of (el.getAttributeNames ? el.getAttributeNames() : [])) {
        attrs.push(el.getAttribute(a) || '');
      }
    } catch(e) {}
    const text = [
      el.innerText || '', el.textContent || '',
      el.href || '', el.src || '',
      ds.tableId || '', ds.tableid || '', ds.table || '',
      ...attrs
    ].join(' ');

    if (qpid && text.includes(qpid)) return true;
    if (tableId && text.includes(tableId)) return true;
    for (const c of candidates) {
      if (c && text.includes(c)) return true;
    }
    return false;
  };

  // Strategy 1: data attributes
  const all = document.querySelectorAll(
    'a, button, [role="button"], [data-table-id], [data-tableid], [data-table-name], img'
  );
  for (const el of all) {
    if (matches(el)) {
      tryClick(el);
      return { clicked: true, strategy: 'attr', text: (el.innerText || '').slice(0, 60) };
    }
  }

  // Strategy 2: broader search with scrolling
  const maxScroll = Number(args.maxScroll || 0) || 0;
  for (let s = 0; s <= maxScroll; s++) {
    if (s > 0) {
      window.scrollBy(0, 300);
      // yield to render
      const start = Date.now();
      while (Date.now() - start < 500) { /* busy-wait for rendering */ }
    }
    const all2 = document.querySelectorAll(
      'a, button, [role="button"], img, [class*="table"], [class*="Table"], [class*="game"]'
    );
    for (const el of all2) {
      if (matches(el)) {
        tryClick(el);
        return { clicked: true, strategy: 'scroll', scroll: s, text: (el.innerText || '').slice(0, 60) };
      }
    }
  }

  return { clicked: false, qpid, tableId, candidates, elementsChecked: all.length };
}
"""


# ── LiveBetExecutor ───────────────────────────────────────────────────


class LiveBetExecutor:
    """LIVE 用 BetExecutor — ハイブリッド方式。

    管理する状態:
      - _context: Playwright BrowserContext
      - _lobby_page: Collector の lobby page (dga WS 監視中)
      - _table_page: 現在アクティブな table page (or None)
      - _table_id: 現在 game WS が接続しているテーブルの table_id
      - _phase: "idle" | "entering" | "ready" | "betting"
    """

    def __init__(self, notify_fn=None):
        self._notify = notify_fn or (lambda text: None)
        self._context: Any = None
        self._lobby_page: Any = None
        self._table_page: Any = None
        self._table_id: str = ""
        self._phase: str = "idle"  # idle | entering | ready | betting

        # game WS 状態
        self._bets_open_game_id: str = ""
        self._bets_closed_game_id: str = ""
        self._last_bets_open_at: float = 0.0
        self._user_id: str = ""
        self._game_id: str = ""  # current game_id from {"game":{"id":...}}

        # bet 管理
        self._pending_bet: dict | None = None
        self._pending_switch_to: str = ""   # 切替先 table_id
        self._pending_switch_name: str = ""  # 切替先 table_name
        self._bet_id_counter: int = 0
        self._bet_result: dict | None = None

        # 入場状態
        self._enter_started_at: float = 0.0
        self._enter_deadline: float = 0.0
        self._game_ws_detected: bool = False

        # 統計
        self._consecutive_failures: int = 0

    # ── BetExecutor Protocol ─────────────────────────────────────

    @property
    def is_live(self) -> bool:
        return True

    def place_bet(
        self, table_id: str, side: str, amount: float, metadata: dict
    ) -> str | None:
        """ベットを予約する。

        table_id の game WS に未接続なら切替をトリガー。
        """
        self._bet_id_counter += 1
        bet_id = f"live_{self._bet_id_counter}"
        self._pending_bet = {
            "bet_id": bet_id,
            "table_id": table_id,
            "side": side,
            "amount": amount,
            "metadata": metadata,
            "scheduled_at": time.time(),
        }

        # 別テーブルなら切替
        if table_id != self._table_id:
            logger.info(
                f"[LIVE] bet scheduled on table {table_id} "
                f"(current={self._table_id or 'none'}), requesting switch"
            )
            self._request_switch(table_id)

        return bet_id

    def get_last_bet_result(self) -> dict | None:
        r = self._bet_result
        self._bet_result = None
        return r

    # ── ライフサイクル ──────────────────────────────────────────

    def setup(self, context: Any, lobby_page: Any) -> None:
        """Collector の run() から呼ばれる。"""
        self._context = context
        self._lobby_page = lobby_page
        logger.info("[LIVE] executor setup complete")

    def _request_switch(self, table_id: str, table_name: str = "") -> None:
        """テーブル切替をリクエスト。"""
        if self._phase == "entering" and self._table_id == table_id:
            return  # すでに入場中
        self._pending_switch_to = table_id
        self._pending_switch_name = table_name
        logger.info(
            f"[LIVE] switch requested: {table_id} name={table_name!r} (current={self._table_id or 'none'})"
        )

    def tick(self) -> None:
        """bot.run() ループから定期呼出 (1 秒間隔)。"""
        if not self._context:
            return

        # ── テーブル切替 ──
        if self._pending_switch_to and self._phase != "entering":
            if self._phase == "betting":
                # bet 中は切替しない
                pass
            else:
                self._begin_enter(self._pending_switch_to)

        # ── 入場中: game WS 確立待ち ──
        if self._phase == "entering":
            self._poll_enter()

        # ── ready: game WS イベント drain ──
        if self._phase == "ready" and self._table_page:
            try:
                self._drain_game_ws()
            except Exception as e:
                logger.debug(f"[LIVE] drain error: {e}")

        # ── betting: bet 実行後の結果監視 ──
        if self._phase == "betting":
            try:
                self._drain_game_ws()
            except Exception:
                pass

        # ── タイムアウト検知 ──
        if self._phase == "entering" and time.time() > self._enter_deadline:
            elapsed = time.time() - self._enter_started_at
            logger.error(
                f"[LIVE] table entry timeout for {self._table_id} "
                f"({elapsed:.0f}s)"
            )
            self._notify(f"⏱ 入場タイムアウト\ntable: {self._table_id}\n{elapsed:.0f}秒で game WS 未確立\n→ 次の WARM/HOT テーブルで再試行")
            self._phase = "idle"
            self._table_id = ""

        # ── pending bet があるのに game_id 取得できたら bet 実行 ──
        if (
            self._phase == "ready"
            and self._pending_bet
            and self._is_bet_window_open()
        ):
            self._execute_bet()

    # ── テーブル入場 ────────────────────────────────────────────

    def _begin_enter(self, table_id: str) -> None:
        """テーブル入場を開始する。"""
        self._pending_switch_to = ""

        # 既存 page を閉じる
        if self._table_page:
            try:
                self._table_page.close()
            except Exception:
                pass
            self._table_page = None

        self._table_id = table_id
        self._phase = "entering"
        self._enter_started_at = time.time()
        self._enter_deadline = self._enter_started_at + 90.0
        self._game_ws_detected = False
        self._bets_open_game_id = ""
        self._bets_closed_game_id = ""
        self._last_bets_open_at = 0.0
        self._game_id = ""

        logger.info(f"[LIVE] entering table {table_id}")
        self._notify(f"🚪 テーブル入場開始\ntable_id: {table_id}")

        try:
            page = self._context.new_page()
            self._table_page = page

            page.goto(PRAGMATIC_LOBBY_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)

            # WS bridge 注入
            page.evaluate(_WS_BRIDGE_INIT)
            for fr in page.frames:
                try:
                    fr.evaluate(_WS_BRIDGE_INIT)
                except Exception:
                    pass

            # lobby iframe 出現待ち (最大 10 秒)
            lobby_ok = False
            for _ in range(20):
                page.wait_for_timeout(500)
                if _find_lobby_frames(page):
                    lobby_ok = True
                    break
            if not lobby_ok:
                logger.warning(f"[LIVE] lobby iframe not found for table {table_id}")

            # table id / table name を click
            self._click_table(page, table_id, self._pending_switch_name)

            # ブラウザ②を右側・大きめに固定配置（ユーザーが見やすいよう）
            try:
                page.evaluate(
                    "() => { window.moveTo(640, 0); window.resizeTo(1280, 960); }"
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[LIVE] begin_enter failed: {e}")
            self._phase = "idle"
            self._table_id = ""

    def _click_table(self, page: Any, table_id: str, table_name: str = "") -> None:
        """lobby DOM から table を探して click。テーブル名も候補に含める。"""
        candidates = [c for c in [table_id, table_name] if c]
        for fr in _find_lobby_frames(page):
            try:
                res = fr.evaluate(_LOBBY_CLICK_JS, {
                    "qpid": "",
                    "tableId": table_id,
                    "candidates": candidates,
                    "maxScroll": 10,
                })
                if isinstance(res, dict) and res.get("clicked"):
                    logger.info(
                        f"[LIVE] clicked table {table_id}: "
                        f"strategy={res.get('strategy')} text={res.get('text', '')[:40]}"
                    )
                    page.wait_for_timeout(2000)
                    return
            except Exception as e:
                logger.debug(f"[LIVE] click attempt on frame failed: {e}")

        # fallback: page 直接
        try:
            page.evaluate(_LOBBY_CLICK_JS, {
                "qpid": "",
                "tableId": table_id,
                "candidates": candidates,
                "maxScroll": 5,
            })
            page.wait_for_timeout(2000)
        except Exception:
            pass

    def _poll_enter(self) -> None:
        """入場中: game WS が開いたか確認。"""
        if not self._table_page:
            return
        try:
            has_game = self._table_page.evaluate(
                "() => window.__bacopy_ws_has ? "
                "window.__bacopy_ws_has('pragmaticplaylive.net/game') : false"
            )
        except Exception:
            has_game = False

        if has_game and not self._game_ws_detected:
            self._game_ws_detected = True
            logger.info(f"[LIVE] game WS detected for table {self._table_id}")
            self._phase = "ready"
            self._notify(f"🔗 game WS 確立\ntable: {self._table_id}\nBET待機中...")
            # user_id を chat WS から抽出試行
            self._extract_user_id()

    def _extract_user_id(self) -> None:
        """game WS イベントから user_id を抽出。"""
        if not self._table_page or self._user_id:
            return
        try:
            events = self._table_page.evaluate(
                "() => window.__bacopy_ws_drain ? "
                "window.__bacopy_ws_drain(200) : []"
            ) or []
        except Exception:
            return

        for ev in events:
            if not isinstance(ev, dict):
                continue
            data = ev.get("data", "")
            if not isinstance(data, str) or not data:
                continue
            # chat SUBSCRIBE XML から userId 抽出
            m = re.search(r'userId="([^"]+)"', data)
            if m:
                self._user_id = m.group(1)
                logger.info(f"[LIVE] user_id extracted: ...{self._user_id[-8:]}")
                return
            # JSON ALERT_JOINED から userId
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict):
                u = obj.get("user") or {}
                uid = str(u.get("userId") or "")
                if uid:
                    self._user_id = uid
                    logger.info(f"[LIVE] user_id extracted: ...{uid[-8:]}")
                    return

    # ── game WS イベント drain ──────────────────────────────────

    def _drain_game_ws(self) -> None:
        if not self._table_page:
            return
        try:
            events = self._table_page.evaluate(
                "() => window.__bacopy_ws_drain_by_url ? "
                "window.__bacopy_ws_drain_by_url('pragmaticplaylive.net/game', 100) : []"
            ) or []
        except Exception:
            return

        for ev in events:
            if not isinstance(ev, dict):
                continue
            if str(ev.get("dir", "")).lower() != "recv":
                continue

            data = ev.get("data", "")
            if not data or not isinstance(data, str):
                continue

            # XML
            if data.startswith("<"):
                self._parse_game_xml(data)
                continue

            # JSON
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            self._parse_game_json(obj)

    def _parse_game_json(self, obj: dict) -> None:
        # betsopen
        if "betsopen" in obj and isinstance(obj["betsopen"], dict):
            gid = str(obj["betsopen"].get("game") or "")
            if gid:
                was_new = (gid != self._bets_open_game_id)
                self._bets_open_game_id = gid
                self._last_bets_open_at = time.time()
                if was_new:
                    logger.info(
                        f"[LIVE] betsopen game_id={gid} "
                        f"table={self._table_id}"
                    )
                    self._notify(f"🟢 BET WINDOW OPEN\ntable: {self._table_id}\ngame: {gid}\nBET受付中 (~17秒)")
            return

        # betsclosed
        if "betsclosed" in obj and isinstance(obj["betsclosed"], dict):
            gid = str(obj["betsclosed"].get("game") or "")
            if gid:
                self._bets_closed_game_id = gid
            return

        # game
        if "game" in obj and isinstance(obj["game"], dict):
            gid = str(obj["game"].get("id") or "")
            if gid:
                self._game_id = gid
            return

        # user / ALERT_JOINED
        u = obj.get("user") if isinstance(obj.get("user"), dict) else {}
        uid = str(u.get("userId") or "")
        if uid and not self._user_id:
            self._user_id = uid
            logger.info(f"[LIVE] user_id from ALERT_JOINED: ...{uid[-8:]}")

    def _parse_game_xml(self, data: str) -> None:
        m = re.search(r'userId="([^"]+)"', data)
        if m and not self._user_id:
            self._user_id = m.group(1)
            logger.info(f"[LIVE] user_id from chat XML: ...{self._user_id[-8:]}")

    # ── bet window 判定 ─────────────────────────────────────────

    def _is_bet_window_open(self) -> bool:
        if not self._bets_open_game_id:
            return False
        if self._bets_open_game_id == self._bets_closed_game_id:
            return False
        if not self._last_bets_open_at:
            return False
        age = time.time() - self._last_bets_open_at
        max_age = float(os.getenv("BACOPY_BET_WINDOW_OPEN_MAX_SEC", "17") or 17)
        return age < max_age

    # ── ベット実行 ──────────────────────────────────────────────

    def _execute_bet(self) -> None:
        if not self._pending_bet or not self._table_page:
            return

        bet = self._pending_bet
        game_id = self._bets_open_game_id
        user_id = self._user_id or os.getenv("BACOPY_USER_ID", "").strip()
        side = bet["side"]
        amount = bet["amount"]
        table_id = bet["table_id"]
        bc = _side_to_bc(side)

        if not game_id:
            return
        if not user_id:
            logger.warning("[LIVE] no user_id, deferring bet")
            return

        self._phase = "betting"
        logger.info(
            f"[LIVE] executing bet: {bet['bet_id']} "
            f"table={table_id} side={side} amount=${amount} game_id={game_id}"
        )
        side_name = "BANKER" if side in ("B", "BANKER") else "PLAYER"
        self._notify(f"💰 BET 送信中\ntable: {table_id}\n{side_name} ${amount}\ngame: {game_id}")

        xml = _build_lpbet_xml(
            table_id=table_id,
            game_id=game_id,
            user_id=user_id,
            bc=bc,
            amount=amount,
        )
        ck = _extract_ck_from_lpbet_xml(xml)

        try:
            match = f"tableId={table_id}"
            result = self._table_page.evaluate(
                "(args) => window.__bacopy_ws_send(args.match, args.payload)",
                {"match": match, "payload": xml},
            )
        except Exception as e:
            logger.error(f"[LIVE] ws_send failed: {e}")
            result = {"ok": False, "error": str(e)}
            self._consecutive_failures += 1

        ok = isinstance(result, dict) and result.get("ok")
        if ok:
            logger.info(f"[LIVE] bet sent OK: {bet['bet_id']} ck={ck}")
            self._consecutive_failures = 0
            self._notify(f"✅ BET 送信 OK\n{side_name} ${amount} ck={ck}")
        else:
            logger.error(f"[LIVE] bet send failed: {result}")
            self._notify(f"❌ BET 送信 失敗\n{side_name} ${amount}\nerror: {result}")

        self._bet_result = {
            "bet_id": bet["bet_id"],
            "ck": ck,
            "game_id": game_id,
            "side": side,
            "amount": amount,
            "send_ok": ok,
            "sent_at": time.time(),
        }
        self._pending_bet = None
        # bet 後は ready に戻る
        self._phase = "ready"

    # ── 状態取得 ────────────────────────────────────────────────

    def is_on_table(self, table_id: str) -> bool:
        """指定 table の game WS に接続中か。"""
        return self._table_id == table_id and self._phase in ("ready", "betting")

    def can_switch(self) -> bool:
        """切替可能か（bet 中でない）。"""
        return self._phase in ("idle", "ready")
