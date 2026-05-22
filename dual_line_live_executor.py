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
import sqlite3
import threading
import time
import base64
from typing import Any

# bot と同じロガーを使うことでログファイルへの書き込みを保証する
logger = logging.getLogger("dual_line.bot")

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
    const m = String(match || '').toLowerCase();
    const openSockets = [];

    for (const ws of window.__bacopy_sockets) {
      try {
        if (ws.readyState === 1 && ws.url) {
          openSockets.push(ws);
          const u = String(ws.url || '');
          if (m && u.toLowerCase().includes(m)) {
            ws.send(payload);
            return { ok: true, url: u.slice(-120), mode: 'match' };
          }
        }
      } catch(_) {}
    }

    const tm = m.match(/tableid=([^&]+)/);
    const tid = tm ? tm[1] : '';
    if (tid) {
      for (const ws of openSockets) {
        try {
          const u = String(ws.url || '');
          const ul = u.toLowerCase();
          if (ul.includes(tid) || ul.includes(encodeURIComponent(tid))) {
            ws.send(payload);
            return { ok: true, url: u.slice(-120), mode: 'tid' };
          }
        } catch(_) {}
      }
    }

    const pragmatic = openSockets.filter((ws) => {
      try {
        const u = String(ws.url || '');
        return /pragmaticplaylive\.net\/game/i.test(u);
      } catch(_) {
        return false;
      }
    });
    if (pragmatic.length === 1) {
      try {
        const u = String(pragmatic[0].url || '');
        pragmatic[0].send(payload);
        return { ok: true, url: u.slice(-120), mode: 'single_pragmatic' };
      } catch(_) {}
    }

    return {
      ok: false,
      sockets: window.__bacopy_sockets.length,
      open: openSockets.length,
      sample: openSockets.slice(0, 3).map((ws) => {
        try { return String(ws.url || '').slice(-120); } catch(_) { return ''; }
      }),
    };
  };

  window.__bacopy_ws_urls = () =>
    window.__bacopy_sockets.map(ws => ({
      url: ws.url, readyState: ws.readyState
    }));

  // Pragmatic の WS は Worker 内で開かれるため __bacopy_sockets に入らない。
  // このヘルパーでページコンテキストから同じ URL に並列 WS を開いて送信する。
  window.__bacopy_ws_open = (url) => {
    try {
      const u = String(url || '');
      if (!u) return { ok: false, error: 'url_required' };
      for (const ws of window.__bacopy_sockets) {
        try {
          if (ws.url === u && ws.readyState !== 3) {
            return { ok: true, existing: true, url: u, readyState: ws.readyState };
          }
        } catch(_) {}
      }
      const ws = new OrigWS(u);
      window.__bacopy_sockets.push(ws);
      return { ok: true, existing: false, url: u, readyState: ws.readyState };
    } catch(e) {
      return { ok: false, error: 'open_failed', detail: String(e) };
    }
  };

  window.__bacopy_ws_open_send = async (url, payload, timeoutMs) => {
    try {
      const u = String(url || '');
      if (!u) return { ok: false, error: 'url_required' };
      for (const ws of window.__bacopy_sockets) {
        try {
          if (ws.url === u && ws.readyState === 1) {
            ws.send(payload);
            return { ok: true, url: u, reused: true };
          }
        } catch(_) {}
      }
      const res = window.__bacopy_ws_open(u);
      if (!res || !res.ok) return res || { ok: false, error: 'open_failed' };
      const tmo = (typeof timeoutMs === 'number' && timeoutMs > 0) ? timeoutMs : 5000;
      const t0 = Date.now();
      while (Date.now() - t0 < tmo) {
        for (const ws of window.__bacopy_sockets) {
          try {
            if (ws.url === u && ws.readyState === 1) {
              ws.send(payload);
              return { ok: true, url: u, reused: false };
            }
          } catch(_) {}
        }
        await new Promise(r => setTimeout(r, 50));
      }
      return { ok: false, error: 'open_timeout', url: u };
    } catch(e) {
      return { ok: false, error: 'exception', detail: String(e) };
    }
  };

  // ── Worker WebSocket ブリッジ ───────────────────────────────────────
  // Pragmatic の game WS は Web Worker 内で開かれるため window.__bacopy_sockets
  // に入らない。Worker コンストラクタを横取りし、ラッパー blob で Worker を生成する。
  // ラッパーは WebSocket を上書きして WS を捕捉し、postMessage でベット送信を受け付ける。
  if (!window.__bacopy_worker_bridge_installed && window.Worker) {
    window.__bacopy_worker_bridge_installed = true;
    const _OrigWorker = window.Worker;
    const _bworkers = [];

    const _WINJ = (
      'var _bw=self.WebSocket,_bws=null;' +
      'self.WebSocket=function(u,p){var ws=p?new _bw(u,p):new _bw(u);' +
      'if(/pragmaticplaylive\\.net\\/game/i.test(String(u||""))){_bws=ws;}return ws;};' +
      'self.WebSocket.prototype=_bw.prototype;' +
      'self.addEventListener("message",function(e){' +
      'try{var d=e&&e.data;' +
      'if(d&&d.__bcmd==="bwsend"&&_bws&&_bws.readyState===1){_bws.send(d.p);}' +
      '}catch(_){}});'
    );

    window.__bacopy_worker_urls = [];

    window.Worker = function(url, opts) {
      var u = String(url || '');
      window.__bacopy_worker_urls.push(u.slice(0, 120));
      var code = _WINJ + '\ntry{importScripts(' + JSON.stringify(u) + ');}catch(e){console.warn("[bacopy-worker]importScripts failed",String(e));}';
      var blob = new Blob([code], {type: 'application/javascript'});
      var bu = (window.URL || window.webkitURL).createObjectURL(blob);
      var w;
      try {
        w = opts ? new _OrigWorker(bu, opts) : new _OrigWorker(bu);
      } catch(e) {
        w = opts ? new _OrigWorker(url, opts) : new _OrigWorker(url);
      }
      _bworkers.push(w);
      return w;
    };
    window.Worker.prototype = _OrigWorker.prototype;

    // SharedWorker も捕捉する
    if (window.SharedWorker) {
      var _OrigShared = window.SharedWorker;
      window.SharedWorker = function(url, opts) {
        window.__bacopy_worker_urls.push('SHARED:' + String(url || '').slice(0, 100));
        return opts ? new _OrigShared(url, opts) : new _OrigShared(url);
      };
      window.SharedWorker.prototype = _OrigShared.prototype;
    }

    window.__bacopy_worker_ws_send = function(payload) {
      var sent = 0;
      for (var i = 0; i < _bworkers.length; i++) {
        try { _bworkers[i].postMessage({__bcmd: 'bwsend', p: payload}); sent++; } catch(e) {}
      }
      return {ok: sent > 0, workers: _bworkers.length, sent: sent};
    };
  }
})();
"""

_MULTI_LOBBY_FOCUS_JS = r"""
async (args) => {
  const qpid = String((args && args.qpid) || '').trim();
  const click = !!(args && args.click);
  const maxScroll = Math.max(1, Number((args && args.maxScroll) || 48));
  const hintIndex = Number.isFinite(Number(args && args.hintIndex)) ? Number(args && args.hintIndex) : -1;
  const hintTotal = Math.max(0, Number((args && args.hintTotal) || 0));
  const candidates = Array.isArray(args && args.candidates) ? args.candidates : [];
  const norm = (s) => String(s || '').replace(/\s+/g, '').replace(/[$￥¥]/g, '').toLowerCase();
  const candNorm = candidates.map(norm).filter(Boolean);

  function containsQpid(v) {
    const s = String(v || '');
    if (!qpid || !s) return false;
    return s.includes('/snaps/' + qpid + '/') || s.includes('tableId=' + qpid) || s.includes('tableid=' + qpid) || s === qpid;
  }
  function isCandidateText(text) {
    const t = norm(text);
    if (!t) return false;
    for (const c of candNorm) {
      if (t === c || t.includes(c) || c.includes(t)) return true;
    }
    return false;
  }
  function textOf(el) {
    const raw = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!raw) return '';
    const i = raw.indexOf('\n');
    if (i > 0) return raw.slice(0, i).trim();
    return raw;
  }
  function clickableAncestor(el) {
    let cur = el;
    for (let i = 0; i < 8 && cur; i++) {
      const tag = (cur.tagName || '').toLowerCase();
      const role = cur.getAttribute ? (cur.getAttribute('role') || '') : '';
      if (role === 'button' || tag === 'button' || tag === 'a') return cur;
      cur = cur.parentElement;
    }
    return el;
  }
  function clickEl(el) {
    try { el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
    const cx = r.left + (r.width || 0) / 2;
    const cy = r.top + (r.height || 0) / 2;
    const base = { bubbles:true, cancelable:true, composed:true, clientX:cx, clientY:cy, view:window };
    try {
      if (window.PointerEvent) {
        el.dispatchEvent(new PointerEvent('pointerdown', base));
        el.dispatchEvent(new PointerEvent('pointerup', base));
      }
      el.dispatchEvent(new MouseEvent('mousedown', base));
      el.dispatchEvent(new MouseEvent('mouseup', base));
      el.dispatchEvent(new MouseEvent('click', base));
      el.click();
    } catch(_) {}
  }
  function findTarget() {
    const sels = '[role="button"], button, a, div[role="button"], [data-testid*="table"], [data-testid*="lobby"]';
    const nodes = document.querySelectorAll(sels);
    let idx = 0;
    for (const el of nodes) {
      try {
        if (qpid) {
          if (containsQpid(el.getAttribute && el.getAttribute('data-qpid'))) return { el: clickableAncestor(el), idx, total: nodes.length };
          if (containsQpid(el.getAttribute && el.getAttribute('data-table-id'))) return { el: clickableAncestor(el), idx, total: nodes.length };
          if (containsQpid(el.getAttribute && el.getAttribute('data-tableid'))) return { el: clickableAncestor(el), idx, total: nodes.length };
          if (containsQpid(el.getAttribute && el.getAttribute('href'))) return { el: clickableAncestor(el), idx, total: nodes.length };
          if (containsQpid(el.getAttribute && el.getAttribute('src'))) return { el: clickableAncestor(el), idx, total: nodes.length };
          if (containsQpid(el.getAttribute && el.getAttribute('style'))) return { el: clickableAncestor(el), idx, total: nodes.length };
          try {
            const cs = getComputedStyle(el);
            if (containsQpid(cs && cs.backgroundImage)) return { el: clickableAncestor(el), idx, total: nodes.length };
          } catch(_) {}
          try {
            if (containsQpid(el.innerHTML || '')) return { el: clickableAncestor(el), idx, total: nodes.length };
          } catch(_) {}
        }
        if (isCandidateText(textOf(el))) return { el: clickableAncestor(el), idx, total: nodes.length };
      } catch(_) {}
      idx += 1;
    }
    return { el: null, idx: -1, total: nodes.length };
  }
  function pickScroller() {
    let best = document.scrollingElement || document.documentElement || document.body;
    let bestDelta = (best && best.scrollHeight ? (best.scrollHeight - best.clientHeight) : 0);
    const all = document.querySelectorAll('*');
    for (const el of all) {
      try {
        const cs = getComputedStyle(el);
        if (!/(auto|scroll)/.test(cs.overflowY || '')) continue;
        const delta = (el.scrollHeight || 0) - (el.clientHeight || 0);
        if (delta > bestDelta + 50) {
          best = el;
          bestDelta = delta;
        }
      } catch(_) {}
    }
    return best;
  }
  function scrollStep(sc, down=true) {
    if (!sc) return;
    const delta = Math.max(240, (sc.clientHeight || 500) * 0.8) * (down ? 1 : -1);
    try { sc.scrollTop = Math.max(0, Math.min((sc.scrollHeight || 99999), (sc.scrollTop || 0) + delta)); } catch(_) {}
    try {
      const r = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
      const ev = new WheelEvent('wheel', {deltaY: delta, bubbles:true, cancelable:true, composed:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2});
      sc.dispatchEvent(ev);
    } catch(_) {}
  }
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const initScroller = pickScroller();
  if (initScroller && hintIndex >= 0 && hintTotal > 0) {
    try {
      const ratio = Math.max(0, Math.min(1, hintIndex / Math.max(1, hintTotal)));
      const span = Math.max(0, (initScroller.scrollHeight || 0) - (initScroller.clientHeight || 0));
      initScroller.scrollTop = Math.max(0, Math.min(span, Math.floor(span * ratio)));
    } catch(_) {}
  }

  for (let i = 0; i < maxScroll; i++) {
    const t = findTarget();
    if (t && t.el) {
      try { t.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
      if (click) clickEl(t.el);
      return { ok:true, found:true, clicked: !!click, matchIndex: Number(t.idx), totalNodes: Number(t.total) };
    }
    scrollStep(pickScroller(), true);
    await sleep(70);
  }
  for (let i = 0; i < maxScroll; i++) {
    const t = findTarget();
    if (t && t.el) {
      try { t.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
      if (click) clickEl(t.el);
      return { ok:true, found:true, clicked: !!click, matchIndex: Number(t.idx), totalNodes: Number(t.total) };
    }
    scrollStep(pickScroller(), false);
    await sleep(70);
  }
  return { ok:true, found:false, clicked:false, matchIndex:-1, totalNodes:0 };
}
"""

_MULTI_LOBBY_ENSURE_TAB_JS = r"""
() => {
  const labels = ['マルチ', 'Multi', 'Multiplayer', 'Multi Play', 'Multi-Play'];
  const nodes = document.querySelectorAll('[role="tab"], [role="button"], button, a');
  let found = null;
  for (const el of nodes) {
    try {
      const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      if (!txt) continue;
      const low = txt.toLowerCase();
      const jp = txt.replace(/\s+/g, '');
      const matched =
        labels.some(l => low.includes(String(l).toLowerCase())) ||
        (jp.includes('マルチ') && (jp.includes('プレイ') || jp.includes('レイ')));
      if (!matched) continue;
      found = el;
      const sel = (el.getAttribute('aria-selected') || '').toLowerCase() === 'true'
        || (el.getAttribute('data-state') || '').toLowerCase() === 'active'
        || /\bactive\b/i.test(el.className || '');
      if (!sel) {
        try { el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
        try { el.click(); } catch(_) {}
        return { ok:true, clicked:true, active:false, text:txt.slice(0, 80) };
      }
      return { ok:true, clicked:false, active:true, text:txt.slice(0, 80) };
    } catch(_) {}
  }
  return { ok:true, clicked:false, active:false, text: found ? 'found' : '' };
}
"""

# 「再接続しています」テキストを全フレームのテキストノードから検索する。
# 見つかった場合は true を返す。
_RECONNECTING_DETECT_JS = r"""
() => {
  const NEEDLE = '再接続';
  function hasText(doc) {
    try {
      const walker = doc.createTreeWalker(doc.body || doc, 0x4 /* NodeFilter.SHOW_TEXT */);
      let node;
      while ((node = walker.nextNode())) {
        if (node.nodeValue && node.nodeValue.includes(NEEDLE)) return true;
      }
    } catch(_) {}
    return false;
  }
  if (hasText(document)) return true;
  try {
    const frames = document.querySelectorAll('iframe');
    for (const fr of frames) {
      try {
        if (fr.contentDocument && hasText(fr.contentDocument)) return true;
      } catch(_) {}
    }
  } catch(_) {}
  return false;
}
"""

# ── ヘルパー ──────────────────────────────────────────────────────────

def _side_to_bc(side: str) -> str:
    return "B" if side.upper() in ("B", "BANKER") else "P"


def _build_lpbet_xml(*, table_id: str, game_id: str, user_id: str,
                     bc: str, amount: float) -> str:
    ck = str(int(time.time() * 1000))
    amt = str(int(amount)) if float(amount).is_integer() else str(amount)
    return (
        f'<command channel="table-{table_id}">'
        f'<lpbet gm="baccarat_desktop" gId="{game_id}" uId="{user_id}" ck="{ck}"  >'
        f'<bet amt="{amt}" bc="{bc}" ck="{ck}"/>'
        f'</lpbet></command>'
    )


def _extract_ck(xml: str) -> str:
    m = re.search(r'\bck="(\d{8,})"', xml)
    if not m:
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

        # keep-alive / inactivity / WS silence / session-elsewhere / reconnect 対策
        self._last_keep_alive_at: float = 0.0
        self._last_game_ws_recv_at: float = 0.0
        self._last_inactivity_check_at: float = 0.0
        self._last_session_elsewhere_check_at: float = 0.0
        self._last_reconnecting_check_at: float = 0.0
        self._reconnecting_first_at: float = 0.0   # 再接続しています 最初の検出時刻
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
        self._switch_target_table_id: str = ""
        self._multi_lobby_mode: bool = os.getenv("BACOPY_MULTI_LOBBY_MODE", "1") != "0"
        self._table_states: dict[str, dict[str, Any]] = {}
        self._last_multi_area_ensure_at: float = 0.0
        self._multi_area_ready: bool = False
        self._sent_bet_ids: set[str] = set()
        self._bet_send_in_progress: bool = False
        self._lock = threading.Lock()
        self._owner_thread_id: int = threading.get_ident()

        # 統計
        self._consecutive_failures: int = 0

        # 定期ブリッジ注入
        self._last_bridge_inject: float = 0.0

        # 診断用: 定期 state dump
        self._last_state_dump_at: float = 0.0
        self._is_multi_table_ws: bool = False  # multiTable=true WS が確立されたら True
        self._last_ws_reload_at: float = 0.0   # WS 死亡リロード最終実行時刻
        self._last_user_id_probe_at: float = 0.0
        self._table_focus_cache: dict[str, dict[str, Any]] = {}
        self._last_focus_cache_save_at: float = 0.0
        # Playwright route_web_socket プロキシ（multi-lobby BET 送信用）
        self._ws_proxy_server: Any = None      # WebSocketRoute server side
        self._ws_proxy_route: Any = None       # WebSocketRoute client side

    # ── setup ────────────────────────────────────────────────────────

    def setup(self, context: Any, lobby_page: Any, bet_page: Any | None = None) -> None:
        self._context = context
        self._lobby_page = lobby_page
        self._bet_page = bet_page or lobby_page
        logger.info(f"[EXEC-SETUP] setup called. multi_lobby={self._multi_lobby_mode} lobby_page={getattr(lobby_page,'url','?')[:80]}")
        self._try_discover_user_id()

        try:
            context.add_init_script(_WS_BRIDGE_INIT)
            logger.info("[EXEC-SETUP] WS bridge pre-installed via add_init_script OK")
        except Exception as e:
            logger.warning(f"[EXEC-SETUP] add_init_script FAILED: {e}")

        # multi-lobby モード: Playwright route_web_socket で game WS を透過プロキシ化し
        # Python 側から直接 BET メッセージを送信できるようにする。
        if self._multi_lobby_mode:
            self._install_ws_proxy_route(context)

        # セッション切り替えモーダル（「他の場所でセッションが開始されました」）を
        # ページロード直後から自動 dismiss する MutationObserver を全ページに仕込む。
        # 旧 executor と同じ _SESSION_ELSEWHERE_SUPPRESSOR_JS を流用。
        try:
            from bacopy_executor_pragmatic_ws_live import _SESSION_ELSEWHERE_SUPPRESSOR_JS as _SE_JS
            context.add_init_script(_SE_JS)
            logger.info("[EXEC-SETUP] session-elsewhere suppressor pre-installed via add_init_script OK")
        except Exception as e:
            logger.warning(f"[EXEC-SETUP] session-elsewhere suppressor add_init_script FAILED: {e}")

        try:
            pages = context.pages or []
            logger.info(f"[EXEC-SETUP] attaching to {len(pages)} existing pages")
            for p in pages:
                try:
                    logger.info(f"[EXEC-SETUP]   page: {p.url[:80]}")
                except Exception:
                    pass
                self._attach_page(p)
        except Exception as ex:
            logger.warning(f"[EXEC-SETUP] context.pages failed ({ex}), attaching lobby_page only")
            self._attach_page(lobby_page)

        def _on_new_page(page: Any) -> None:
            try:
                logger.info(f"[EXEC-SETUP] new page detected: {page.url[:80]}")
            except Exception:
                logger.info("[EXEC-SETUP] new page detected (URL unknown)")
            self._attach_page(page)
        context.on("page", _on_new_page)

        logger.info(f"[EXEC-SETUP] setup complete — hooks registered. multi_lobby={self._multi_lobby_mode} attached_pages={len(self._attached_page_ids)}")
        if self._multi_lobby_mode:
            # add_init_script は登録済みだが、既に開いている WS には適用されない。
            # ロビーを再ロードすることで、Pragmatic iframe が bridge インストール後に
            # 再描画され、マルチテーブル WS が __bacopy_sockets に捕捉される。
            logger.info("[EXEC-SETUP] multi-lobby: reloading lobby so bridge pre-installs before multi-table WS opens")
            try:
                lobby_page.reload(wait_until="domcontentloaded", timeout=30000)
                lobby_page.wait_for_timeout(5000)
                logger.info("[EXEC-SETUP] lobby reload complete")
            except Exception as e:
                logger.warning(f"[EXEC-SETUP] lobby reload failed: {e}")
            logger.info("[EXEC-SETUP] multi-lobby mode: ensuring multi-play tab is active")
            try:
                self._ensure_multi_area(force=True)
            except Exception as ex:
                logger.warning(f"[EXEC-SETUP] _ensure_multi_area failed: {ex}")
        self._notify(
            "📡 ロビー監視中\n"
            "シグナル発生時に自動でテーブル入場・BETします\n"
            "手動操作は不要です"
        )

    def _attach_page(self, page: Any) -> None:
        """既存/新規ページへ bridge + websocket hook を1回だけ設定。"""
        try:
            pid = id(page)
            if pid in self._attached_page_ids:
                logger.info(f"[EXEC-ATTACH] skip (already attached) page={getattr(page,'url','?')[:60]}")
                return
            self._attached_page_ids.add(pid)
        except Exception:
            pass
        self._inject_all(page)
        try:
            page.on("websocket", self._on_ws_event)
            logger.info(f"[EXEC-ATTACH] websocket hook registered page={getattr(page,'url','?')[:60]}")
        except Exception as e:
            logger.warning(f"[EXEC-ATTACH] page.on(websocket) FAILED: {e}")

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

    def _ensure_table_state(self, table_id: str) -> dict[str, Any]:
        tid = str(table_id or "").strip()
        if not tid:
            return {}
        st = self._table_states.get(tid)
        if not st:
            st = {
                "table_id": tid,
                "table_name": "",
                "ws_url": "",
                "user_id": "",
                "game_id": "",
                "bets_open_game_id": "",
                "bets_closed_game_id": "",
                "last_bets_open_at": 0.0,
                "last_seen": 0.0,
            }
            self._table_states[tid] = st
        st["last_seen"] = time.time()
        return st

    def _sync_active_from_table(self, table_id: str) -> None:
        tid = str(table_id or "").strip()
        if not tid:
            return
        st = self._table_states.get(tid) or {}
        if not st:
            return
        self._table_id = tid
        self._table_name = str(st.get("table_name") or self._table_name or "")
        self._game_ws_url = str(st.get("ws_url") or self._game_ws_url or "")
        self._user_id = str(st.get("user_id") or self._user_id or "")
        self._game_id = str(st.get("game_id") or self._game_id or "")
        self._bets_open_game_id = str(st.get("bets_open_game_id") or "")
        self._bets_closed_game_id = str(st.get("bets_closed_game_id") or "")
        self._last_bets_open_at = float(st.get("last_bets_open_at") or 0.0)

    def _is_table_bet_window_open(self, table_id: str) -> bool:
        tid = str(table_id or "").strip()
        if not tid:
            return False
        st = self._table_states.get(tid) or {}
        open_gid = str(st.get("bets_open_game_id") or "")
        closed_gid = str(st.get("bets_closed_game_id") or "")
        if not open_gid or open_gid == closed_gid:
            return False
        age = time.time() - float(st.get("last_bets_open_at") or 0.0)
        max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "17") or 17)
        return 0 < age < max_age

    def _focus_table_in_multi(self, req: dict) -> bool:
        page = self._bet_page or self._lobby_page
        if page is None:
            logger.warning("[FOCUS] FAIL: no page (bet_page and lobby_page both None)")
            return False
        try:
            page_url = page.url[:80]
        except Exception:
            page_url = "(unknown)"
        self._ensure_multi_area(force=True)
        table_id = str(req.get("table_id") or "").strip()
        qpid = str(req.get("qpid") or "").strip()
        table_name = str(req.get("table_name") or "").strip()
        intent = str(req.get("intent") or "preposition").strip().lower()
        click = intent in ("prepare", "decision", "bet")
        candidates = [x for x in [table_name, table_id, qpid] if x]
        # 英日テーブル名の差分を吸収（例: "Baccarat 5" <-> "バカラ 5"）
        try:
            m_en = re.search(r"(?i)baccarat\s*(\d+)", table_name)
            if m_en:
                candidates.append(f"バカラ {m_en.group(1)}")
            m_ja = re.search(r"バカラ\s*(\d+)", table_name)
            if m_ja:
                candidates.append(f"Baccarat {m_ja.group(1)}")
        except Exception:
            pass
        logger.info(f"[FOCUS] intent={intent} click={click} table={table_name!r} qpid={qpid!r} page={page_url}")

        frames = [page]
        try:
            from bacopy_executor_pragmatic_ws_live import find_lobby_frames  # type: ignore
            lfs = find_lobby_frames(page) or []
            if lfs:
                frames = lfs
                logger.info(f"[FOCUS] using {len(frames)} lobby frames")
            else:
                logger.info(f"[FOCUS] find_lobby_frames returned empty, using page directly")
        except Exception as ex:
            logger.info(f"[FOCUS] find_lobby_frames not available ({ex}), using page directly")

        args = {
            "qpid": qpid or table_id,
            "click": bool(click),
            "maxScroll": int(os.getenv("BACOPY_MULTI_SCROLL_MAX", "48") or "48"),
            "candidates": candidates,
            "hintIndex": -1,
            "hintTotal": 0,
        }
        cache_key = str(qpid or table_id or "").strip()
        cached = self._table_focus_cache.get(cache_key) if cache_key else None
        if isinstance(cached, dict):
            if cached.get("table_name") and cached.get("table_name") not in args["candidates"]:
                args["candidates"].append(str(cached.get("table_name")))
            try:
                args["hintIndex"] = int(cached.get("match_index", -1))
                args["hintTotal"] = int(cached.get("total_nodes", 0))
            except Exception:
                args["hintIndex"] = -1
                args["hintTotal"] = 0

        frame_order = list(range(len(frames)))
        if isinstance(cached, dict):
            try:
                cfi = int(cached.get("frame_index", -1))
            except Exception:
                cfi = -1
            if 0 <= cfi < len(frame_order):
                frame_order = [cfi] + [i for i in frame_order if i != cfi]

        for i in frame_order:
            fr = frames[i]
            try:
                res = fr.evaluate(_MULTI_LOBBY_FOCUS_JS, args)
                logger.info(f"[FOCUS] frame[{i}] JS result: {res}")
            except Exception as ex:
                logger.warning(f"[FOCUS] frame[{i}] JS evaluate error: {ex}")
                continue
            if isinstance(res, dict) and res.get("found"):
                tid = str(qpid or table_id or "")
                if tid:
                    st = self._ensure_table_state(tid)
                    if table_name and not st.get("table_name"):
                        st["table_name"] = table_name
                if cache_key:
                    try:
                        self._table_focus_cache[cache_key] = {
                            "table_name": str(table_name or cached.get("table_name") if isinstance(cached, dict) else table_name or ""),
                            "frame_index": int(i),
                            "match_index": int(res.get("matchIndex") or -1),
                            "total_nodes": int(res.get("totalNodes") or 0),
                            "updated_at": time.time(),
                        }
                        self._save_table_focus_cache()
                    except Exception:
                        pass
                tag = "[ML-PREPARE]" if click else "[ML-PREPOS]"
                logger.info(
                    f"{tag} FOUND table={table_name or table_id or qpid} qpid={qpid or table_id or '-'} "
                    f"clicked={bool(res.get('clicked'))}"
                )
                return True
        logger.warning(
            f"[FOCUS] NOT FOUND table={table_name or table_id or qpid or '-'} intent={intent} candidates={candidates}"
        )
        return False

    def _ensure_multi_area(self, force: bool = False) -> None:
        if not self._multi_lobby_mode:
            return
        now = time.time()
        if (not force) and (now - self._last_multi_area_ensure_at < 15.0):
            return
        self._last_multi_area_ensure_at = now

        # マルチテーブル WS が既に確立 → ゲーム内にいる証拠、再クリック不要
        # ただし最終 betsopen から 120s 超えたら WS 死亡と見なしてスキップしない
        if self._is_multi_table_ws and self._multi_area_ready and not force:
            multi_st = None
            for st_val in self._table_states.values():
                if st_val.get("is_multi_table"):
                    multi_st = st_val
                    break
            last_open = float((multi_st or {}).get("last_bets_open_at") or 0.0)
            ws_alive = last_open and (now - last_open < 120.0)
            if ws_alive:
                logger.info("[MULTI-AREA] multi-WS alive (recent betsopen), already in multi-play game — skip re-click")
                return
            logger.info("[MULTI-AREA] multi-WS flag set but no recent betsopen → allow re-click")

            # BETSOPEN が長時間止まった場合はページリロードで WS を再接続する
            reload_threshold = float(os.getenv("BACOPY_MULTI_WS_RELOAD_SEC", "180") or 180)
            reload_cooldown = reload_threshold + 30.0
            if (last_open and (now - last_open > reload_threshold)
                    and (now - self._last_ws_reload_at > reload_cooldown)):
                logger.warning(
                    f"[MULTI-AREA] BETSOPEN silent {now - last_open:.0f}s > {reload_threshold:.0f}s"
                    " — page reload to reconnect WS"
                )
                self._last_ws_reload_at = now
                try:
                    reload_page = self._bet_page or self._lobby_page
                    if reload_page:
                        reload_page.reload(wait_until="domcontentloaded", timeout=30000)
                        reload_page.wait_for_timeout(3000)
                        self._is_multi_table_ws = False
                        self._multi_area_ready = False
                        for st in self._table_states.values():
                            if st.get("is_multi_table"):
                                st["last_bets_open_at"] = 0.0
                                st["bets_open_game_id"] = ""
                                st["bets_closed_game_id"] = ""
                                st["ws_url"] = ""
                        self._game_ws_url = ""
                        logger.info("[MULTI-AREA] page reload complete — WS state reset")
                except Exception as _re:
                    logger.warning(f"[MULTI-AREA] page reload failed: {_re}")
                return

        page = self._bet_page or self._lobby_page
        if page is None:
            logger.warning("[MULTI-AREA] no page available")
            return
        try:
            logger.info(f"[MULTI-AREA] checking page={page.url[:80]} force={force}")
        except Exception:
            logger.info(f"[MULTI-AREA] checking page=(url unknown) force={force}")

        frames = [page]
        try:
            from bacopy_executor_pragmatic_ws_live import find_lobby_frames  # type: ignore
            lfs = find_lobby_frames(page) or []
            if lfs:
                frames = lfs
        except Exception:
            pass

        found_tab = False
        for fr in frames:
            try:
                res = fr.evaluate(_MULTI_LOBBY_ENSURE_TAB_JS)
                logger.info(f"[MULTI-AREA] tab JS result: {res}")
            except Exception as ex:
                logger.warning(f"[MULTI-AREA] tab JS error: {ex}")
                continue
            if not isinstance(res, dict):
                continue
            if res.get("clicked") or res.get("active"):
                logger.info(
                    f"[MULTI-AREA] multi-play tab {'CLICKED' if res.get('clicked') else 'ALREADY ACTIVE'}: {res.get('text') or '-'}"
                )
                self._multi_area_ready = True
                found_tab = True
                return
        if not found_tab:
            logger.warning(f"[MULTI-AREA] multi-play tab NOT FOUND in page — may be on wrong page or UI changed")

    # ── game WS 検出 ─────────────────────────────────────────────────

    def _on_ws_event(self, ws: Any) -> None:
        url = str(ws.url or "")
        # 全WSイベントをまず記録（フィルタ前）
        logger.info(f"[WS-EVENT] new WS: {url[:120]}")
        # Pragmatic の game WS か確認
        if "pragmaticplaylive.net" not in url and "qpidreoxcc.net" not in url:
            logger.info(f"[WS-FILTER] SKIP non-pragmatic: {url[:80]}")
            return
        # dga lobby WS (/ws パス) は除外。game WS (dga domain + /game パス) は通す
        if "/dga" in url:
            logger.info(f"[WS-FILTER] SKIP /dga path: {url[:80]}")
            return
        if "dga." in url and "/game" not in url:
            logger.info(f"[WS-FILTER] SKIP dga non-game: {url[:80]}")
            return
        is_multi_ws = 'multiTable=true' in url
        logger.info(f"[WS-FILTER] PASS: tableId={re.search(r'tableId=([^&]+)',url) and re.search(r'tableId=([^&]+)',url).group(1)} multi={is_multi_ws}")

        ws_table_id = ""
        m = re.search(r'[?&]tableId=([^&]+)', url)
        if m:
            ws_table_id = m.group(1)

        if self._multi_lobby_mode:
            if ws_table_id:
                st = self._ensure_table_state(ws_table_id)
                st["ws_url"] = url
                if is_multi_ws:
                    st["is_multi_table"] = True
            if is_multi_ws and not self._is_multi_table_ws:
                self._is_multi_table_ws = True
                logger.info(f"[MULTI-WS] multi-table WS confirmed: {ws_table_id} — _is_multi_table_ws=True")
            expected = str(self._switch_target_table_id or "")
            if expected and ws_table_id and ws_table_id == expected:
                self._sync_active_from_table(ws_table_id)
                if self._phase == "waiting":
                    self._phase = "ready"
                    tname = self._table_name or ws_table_id or "?"
                    self._notify(
                        f"🔗 game WS 確立\n"
                        f"table: {tname}\n"
                        f"BET待機中 (betsopen を待っています)"
                    )
                    self._switch_target_table_id = ""
        else:
            if self._phase == "waiting":
                expected = str(self._switch_target_table_id or "")
                if not expected and isinstance(self._pending_bet, dict):
                    expected = str(self._pending_bet.get("table_id") or "")
                if expected:
                    if not ws_table_id or ws_table_id != expected:
                        logger.info(
                            f"[LIVE] ignore unexpected game WS while waiting: {ws_table_id or '-'} (expected={expected})"
                        )
                        return
                else:
                    if ws_table_id:
                        logger.info(f"[LIVE] ignore unsolicited game WS while waiting: {ws_table_id}")
                    return

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

        logger.info(f"[WS-HOOK] registering framereceived/framesent for tableId={ws_table_id or '(unknown)'}")
        ws.on("framereceived", lambda f, tid=ws_table_id: self._on_ws_message(_to_data(f), tid))
        ws.on("framesent",     lambda f, tid=ws_table_id: self._on_ws_sent(_to_data(f), tid))

        # Worker/socket 診断ログ（WS検出直後に全フレームを調査）
        try:
            pages = [self._lobby_page]
            try:
                for _p in (self._context.pages or []):
                    if _p not in pages:
                        pages.append(_p)
            except Exception:
                pass
            for _page in pages:
                all_frames = [_page]
                try:
                    all_frames += list(_page.frames)
                except Exception:
                    pass
                for _fr in all_frames:
                    try:
                        _d = _fr.evaluate(
                            "({sockets:(window.__bacopy_sockets||[]).length,"
                            "workers:(window.__bacopy_worker_urls||[]),"
                            "worker_bridge:!!window.__bacopy_worker_bridge_installed,"
                            "ws_bridge:!!window.__bacopy_ws_bridge_installed})"
                        )
                        logger.info(f"[WS-DIAG] frame={_fr.url[:60]} -> {_d}")
                    except Exception as _e:
                        logger.info(f"[WS-DIAG] frame={getattr(_fr,'url','?')[:60]} ERROR={_e}")

                # --- DOM 詳細調査（multi-WS確立 + 最初のBETSOPEN後、1回だけ実行）---
                # _dom_dump_ready は BETSOPEN ハンドラ内でセットされる
                if self._is_multi_table_ws and not getattr(self, '_dom_dumped', False) and getattr(self, '_dom_dump_ready', False):
                    self._dom_dumped = True
                    _DOM_JS2 = """() => {
  const r = {};
  // Canvas要素を調査
  const canvases = [];
  for(const c of document.querySelectorAll('canvas')) {
    const rect=c.getBoundingClientRect();
    canvases.push({id:c.id,cls:(c.className||'').slice(0,60),x:Math.round(rect.x),y:Math.round(rect.y),w:Math.round(rect.width),h:Math.round(rect.height)});
  }
  r.canvases = canvases;
  // 全要素（深さ制限なし）でサイズのある要素
  const els = [];
  for(const el of document.querySelectorAll('*')) {
    const rect=el.getBoundingClientRect();
    if(rect.width<10||rect.height<10) continue;
    const txt=(el.textContent||'').trim().replace(/\\s+/g,' ').slice(0,50);
    const cls=(el.className||'').slice(0,60);
    const id=el.id||'';
    const tag=el.tagName;
    if(tag==='SCRIPT'||tag==='STYLE'||tag==='HEAD') continue;
    els.push({tag,txt,cls,id,x:Math.round(rect.x),y:Math.round(rect.y),w:Math.round(rect.width),h:Math.round(rect.height)});
  }
  r.els = els.slice(0,80);
  // body直下の構造
  r.bodyHTML = (document.body||{}).innerHTML ? document.body.innerHTML.slice(0,2000) : '';
  // document.title
  r.title = document.title;
  // iframeリスト
  r.iframes = [...document.querySelectorAll('iframe')].map(f=>({src:(f.src||f.getAttribute('src')||'').slice(0,120),cls:(f.className||'').slice(0,40)}));
  return r;
}"""
                    for _pfr in all_frames:
                        _furl = getattr(_pfr, 'url', '?')
                        if 'pragmatic' not in _furl and 'zmcdpj' not in _furl:
                            continue
                        try:
                            _dom = _pfr.evaluate(_DOM_JS2)
                            logger.info(f"[DOM2] frame={_furl[:80]} title={_dom.get('title')}")
                            logger.info(f"[DOM2] canvases={_dom.get('canvases')}")
                            for _e in (_dom.get('els') or []):
                                logger.info(f"[DOM2-EL] {_e}")
                            logger.info(f"[DOM2-BODY] {_dom.get('bodyHTML','')[:500]}")
                            for _if in (_dom.get('iframes') or []):
                                logger.info(f"[DOM2-IFRAME] {_if}")
                        except Exception as _de:
                            logger.info(f"[DOM2] frame={_furl[:80]} ERROR={_de}")
        except Exception:
            pass

        # phase = ready
        if self._phase == "waiting" and not self._multi_lobby_mode:
            self._phase = "ready"
            tname = self._table_name or self._table_id or "?"
            if self._switch_target_table_id and ws_table_id == self._switch_target_table_id:
                self._switch_target_table_id = ""
            self._notify(
                f"🔗 game WS 確立\n"
                f"table: {tname}\n"
                f"BET待機中 (betsopen を待っています)"
            )

    def _on_ws_message(self, data: str, ws_table_id: str = "") -> None:
        """ゲームWSからの受信メッセージを解析。"""
        if not data:
            return
        # userId を生データ全体から緩く抽出（ネスト構造差分への保険）
        if not self._user_id:
            m_uid_any = re.search(r'"(?:userId|uId)"\s*:\s*"([^"]{6,})"', data)
            if m_uid_any:
                uid_any = str(m_uid_any.group(1) or "").strip()
                if uid_any:
                    self._user_id = uid_any
                    if ws_table_id:
                        try:
                            self._ensure_table_state(ws_table_id)["user_id"] = uid_any
                        except Exception:
                            pass
                    self._persist_user_id(uid_any)
                    logger.info(f"[USER-ID] captured from raw ws message: ...{uid_any[-8:]}")
        if (not self._multi_lobby_mode) and self._table_id and ws_table_id and ws_table_id != self._table_id:
            return
        effective_tid = str(ws_table_id or self._table_id or "").strip()
        st = self._ensure_table_state(effective_tid) if effective_tid else {}

        # JSON
        if data.startswith("{") or data.startswith("["):
            try:
                obj = json.loads(data)
            except Exception:
                return
            if not isinstance(obj, dict):
                return

            msg_tid = str(obj.get("tableId") or obj.get("tableid") or effective_tid or "").strip()
            if msg_tid:
                st = self._ensure_table_state(msg_tid)
                if self._game_ws_url and not st.get("ws_url"):
                    st["ws_url"] = self._game_ws_url

            # betsopen
            bo = obj.get("betsopen")
            if isinstance(bo, dict):
                gid = str(bo.get("game") or "")
                # マルチプレイ WS では betsopen 内に個別テーブル ID が入る場合がある
                bo_table_id = str(
                    bo.get("table") or bo.get("tableId") or bo.get("tableid") or
                    bo.get("sourceTable") or bo.get("source_table") or ""
                ).strip()
                now = time.time()
                if gid:
                    if st:
                        st["bets_open_game_id"] = gid
                        st["last_bets_open_at"] = now
                    if bo_table_id:
                        # betsopen に個別テーブル ID があれば per-table state も更新
                        per_st = self._ensure_table_state(bo_table_id)
                        per_st["bets_open_game_id"] = gid
                        per_st["last_bets_open_at"] = now
                        per_st.setdefault("multi_channel", msg_tid)
                    if (not self._multi_lobby_mode) and gid != self._bets_open_game_id:
                        self._bets_open_game_id = gid
                        self._last_bets_open_at = now
                    logger.info(
                        f"[BETSOPEN] table={msg_tid} game={gid} bo_table={bo_table_id!r} "
                        f"bo_keys={list(bo.keys())} pending_bet={bool(self._pending_bet)}"
                    )
                    pending_target = ""
                    if isinstance(self._pending_bet, dict):
                        pending_target = str(self._pending_bet.get("table_id") or "")
                    if self._multi_lobby_mode:
                        is_multi_ws = self._is_multi_table_ws or bool(
                            msg_tid and (self._table_states.get(msg_tid) or {}).get("is_multi_table")
                        )
                        # betsopen に個別テーブル ID があればそちらで照合、なければ msg_tid で照合
                        effective_table = bo_table_id or msg_tid
                        logger.info(
                            f"[BETSOPEN-MATCH] pending={pending_target!r} effective={effective_table!r} "
                            f"is_multi_ws={is_multi_ws} bo_table={bo_table_id!r}"
                        )
                        if pending_target:
                            if bo_table_id and pending_target == bo_table_id:
                                # betsopen に個別 ID あり → 完全一致
                                # WS送信は必ず multi-channel (msg_tid) を使う。bo_table_id は照合のみ
                                ws_channel = msg_tid if is_multi_ws else effective_table
                                logger.info(f"[BETSOPEN-HIT] per-table match: {bo_table_id} game={gid} ws_channel={ws_channel}")
                                self._sync_active_from_table(ws_channel)
                                self._try_execute_bet(gid, table_id=ws_channel)
                            elif bo_table_id and pending_target != bo_table_id:
                                logger.info(f"[BETSOPEN-SKIP] per-table mismatch: pending={pending_target} bo_table={bo_table_id}")
                            elif is_multi_ws:
                                # マルチテーブル WS: betsopen に個別 ID なし → 任意の betsopen で BET
                                logger.info(
                                    f"[BETSOPEN-MULTI] multi-WS, no per-table in betsopen → "
                                    f"executing bet for pending={pending_target} via channel={msg_tid} game={gid}"
                                )
                                self._sync_active_from_table(msg_tid)
                                self._try_execute_bet(gid, table_id=msg_tid)
                            elif pending_target == msg_tid:
                                self._sync_active_from_table(msg_tid)
                                self._try_execute_bet(gid, table_id=msg_tid)
                            else:
                                logger.info(f"[BETSOPEN-SKIP] table mismatch: pending={pending_target} arrived={msg_tid}")
                        else:
                            logger.info(f"[BETSOPEN-SKIP] no pending bet for table={effective_table}")
                        # ゲームロード完了 → 30秒後にDOMダンプ（1回だけ）
                        if not getattr(self, '_dom_dump_scheduled', False):
                            self._dom_dump_scheduled = True
                            self._dom_dump_at = time.time() + 30.0
                        # BETSOPENのタイミングでb_cの中身を確認（最初の5回）
                        _bc_check_count = getattr(self, '_bc_check_count', 0)
                        if _bc_check_count < 5:
                            self._bc_check_count = _bc_check_count + 1
                            try:
                                for _p2 in ([self._lobby_page] + list(self._context.pages or [])):
                                    for _f2 in ([_p2] + list(_p2.frames or [])):
                                        if 'pragmaticplaylive' in getattr(_f2, 'url', ''):
                                            _bc = _f2.evaluate(r"()=>{const b=document.querySelector('.b_c,.b_e');return b?{len:b.innerHTML.length,html:b.innerHTML.slice(0,200),tag:b.tagName,cls:b.className}:{notfound:1}}")
                                            logger.info(f"[BC-CHECK] count={_bc_check_count} {_bc}")
                            except Exception as _bce:
                                logger.info(f"[BC-CHECK] error: {_bce}")
                    else:
                        self._try_execute_bet(gid)
                else:
                    logger.info(f"[BETSOPEN] no game_id in betsopen msg table={msg_tid} bo={bo}")
                return

            # betsclosed
            bc_obj = obj.get("betsclosed")
            if isinstance(bc_obj, dict):
                gid = str(bc_obj.get("game") or "")
                if gid:
                    if st:
                        st["bets_closed_game_id"] = gid
                    if (not self._multi_lobby_mode) or (msg_tid and msg_tid == self._table_id):
                        self._bets_closed_game_id = gid
                    logger.info(f"[BETSCLOSED] table={msg_tid} game={gid}")
                return

            # game id
            g = obj.get("game")
            if isinstance(g, dict):
                gid = str(g.get("id") or "")
                if gid:
                    if st:
                        st["game_id"] = gid
                    if (not self._multi_lobby_mode) or (msg_tid and msg_tid == self._table_id):
                        self._game_id = gid
                    logger.info(f"[GAME-ID] table={msg_tid} game_id={gid}")
                return

            # user_id from ALERT_JOINED
            u = obj.get("user") if isinstance(obj.get("user"), dict) else {}
            uid = str(u.get("userId") or "")
            if uid:
                if st:
                    st["user_id"] = uid
                if (not self._user_id) or (msg_tid and msg_tid == self._table_id):
                    self._user_id = uid
                    self._persist_user_id(uid)
                logger.info(f"[USER-ID] table={msg_tid} uid=...{uid[-8:]} global_uid=...{self._user_id[-8:] if self._user_id else 'NONE'}")
                pending_target = ""
                if isinstance(self._pending_bet, dict):
                    pending_target = str(self._pending_bet.get("table_id") or "")
                if self._multi_lobby_mode:
                    win_open = self._is_table_bet_window_open(msg_tid)
                    logger.info(f"[USER-ID] pending_target={pending_target!r} bet_window_open={win_open}")
                    if pending_target and msg_tid and pending_target == msg_tid and win_open:
                        gid = str((st or {}).get("bets_open_game_id") or "")
                        if gid:
                            self._sync_active_from_table(msg_tid)
                            self._try_execute_bet(gid, table_id=msg_tid)
                        else:
                            logger.info(f"[USER-ID] no bets_open_game_id for table={msg_tid}")
                else:
                    if self._pending_bet and self._is_bet_window_open() and self._bets_open_game_id:
                        self._try_execute_bet(self._bets_open_game_id)
            self._last_game_ws_recv_at = time.time()
            return

        # XML
        if data.startswith("<"):
            m = re.search(r'userId="([^"]+)"', data)
            if m:
                uid = m.group(1)
                if st:
                    st["user_id"] = uid
                if (not self._user_id) or (effective_tid and effective_tid == self._table_id):
                    self._user_id = uid
                    logger.info(f"[LIVE] user_id from XML: ...{self._user_id[-8:]}")

    def _on_ws_sent(self, data: str, ws_table_id: str = "") -> None:
        """送信メッセージから tableId / userId を補完。"""
        _raw = repr(data[:120]) if data else "(empty)"
        logger.info(f"[WS-SENT-RAW] tid={ws_table_id} len={len(data) if data else 0} raw={_raw}")
        if not data:
            return
        if not self._user_id:
            m_uid_any = re.search(r'"(?:userId|uId)"\s*:\s*"([^"]{6,})"', data)
            if m_uid_any:
                uid_any = str(m_uid_any.group(1) or "").strip()
                if uid_any:
                    self._user_id = uid_any
                    if ws_table_id:
                        try:
                            self._ensure_table_state(ws_table_id)["user_id"] = uid_any
                        except Exception:
                            pass
                    self._persist_user_id(uid_any)
                    logger.info(f"[USER-ID] captured from raw ws sent: ...{uid_any[-8:]}")
        if (not self._multi_lobby_mode) and self._table_id and ws_table_id and ws_table_id != self._table_id:
            return
        # JSON SUBSCRIBE
        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                tid = str(obj.get("tableId") or ws_table_id or "").strip()
                st = self._ensure_table_state(tid) if tid else {}
                uid = str(obj.get("userId") or "")
                logger.info(f"[WS-SENT] JSON tableId={tid!r} userId={'...' + uid[-8:] if uid else 'NONE'} keys={list(obj.keys())[:6]}")
                if uid:
                    if st:
                        st["user_id"] = uid
                    if (not self._user_id) or (tid and tid == self._table_id):
                        self._user_id = uid
                        self._persist_user_id(uid)
                    logger.info(f"[SUBSCRIBE] userId captured table={tid} uid=...{uid[-8:]} global=...{self._user_id[-8:] if self._user_id else 'NONE'}")
                    if self._pending_bet and self._is_bet_window_open() and self._bets_open_game_id and (not self._multi_lobby_mode):
                        self._try_execute_bet(self._bets_open_game_id)
                if tid and not self._table_id:
                    if self._phase == "waiting" and not self._multi_lobby_mode:
                        expected = str(self._switch_target_table_id or "")
                        if not expected and isinstance(self._pending_bet, dict):
                            expected = str(self._pending_bet.get("table_id") or "")
                        if expected and tid != expected:
                            logger.info(
                                f"[LIVE] ignore SUBSCRIBE tableId while waiting: {tid} (expected={expected})"
                            )
                            return
                        if not expected:
                            logger.info(f"[LIVE] ignore unsolicited SUBSCRIBE tableId while waiting: {tid}")
                            return
                    self._table_id = tid
                    logger.info(f"[LIVE] tableId from SUBSCRIBE: {tid}")
                    if self._phase == "waiting":
                        self._phase = "ready"
                        if self._switch_target_table_id and tid == self._switch_target_table_id:
                            self._switch_target_table_id = ""
                if st and self._game_ws_url and not st.get("ws_url"):
                    st["ws_url"] = self._game_ws_url
        except Exception:
            pass
        # XML SUBSCRIBE
        m = re.search(r'userId="([^"]+)"', data)
        if m:
            uid = m.group(1)
            if ws_table_id:
                st = self._ensure_table_state(ws_table_id)
                st["user_id"] = uid
            if (not self._user_id) or (ws_table_id and ws_table_id == self._table_id):
                self._user_id = uid
                self._persist_user_id(uid)
        m = re.search(r'tableId="([^"]+)"', data)
        if m and not self._table_id:
            tid = m.group(1)
            if self._phase == "waiting" and not self._multi_lobby_mode:
                expected = str(self._switch_target_table_id or "")
                if not expected and isinstance(self._pending_bet, dict):
                    expected = str(self._pending_bet.get("table_id") or "")
                if expected and tid != expected:
                    return
                if not expected:
                    return
            self._table_id = tid
            if self._switch_target_table_id and tid == self._switch_target_table_id:
                self._switch_target_table_id = ""
        if m:
            tid2 = m.group(1)
            if tid2:
                st2 = self._ensure_table_state(tid2)
                if self._game_ws_url and not st2.get("ws_url"):
                    st2["ws_url"] = self._game_ws_url

    # ── tick (bot.run() から定期呼出) ────────────────────────────────

    def tick(self) -> None:
        now = time.time()
        page = self._lobby_page

        # ── DOMダンプ（スケジュール済みの場合のみ）─────────────────────
        if getattr(self, '_dom_dump_scheduled', False) and not getattr(self, '_dom_dumped', False):
            if now >= getattr(self, '_dom_dump_at', float('inf')):
                self._dom_dumped = True
                try:
                    self._run_dom_dump()
                except Exception:
                    pass

        # ── 診断用 state dump (30秒ごと) ─────────────────────────────
        if now - self._last_state_dump_at >= 30.0:
            self._last_state_dump_at = now
            try:
                page_url = page.url[:100] if page else "(no page)"
            except Exception:
                page_url = "(url error)"
            pending_info = ""
            if isinstance(self._pending_bet, dict) and self._pending_bet:
                pb = self._pending_bet
                pending_info = (
                    f"side={pb.get('side')} table={pb.get('table_id')} "
                    f"age={now - float(pb.get('queued_at') or now):.1f}s "
                    f"prepared={pb.get('_prepare_attempted')} joined={pb.get('_fallback_join_attempted')}"
                )
            else:
                pending_info = "None"
            ws_summary = {
                tid: {
                    "last_bets_open": f"{now - float(st.get('last_bets_open_at') or 0):.0f}s ago" if st.get("last_bets_open_at") else "never",
                    "has_ws": bool(st.get("ws_url")),
                }
                for tid, st in self._table_states.items()
            }
            logger.info(
                f"[DIAG] page={page_url} | phase={self._phase} | multi={self._multi_lobby_mode} | "
                f"multi_ready={self._multi_area_ready} | multi_ws={self._is_multi_table_ws} | pending={pending_info}"
            )
            logger.info(f"[DIAG] known tables({len(self._table_states)}): {ws_summary}")

        # switch 要求があれば先に処理（テーブル入場）
        if self._switch_request and not self._switch_in_progress:
            req = self._switch_request
            logger.info(f"[TICK] processing switch_request: intent={req.get('intent')} table={req.get('table_id')} pending_bet={bool(self._pending_bet)}")
            self._switch_request = None
            self._switch_in_progress = True
            try:
                self._perform_switch(req)
            except Exception as e:
                logger.warning(f"[TICK] switch failed: {e}")
            finally:
                self._switch_in_progress = False

        # pending bet が長時間残っている場合は解放（decision 詰まり防止）
        pending_age = 0.0
        pending_snapshot: dict[str, Any] = {}
        with self._lock:
            if isinstance(self._pending_bet, dict) and self._pending_bet:
                pending_age = now - float(self._pending_bet.get("queued_at") or now)
                pending_snapshot = dict(self._pending_bet)

        # multi-lobby: pending卓のWSが開かないケースでは、一定時間後に1回だけ click fallback を試す
        if self._multi_lobby_mode and pending_snapshot:
            try:
                target = str(pending_snapshot.get("table_id") or "").strip()
                attempted = bool(pending_snapshot.get("_prepare_attempted"))
                fallback_sec = float(os.getenv("BACOPY_MULTI_PREPARE_FALLBACK_SEC", "30") or 30)
                st = self._table_states.get(target) or {}
                target_last_open = float(st.get("last_bets_open_at") or 0.0)
                if self._is_multi_table_ws and not target_last_open:
                    for _st in self._table_states.values():
                        if _st.get("is_multi_table"):
                            target_last_open = float(_st.get("last_bets_open_at") or 0.0)
                            break
                no_target_open = (not target_last_open) or ((now - target_last_open) > fallback_sec)
                if target and (not attempted) and pending_age > fallback_sec and no_target_open:
                    logger.warning(
                        f"[TICK] pending table WS not opened yet -> fallback click "
                        f"(target={target} age={pending_age:.1f}s)"
                    )
                    with self._lock:
                        if isinstance(self._pending_bet, dict) and self._pending_bet:
                            self._pending_bet["_prepare_attempted"] = True
                    self._request_switch(
                        target,
                        str(st.get("table_name") or self._table_name or target),
                        str(st.get("qpid") or target),
                        intent="prepare",
                    )
            except Exception as ex:
                logger.warning(f"[TICK] fallback prepare check failed: {ex}")

        # multi-lobby: click fallback後もWS未確立なら、1回だけ通常joinを試す
        if self._multi_lobby_mode and pending_snapshot:
            try:
                target = str(pending_snapshot.get("table_id") or "").strip()
                prepared = bool(pending_snapshot.get("_prepare_attempted"))
                joined = bool(pending_snapshot.get("_fallback_join_attempted"))
                join_sec = float(os.getenv("BACOPY_MULTI_FALLBACK_JOIN_SEC", "90") or 90)
                st = self._table_states.get(target) or {}
                target_last_open = float(st.get("last_bets_open_at") or 0.0)
                if self._is_multi_table_ws and not target_last_open:
                    for _st in self._table_states.values():
                        if _st.get("is_multi_table"):
                            target_last_open = float(_st.get("last_bets_open_at") or 0.0)
                            break
                no_target_open = (not target_last_open) or ((now - target_last_open) > join_sec)
                if target and prepared and (not joined) and pending_age > join_sec and no_target_open:
                    logger.warning(
                        f"[TICK] pending table WS still missing -> fallback join "
                        f"(target={target} age={pending_age:.1f}s)"
                    )
                    with self._lock:
                        if isinstance(self._pending_bet, dict) and self._pending_bet:
                            self._pending_bet["_fallback_join_attempted"] = True
                    self._request_switch(
                        target,
                        str(st.get("table_name") or self._table_name or target),
                        str(st.get("qpid") or target),
                        intent="fallback_join",
                    )
            except Exception as ex:
                logger.warning(f"[TICK] fallback join check failed: {ex}")

        # multi-lobby: 目標卓のbetsopenが長時間来ない pending は早めに解放して詰まり防止
        if self._multi_lobby_mode and pending_snapshot:
            try:
                target = str(pending_snapshot.get("table_id") or "").strip()
                mismatch_clear_sec = float(
                    os.getenv("BACOPY_PENDING_BET_MISMATCH_CLEAR_SEC", "90") or 90
                )
                if bool(pending_snapshot.get("_fallback_join_attempted")):
                    mismatch_clear_sec = max(
                        mismatch_clear_sec,
                        float(os.getenv("BACOPY_PENDING_BET_MISMATCH_CLEAR_AFTER_JOIN_SEC", "60") or 60),
                    )
                st = self._table_states.get(target) or {}
                target_last_open = float(st.get("last_bets_open_at") or 0.0)
                # マルチテーブル WS モードでは betsopen は multi-channel 側にしか来ない
                if self._is_multi_table_ws and not target_last_open:
                    for _st in self._table_states.values():
                        if _st.get("is_multi_table"):
                            target_last_open = float(_st.get("last_bets_open_at") or 0.0)
                            break
                no_target_open = (not target_last_open) or ((now - target_last_open) > mismatch_clear_sec)
                if target and pending_age > mismatch_clear_sec and no_target_open:
                    logger.warning(
                        f"[TICK] drop mismatched pending bet (target={target} age={pending_age:.1f}s, no target betsopen)"
                    )
                    with self._lock:
                        self._pending_bet = None
                    self._switch_request = None
                    self._phase = "waiting"
                    self._notify("⚠️ BET予約を破棄\n対象卓のbet window未検出のためスキップ")
                    pending_age = 0.0
            except Exception as ex:
                logger.warning(f"[TICK] mismatch clear check failed: {ex}")

        max_pending_age = float(os.getenv("BACOPY_PENDING_BET_MAX_SEC", "120") or 120)
        if pending_age > max_pending_age:
            logger.warning(f"[LIVE] drop stuck pending bet (age={pending_age:.1f}s)")
            with self._lock:
                self._pending_bet = None
            self._switch_request = None
            self._phase = "waiting"
            self._notify("⚠️ BET予約を破棄\n送信待機が長すぎたためスキップ")

        if self._multi_lobby_mode:
            try:
                self._ensure_multi_area()
            except Exception:
                pass

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
        if self._multi_lobby_mode:
            max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "17") or 17)
            for tid, st in list(self._table_states.items()):
                last_open = float(st.get("last_bets_open_at") or 0.0)
                if last_open > 0 and (now - last_open > max_age + 2):
                    bo = str(st.get("bets_open_game_id") or "")
                    bc = str(st.get("bets_closed_game_id") or "")
                    if bo and bo != bc:
                        st["bets_closed_game_id"] = bo
                ttl = float(os.getenv("BACOPY_MULTI_TABLE_STATE_TTL_SEC", "900") or 900)
                last_seen = float(st.get("last_seen") or 0.0)
                if last_seen > 0 and now - last_seen > ttl:
                    self._table_states.pop(tid, None)
        else:
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

        # 「他の場所でセッションが開始されました」モーダル検出・解除 (10秒ごと)
        # WS 並列接続後にサーバが session-elsewhere を送ることがある。旧 executor と同じ
        # _dismiss_session_elsewhere_modal を使って「ここに残る」を自動クリックする。
        if now - self._last_session_elsewhere_check_at >= 10.0:
            self._last_session_elsewhere_check_at = now
            try:
                from bacopy_executor_pragmatic_ws_live import _dismiss_session_elsewhere_modal
                found = _dismiss_session_elsewhere_modal(page, None)
                if found:
                    logger.info("[LIVE] session-elsewhere modal detected and dismissed")
            except Exception as e:
                logger.debug(f"[LIVE] session_elsewhere check error: {e}")

        # 「再接続しています」スタック検出 → 30s 超えたらページリロード (10秒ごと)
        if now - self._last_reconnecting_check_at >= 10.0:
            self._last_reconnecting_check_at = now
            try:
                detected = page.evaluate(_RECONNECTING_DETECT_JS)
            except Exception:
                detected = False
            if detected:
                if self._reconnecting_first_at == 0.0:
                    self._reconnecting_first_at = now
                    logger.info("[LIVE] 再接続しています 検出 — 30s 待機開始")
                elif now - self._reconnecting_first_at >= 30.0:
                    stuck_sec = int(now - self._reconnecting_first_at)
                    logger.warning(
                        f"[LIVE] 再接続しています stuck {stuck_sec}s — page reload"
                    )
                    self._reconnecting_first_at = 0.0
                    self._last_game_ws_recv_at = 0.0
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=15000)
                        logger.info("[LIVE] page reload after reconnect-stuck OK")
                    except Exception as e:
                        logger.warning(f"[LIVE] page reload error: {e}")
            else:
                if self._reconnecting_first_at != 0.0:
                    logger.info("[LIVE] 再接続しています 解消 — タイマーリセット")
                self._reconnecting_first_at = 0.0

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
        intent = str(req.get("intent") or "preposition").strip().lower()
        logger.info(f"[SWITCH] _perform_switch: intent={intent} multi={self._multi_lobby_mode} req={dict(list(req.items())[:4])}")
        if self._multi_lobby_mode and intent != "fallback_join":
            table_id = str(req.get("table_id") or "").strip()
            table_name = str(req.get("table_name") or "").strip()
            qpid = str(req.get("qpid") or "").strip()
            if not table_id and not qpid:
                logger.warning("[SWITCH] SKIP: no table_id and no qpid in request")
                return
            self._switch_target_table_id = str(qpid or table_id)
            if table_name:
                self._table_name = table_name
            ok = self._focus_table_in_multi(req)
            logger.info(f"[SWITCH] _focus_table_in_multi result={ok}")
            if ok and self._switch_target_table_id:
                self._switch_target_table_id = ""
            return

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
        self._switch_target_table_id = str(qpid or table_id)

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
        # _join_table 完了後に全フレームへブリッジを追加注入（add_init_script の補完）
        try:
            self._inject_all(page)
        except Exception:
            pass

    # ── BET API ──────────────────────────────────────────────────────

    def send_bet(self, side: str, amount: float, table_id: str = "", bet_id: str = "") -> bool:
        """BET を予約する。次の betsopen で送信。"""
        target_table = str(table_id or self._table_id or "").strip()
        on_owner_thread = threading.get_ident() == self._owner_thread_id
        logger.info(f"[SEND-BET] called: side={side} amount={amount} table={target_table!r} bet_id={bet_id!r}")
        if not target_table:
            logger.warning("[SEND-BET] SKIP: empty target table (table_id and _table_id both empty)")
            return False

        # multi-lobby: テーブルが画面外なら scroll して WS を確立させる（クリック=遷移しない）
        if self._multi_lobby_mode:
            st = self._ensure_table_state(target_table)
            if self._table_name and not st.get("table_name"):
                st["table_name"] = self._table_name
            self._request_switch(target_table, self._table_name, target_table, intent="preposition")
        else:
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
        logger.info(f"[BET-QUEUED] side={side} ${amount:.2f} table={target_table} known_tables={list(self._table_states.keys())[:6]}")

        # すでに betsopen 中なら即送信
        if self._multi_lobby_mode:
            win_open = self._is_table_bet_window_open(target_table)
            logger.info(f"[BET-QUEUED] immediate check: bet_window_open={win_open} for table={target_table}")
            if win_open:
                st = self._table_states.get(target_table) or {}
                gid = str(st.get("bets_open_game_id") or "")
                logger.info(f"[BET-QUEUED] betsopen active! game_id={gid} → firing _try_execute_bet immediately")
                if gid:
                    if on_owner_thread:
                        self._sync_active_from_table(target_table)
                        self._try_execute_bet(gid, table_id=target_table)
                    else:
                        logger.info("[BET-QUEUED] defer immediate execute (non-owner thread)")
            else:
                logger.info(f"[BET-QUEUED] no active betsopen for {target_table} — waiting for next betsopen event")
        else:
            at_target = (not target_table) or (self._table_id == target_table)
            if self._phase != "waiting" and self._is_bet_window_open() and at_target:
                if on_owner_thread:
                    self._try_execute_bet(self._bets_open_game_id)
                else:
                    logger.info("[BET-QUEUED] defer immediate execute (non-owner thread)")

        return True

    def _is_bet_window_open(self) -> bool:
        if not self._bets_open_game_id:
            return False
        if self._bets_open_game_id == self._bets_closed_game_id:
            return False
        age = time.time() - self._last_bets_open_at
        max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "17") or 17)
        return 0 < age < max_age

    def _try_execute_bet(self, game_id: str, table_id: str = "") -> None:
        with self._lock:
            bet = self._pending_bet
            if not bet:
                logger.info("[TRY-BET] called but no pending_bet — skip")
                return
            self._pending_bet = None
        logger.info(f"[TRY-BET] executing: side={bet.get('side')} table={bet.get('table_id')} game_id_arg={game_id!r}")

        queued_at = float(bet.get("queued_at") or 0.0)
        if queued_at:
            age = time.time() - queued_at
            max_age = float(os.getenv("BACOPY_MAX_BET_SIGNAL_AGE_SEC", "300") or 300)
            logger.info(f"[TRY-BET] signal age={age:.1f}s max={max_age}s")
            if age > max_age:
                logger.warning(f"[TRY-BET] DROP stale bet (age={age:.1f}s > {max_age}s)")
                self._notify("⚠️ BET SKIP\nsignal too old")
                return

        side = bet["side"]
        amount = bet["amount"]
        requested_table = str(bet.get("table_id") or "").strip()
        chosen_table = str(table_id or requested_table or self._table_id or "").strip()
        if not chosen_table:
            logger.warning("[TRY-BET] DEFER: no table context — re-queue")
            with self._lock:
                self._pending_bet = bet
            return
        # multi-WS: chosen_table=aggregator channel, requested_table=actual table ID
        # XML channel must use actual table ID; WS send uses aggregator channel
        if requested_table and chosen_table != requested_table:
            bet_table_id = requested_table
            logger.info(f"[TRY-BET] multi-WS: ws_channel={chosen_table} bet_table={bet_table_id}")
        else:
            bet_table_id = chosen_table
        bet_id = str(bet.get("bet_id") or "").strip()
        st = self._table_states.get(bet_table_id) or self._table_states.get(chosen_table) or {}
        game_id = str(game_id or st.get("game_id") or st.get("bets_open_game_id") or self._game_id or "").strip()
        user_id = str(st.get("user_id") or self._user_id or os.getenv("BACOPY_USER_ID", "").strip())
        if not user_id:
            for _st in self._table_states.values():
                uid = str((_st or {}).get("user_id") or "").strip()
                if uid:
                    user_id = uid
                    break
        if not user_id:
            user_id = self._try_discover_user_id()
        if user_id and st and not st.get("user_id"):
            st["user_id"] = user_id
        if user_id and not self._user_id:
            self._user_id = user_id
            self._persist_user_id(user_id)

        logger.info(f"[TRY-BET] table={bet_table_id} game_id={game_id!r} user_id={'...'+user_id[-8:] if user_id else 'NONE'} st_keys={list(st.keys())}")
        if not user_id:
            logger.warning(f"[TRY-BET] DEFER: user_id UNKNOWN for table={bet_table_id} (st.user_id={st.get('user_id')!r} global={self._user_id!r} env={bool(os.getenv('BACOPY_USER_ID'))})")
            with self._lock:
                self._pending_bet = bet
            return
        if not game_id:
            logger.warning(f"[TRY-BET] DEFER: game_id UNKNOWN for table={bet_table_id} (st={st})")
            with self._lock:
                self._pending_bet = bet
            return

        if self._multi_lobby_mode:
            self._sync_active_from_table(chosen_table)

        bc = _side_to_bc(side)
        xml = _build_lpbet_xml(
            table_id=bet_table_id,
            game_id=game_id,
            user_id=user_id,
            bc=bc,
            amount=amount,
        )
        ck = _extract_ck(xml)
        side_name = "BANKER" if bc == "B" else "PLAYER"

        tname = str(st.get("table_name") or self._table_name or bet_table_id)
        logger.info(f"[TRY-BET] SENDING: {side_name} ${amount:.2f} table={tname} game={game_id} ck={ck}")
        self._phase = "betting"

        # multi-lobby モードはクリックBETを使用（Worker WS経由は不可能）
        if self._multi_lobby_mode and self._is_multi_table_ws:
            self._bet_send_in_progress = True
            try:
                click_ok = self._click_place_bet(side, amount)
            finally:
                self._bet_send_in_progress = False
            result = {"ok": click_ok, "method": "click"}
            ok = click_ok
            logger.info(f"[TRY-BET] click_place_bet result={result}")
        else:
            self._bet_send_in_progress = True
            try:
                result = self._ws_send(chosen_table, xml)
            finally:
                self._bet_send_in_progress = False
            ok = isinstance(result, dict) and result.get("ok")
            logger.info(f"[TRY-BET] _ws_send result={result}")

        if ok:
            logger.info(f"[LIVE] bet sent OK ck={ck}")
            self._consecutive_failures = 0
            if bet_id:
                self._sent_bet_ids.add(bet_id)
            self._notify(f"📤 BET送信\n{tname}\n{side_name} ${amount:.2f}\n(確定待ち)")
        else:
            logger.error(f"[LIVE] bet send FAILED: {result}")
            self._consecutive_failures += 1
            # ブリッジ再注入して次の betsopen でリトライ
            for _p in [self._bet_page, self._lobby_page]:
                if _p:
                    try:
                        self._inject_all(_p)
                    except Exception:
                        pass
            with self._lock:
                self._pending_bet = bet  # 再キュー
            self._notify(f"⚠️ BET送信失敗(リトライ待機)\n{tname}\n{side_name} ${amount:.2f}")
            return  # phase/table_id をリセットしない

        if not self._multi_lobby_mode:
            # 従来モード: BET完了後にロビーへ戻す
            try:
                page = self._bet_page
                if page is not None:
                    page.goto(
                        "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
            except Exception:
                pass
        self._phase = "waiting"
        self._table_id = ""
        self._game_ws_url = ""
        self._game_id = ""
        self._user_id = ""
        self._bets_open_game_id = ""
        self._bets_closed_game_id = ""

    # ──────────────────────────────────────────────────────────────────────
    # クリックBET（multi-lobby専用）
    # ──────────────────────────────────────────────────────────────────────

    def _find_pragmatic_frame(self):
        """client.pragmaticplaylive.net フレームを返す（見つからない場合None）。"""
        try:
            for _p in [self._lobby_page] + list(self._context.pages or []):
                for _fr in [_p] + list(_p.frames or []):
                    if 'pragmaticplaylive' in str(getattr(_fr, 'url', '')):
                        return _fr
        except Exception:
            pass
        return None

    _CHIP_DENOMS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000]

    def _pick_chip_denom(self, amount: float) -> str:
        """BET額に最も近い（超えない）チップ額の文字列を返す。"""
        best = self._CHIP_DENOMS[0]
        for d in self._CHIP_DENOMS:
            if d <= amount + 0.001:
                best = d
        # 整数なら整数表記、小数ならそのまま
        return str(int(best)) if best == int(best) else str(best)

    def _click_place_bet(self, side: str, amount: float) -> bool:
        """Pragmatic multi-play フレームでクリックBETを実行する。
        side: 'P'=Player, 'B'=Banker, 'T'=Tie, 'PP'=P-Pair, 'BP'=B-Pair
        amount: BET額 (USDT)
        Returns True if click succeeded.
        """
        side_class = {'P': 'ym_yP', 'B': 'ym_yQ', 'T': 'ym_yB',
                      'PP': 'ym_yT', 'BP': 'ym_yU'}.get(side, 'ym_yP')

        frame = self._find_pragmatic_frame()
        if not frame:
            logger.warning("[CLICK-BET] Pragmatic frame not found")
            return False

        # チップ選択（BETSOPENごとに毎回実施して確実に選択）
        chip_str = self._pick_chip_denom(amount)
        chip_sel = f'[data-testid="chip-stack-value-{chip_str}"]'
        try:
            frame.click(chip_sel, timeout=2000)
            logger.info(f"[CLICK-BET] chip selected: {chip_str} (selector={chip_sel})")
        except Exception as ce:
            logger.warning(f"[CLICK-BET] chip select failed ({chip_sel}): {ce}")
            # チップが見つからなくてもBETクリックは試みる

        # BETスポットクリック: BETSOPENアクティブな要素を優先
        # Playwright の frame.click() は nested cross-origin iframe も自動処理
        for sel in [f'.{side_class}.ym_yn', f'.{side_class}']:
            try:
                frame.click(sel, timeout=2000)
                logger.info(f"[CLICK-BET] bet placed: side={side} selector={sel}")
                return True
            except Exception as e:
                logger.debug(f"[CLICK-BET] frame.click({sel}) failed: {e}")

        logger.warning(f"[CLICK-BET] all selectors failed for side={side}")
        return False

    def _ws_send(self, table_id: str, payload: str) -> dict:
        """game WS にメッセージを送信。"""
        match = f"tableId={table_id}"
        st = self._table_states.get(table_id) or {}
        ws_url = str(st.get("ws_url") or self._game_ws_url or "").strip()

        pages = [self._lobby_page] if self._lobby_page else []
        try:
            for p in (self._context.pages or []):
                if p not in pages:
                    pages.append(p)
        except Exception:
            pass

        def _try_open_send(url: str) -> dict:
            """__bacopy_ws_open_send: 既存 or 並列 WS を開いて送信 (Worker WS 対策)。"""
            best: dict[str, Any] = {"ok": False, "reason": "not_sent"}
            for page in pages:
                all_frames = [page]
                try:
                    all_frames += list(page.frames)
                except Exception:
                    pass
                for fr in all_frames:
                    try:
                        res = fr.evaluate(
                            "async (args) => window.__bacopy_ws_open_send "
                            "? await window.__bacopy_ws_open_send(args.url, args.payload, 3000) "
                            ": {ok:false, reason:'no_open_send'}",
                            {"url": url, "payload": payload},
                        )
                        if isinstance(res, dict) and res.get("ok"):
                            return res
                        if isinstance(res, dict) and res.get("reason") != "no_open_send":
                            best = res
                    except Exception:
                        pass
            return best

        def _send_once() -> dict:
            best: dict[str, Any] = {"ok": False, "reason": "not_sent"}
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
                        if isinstance(res, dict) and res.get("reason") != "no_bridge":
                            best = res
                    except Exception:
                        pass
            return best

        def _try_worker_send() -> dict:
            """Worker内のWSへ postMessage 経由でBETを送信。全フレームを試す。"""
            best: dict[str, Any] = {"ok": False, "reason": "worker_not_tried"}
            for page in pages:
                all_frames = [page]
                try:
                    all_frames += list(page.frames)
                except Exception:
                    pass
                for fr in all_frames:
                    try:
                        res = fr.evaluate(
                            "(p) => window.__bacopy_worker_ws_send"
                            " ? window.__bacopy_worker_ws_send(p)"
                            " : {ok:false,reason:'no_worker_bridge'}",
                            payload,
                        )
                        if isinstance(res, dict) and res.get("workers", 0) > 0:
                            logger.info(f"[WS-SEND] worker_ws_send frame={getattr(fr,'url','?')[:60]}: {res}")
                        if isinstance(res, dict) and res.get("ok"):
                            return res
                        if isinstance(res, dict) and res.get("reason") != "no_worker_bridge":
                            best = res
                    except Exception:
                        pass
            logger.info(f"[WS-SEND] worker bridge (all frames) best={best}")
            return best

        # 0. Playwright WS プロキシルート経由（multi-lobby 最優先）
        if self._multi_lobby_mode and self._ws_proxy_server is not None:
            try:
                self._ws_proxy_server.send(payload)
                logger.info("[WS-SEND] ws_proxy_server.send OK")
                return {"ok": True, "mode": "ws_proxy"}
            except Exception as _e:
                logger.warning(f"[WS-SEND] ws_proxy_server.send failed: {_e}")
                with self._lock:
                    self._ws_proxy_server = None

        # 1. Worker ブリッジ経由（multi-lobby モードの次点ルート）
        if self._multi_lobby_mode:
            res_w = _try_worker_send()
            if isinstance(res_w, dict) and res_w.get("ok"):
                return res_w
            logger.info(f"[WS-SEND] worker bridge result: {res_w} — falling back")

        # 2. 並列 WS open_send（non-multi モードのみ / freeze 回避のため multi では使わない）
        if ws_url and not self._multi_lobby_mode:
            res = _try_open_send(ws_url)
            if isinstance(res, dict) and res.get("ok"):
                return res

        # 3. 既存 __bacopy_ws_send（ページ文脈の WS）
        first = _send_once()
        if isinstance(first, dict) and first.get("ok"):
            return first

        # 4. bridge 未注入なら再注入して再試行
        for page in pages:
            try:
                self._inject_all(page)
            except Exception:
                pass
        if self._multi_lobby_mode:
            res_w2 = _try_worker_send()
            if isinstance(res_w2, dict) and res_w2.get("ok"):
                return res_w2
        if ws_url and not self._multi_lobby_mode:
            res2 = _try_open_send(ws_url)
            if isinstance(res2, dict) and res2.get("ok"):
                return res2
        second = _send_once()
        if isinstance(second, dict) and second.get("ok"):
            return second
        return second

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
        target = str(qpid or table_id or "").strip()
        if target:
            st = self._ensure_table_state(target)
            if table_name:
                st["table_name"] = table_name
        # 既に正しいテーブルにいる場合はスイッチしない（再入場による signal too old を防ぐ）
        already_in = (self._phase == "ready" and target and self._table_id == target)
        # switch が進行中 or 要求済みで同一テーブルなら二重ナビゲーション不要
        pending_req = (self._switch_request or {}).get("table_id", "") or (self._switch_request or {}).get("qpid", "")
        already_switching_to_target = bool(pending_req and (pending_req == target)) or self._switch_in_progress
        if not already_in and not already_switching_to_target:
            # multi-lobby: scroll のみ（クリックで個別テーブルへ遷移させない）
            _intent = "preposition" if self._multi_lobby_mode else "prepare"
            self._request_switch(str(table_id or ""), table_name, qpid, intent=_intent)
        bet_id = f"dl_{uuid.uuid4().hex[:12]}"
        self.send_bet(side=side, amount=amount, table_id=target, bet_id=bet_id)
        return bet_id

    def consume_sent_bet(self, bet_id: str) -> bool:
        bid = str(bet_id or "").strip()
        if not bid:
            return False
        if bid in self._sent_bet_ids:
            self._sent_bet_ids.remove(bid)
            return True
        return False

    def has_sent_bet(self, bet_id: str) -> bool:
        bid = str(bet_id or "").strip()
        if not bid:
            return False
        return bid in self._sent_bet_ids

    def return_to_lobby(self) -> None:
        """テーブルを離れてロビーへ戻る。内部stateをリセットし実際にナビゲートする。"""
        logger.info(f"[RETURN-LOBBY] called: phase={self._phase} table={self._table_id!r} pending_bet={bool(self._pending_bet)} multi={self._multi_lobby_mode}")
        self._phase = "waiting"
        self._table_id = ""
        self._game_ws_url = ""
        self._game_id = ""
        self._user_id = ""
        self._bets_open_game_id = ""
        self._bets_closed_game_id = ""
        self._last_bets_open_at = 0.0
        self._switch_request = None
        self._switch_target_table_id = ""
        self._pending_bet = None
        if self._multi_lobby_mode:
            logger.info("[RETURN-LOBBY] multi-lobby: state reset only (no navigation)")
            return
        page = self._bet_page or self._lobby_page
        if page is not None:
            try:
                page.goto(
                    "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                logger.info("[LIVE] returned to lobby")
            except Exception as e:
                logger.warning(f"[LIVE] return_to_lobby navigation failed: {e}")

    def _request_switch(self, table_id: str = "", table_name: str = "", qpid: str = "", intent: str = "preposition") -> None:
        """対象卓への switch 要求をキューに積む。"""
        tid = str(table_id or "").strip()
        if not tid and not qpid:
            return
        self._switch_request = {
            "table_id": tid or str(qpid or "").strip(),
            "table_name": str(table_name or "").strip(),
            "qpid": str(qpid or "").strip(),
            "intent": str(intent or "preposition").strip().lower(),
        }

    def set_table_name(self, table_id: str, table_name: str) -> None:
        """bot から table_name を補完する。"""
        if table_id and table_name:
            st = self._ensure_table_state(table_id)
            st["table_name"] = table_name
        if table_id == self._table_id and not self._table_name:
            self._table_name = table_name

    def _install_ws_proxy_route(self, context: Any) -> None:
        """Playwright route_web_socket で game WS を透過プロキシ化して BET 送信口を確立。"""
        def _handle_ws(route: Any) -> None:
            try:
                logger.info(f"[WS-PROXY] intercepted WS: {getattr(route, 'url', '?')[:100]}")
                server = route.connect_to_server()
                with self._lock:
                    self._ws_proxy_server = server
                    self._ws_proxy_route = route
                logger.info("[WS-PROXY] transparent proxy established — BET送信口 OK")
                route.on_message(lambda msg: _safe_send(server, msg))
                server.on_message(lambda msg: _safe_send(route, msg))
            except Exception as e:
                logger.warning(f"[WS-PROXY] proxy setup error: {e}")
                try:
                    route.continue_()
                except Exception:
                    pass

        def _safe_send(target: Any, msg: Any) -> None:
            try:
                target.send(msg)
            except Exception:
                pass

        for _pattern in ["**pragmaticplaylive.net/game**", "wss://**pragmaticplaylive**", "**"]:
            try:
                context.route_web_socket(_pattern, _handle_ws)
                logger.info(f"[WS-PROXY] route_web_socket registered pattern={_pattern!r}")
                break
            except AttributeError:
                logger.info("[WS-PROXY] context.route_web_socket not available — trying page level")
                try:
                    if self._lobby_page:
                        self._lobby_page.route_web_socket(_pattern, _handle_ws)
                        logger.info(f"[WS-PROXY] page.route_web_socket registered pattern={_pattern!r}")
                except AttributeError:
                    logger.info("[WS-PROXY] route_web_socket not available in this Playwright version")
                except Exception as _e2:
                    logger.warning(f"[WS-PROXY] page.route_web_socket failed: {_e2}")
                break
            except Exception as e:
                logger.warning(f"[WS-PROXY] route_web_socket pattern={_pattern!r} failed: {e}")
                continue

    def _run_dom_dump(self) -> None:
        """マルチプレイゲームフレームのDOM構造をログに出力する（調査用・1回だけ）。"""
        # stake.com のベットボタン特定JS（テキスト・座標・セレクター）
        _DOM_JS = r"""() => {
  function sc(v,n){try{return String(v||'').slice(0,n);}catch(e){return '';}}
  const r={};
  r.url=location.href.slice(0,100);
  r.title=document.title;

  // 1. テキストでP/B/T系要素を検索
  const betTexts=['プレイヤー','バンカー','タイ','プペア','バペア','Player','Banker','Tie','PLAYER','BANKER','TIE'];
  const byText=[];
  for(const el of document.querySelectorAll('*')){
    try{
      const t=(el.childNodes.length===1&&el.childNodes[0].nodeType===3)?el.textContent.trim():'';
      if(betTexts.some(bt=>t===bt||t.includes(bt))){
        const rc=el.getBoundingClientRect();
        byText.push({tag:el.tagName,txt:sc(t,30),cls:sc(el.className,80),id:sc(el.id,20),
          ti:el.getAttribute('data-testid')||'',
          x:Math.round(rc.x),y:Math.round(rc.y),w:Math.round(rc.width),h:Math.round(rc.height)});
      }
    }catch(e){}
  }
  r.byText=byText.slice(0,60);

  // 2. elementFromPoint で座標からセレクター取得（スクショ座標ベース）
  const testPoints=[
    {label:'bet_player_tile1',x:207,y:652},{label:'bet_banker_tile1',x:410,y:652},
    {label:'bet_tie_tile1',x:318,y:652},{label:'bet_player_tile2',x:600,y:652},
    {label:'bet_banker_tile2',x:810,y:652},{label:'bet_player_tile3',x:1010,y:652},
  ];
  const fromPoints=[];
  for(const pt of testPoints){
    try{
      const el=document.elementFromPoint(pt.x,pt.y);
      if(el){
        const rc=el.getBoundingClientRect();
        fromPoints.push({label:pt.label,tag:el.tagName,txt:sc(el.textContent,30),
          cls:sc(el.className,80),id:sc(el.id,20),ti:el.getAttribute('data-testid')||'',
          x:Math.round(rc.x),y:Math.round(rc.y),w:Math.round(rc.width),h:Math.round(rc.height)});
      }
    }catch(e){}
  }
  r.fromPoints=fromPoints;

  // 3. 全テーブルタイル（ゲームグリッドの各タイルを特定）
  const tiles=[];
  for(const el of document.querySelectorAll('[data-testid],[class*=tile],[class*=card],[class*=game-item],[class*=table-item],[class*=lobby]')){
    const rc=el.getBoundingClientRect();
    if(rc.width<100||rc.height<100) continue;
    const ti=el.getAttribute('data-testid')||'';
    tiles.push({tag:el.tagName,cls:sc(el.className,60),ti,x:Math.round(rc.x),y:Math.round(rc.y),w:Math.round(rc.width),h:Math.round(rc.height)});
  }
  r.tiles=tiles.slice(0,30);

  return r;
}"""
        try:
            pages = [self._lobby_page]
            try:
                for _p in (self._context.pages or []):
                    if _p not in pages:
                        pages.append(_p)
            except Exception:
                pass
            for _page in pages:
                all_frames = [_page]
                try:
                    all_frames += list(_page.frames)
                except Exception:
                    pass
                for _fr in all_frames:
                    _furl = getattr(_fr, 'url', '?')
                    if 'pragmatic' not in _furl and 'zmcdpj' not in _furl:
                        continue
                    try:
                        _dom = _fr.evaluate(_DOM_JS)
                        _fu = (_dom.get('url') or _furl)[:80]
                        logger.info(f"[DOM2] frame={_fu} title={_dom.get('title')}")
                        for _bt in (_dom.get('byText') or []):
                            logger.info(f"[DOM2-BYTEXT] {_bt}")
                        for _fp in (_dom.get('fromPoints') or []):
                            logger.info(f"[DOM2-POINT] {_fp}")
                        for _tl in (_dom.get('tiles') or []):
                            logger.info(f"[DOM2-TILE] {_tl}")
                    except Exception as _de:
                        logger.info(f"[DOM2] frame={_furl[:80]} ERROR={_de}")
                    # スクリーンショット（Pragmaticフレームのみ・ページレベルで撮影）
                    if 'pragmaticplaylive' in _furl:
                        try:
                            _ss_path = r"C:\bacopy\debug_prag_frame.png"
                            _fr.page.screenshot(path=_ss_path)
                            logger.info(f"[DOM2] screenshot saved: {_ss_path}")
                        except Exception as _se:
                            logger.info(f"[DOM2] screenshot error: {_se}")
        except Exception:
            pass

    def set_profile_dir(self, path: str) -> None:
        """プロファイルディレクトリを設定 (bot.run() から呼ばれる)。"""
        self._profile_dir = path
        self._load_table_focus_cache()
        self._try_discover_user_id(force=True)

    def _focus_cache_path(self) -> str:
        if not self._profile_dir:
            return ""
        return os.path.join(self._profile_dir, "table_focus_cache.json")

    def _load_table_focus_cache(self) -> None:
        path = self._focus_cache_path()
        if not path or not os.path.exists(path):
            return
        try:
            raw = json.loads(open(path, "r", encoding="utf-8", errors="ignore").read() or "{}")
            if isinstance(raw, dict):
                out: dict[str, dict[str, Any]] = {}
                for k, v in raw.items():
                    if not isinstance(v, dict):
                        continue
                    key = str(k or "").strip()
                    if not key:
                        continue
                    out[key] = {
                        "table_name": str(v.get("table_name") or ""),
                        "frame_index": int(v.get("frame_index") or 0),
                        "match_index": int(v.get("match_index") or -1),
                        "total_nodes": int(v.get("total_nodes") or 0),
                        "updated_at": float(v.get("updated_at") or 0.0),
                    }
                self._table_focus_cache = out
                logger.info(f"[FOCUS-CACHE] loaded {len(out)} entries")
        except Exception as e:
            logger.debug(f"[FOCUS-CACHE] load failed: {e}")

    def _save_table_focus_cache(self, force: bool = False) -> None:
        path = self._focus_cache_path()
        if not path:
            return
        now = time.time()
        if (not force) and (now - self._last_focus_cache_save_at < 1.0):
            return
        self._last_focus_cache_save_at = now
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._table_focus_cache, f, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            logger.debug(f"[FOCUS-CACHE] save failed: {e}")

    def _persist_user_id(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid or not self._profile_dir:
            return
        try:
            p = os.path.join(self._profile_dir, "last_user_id.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(uid)
        except Exception:
            pass

    def _try_discover_user_id(self, force: bool = False) -> str:
        now = time.time()
        if (not force) and now - self._last_user_id_probe_at < 20.0:
            return str(self._user_id or "")
        self._last_user_id_probe_at = now
        if self._user_id:
            return self._user_id
        if not self._profile_dir:
            return ""

        # 1) cached file from previous runs
        try:
            p = os.path.join(self._profile_dir, "last_user_id.txt")
            if os.path.exists(p):
                uid = open(p, "r", encoding="utf-8", errors="ignore").read().strip()
                if uid:
                    self._user_id = uid
                    logger.info(f"[USER-ID] loaded from cache: ...{uid[-8:]}")
                    return uid
        except Exception:
            pass

        # 2) webappsstore(localStorage) pattern scan
        try:
            db = os.path.join(self._profile_dir, "webappsstore.sqlite")
            if os.path.exists(db):
                conn = sqlite3.connect(db)
                try:
                    cur = conn.cursor()
                    rows = cur.execute("SELECT value FROM webappsstore2 LIMIT 3000").fetchall()
                    pats = [
                        r'"userId"\s*:\s*"([^"]+)"',
                        r'"user_id"\s*:\s*"([^"]+)"',
                        r'"uid"\s*:\s*"([^"]+)"',
                    ]
                    for (v,) in rows:
                        s = v if isinstance(v, str) else str(v or "")
                        if len(s) < 6:
                            continue
                        for pat in pats:
                            m = re.search(pat, s)
                            if m:
                                uid = str(m.group(1) or "").strip()
                                if uid:
                                    self._user_id = uid
                                    self._persist_user_id(uid)
                                    logger.info(f"[USER-ID] discovered from webappsstore: ...{uid[-8:]}")
                                    return uid
                finally:
                    conn.close()
        except Exception:
            pass

        # 3) cookie JWT payload scan
        try:
            db = os.path.join(self._profile_dir, "cookies.sqlite")
            if os.path.exists(db):
                conn = sqlite3.connect(db)
                try:
                    cur = conn.cursor()
                    rows = cur.execute("SELECT name, value FROM moz_cookies WHERE host LIKE '%stake.com%' LIMIT 200").fetchall()
                    for name, value in rows:
                        val = str(value or "")
                        parts = val.split(".")
                        if len(parts) < 2:
                            continue
                        mid = parts[1]
                        pad = "=" * (-len(mid) % 4)
                        try:
                            payload = base64.urlsafe_b64decode((mid + pad).encode("utf-8")).decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                        for key_pat in [r'"userId"\s*:\s*"([^"]+)"', r'"sub"\s*:\s*"([^"]+)"', r'"id"\s*:\s*"([^"]+)"']:
                            m = re.search(key_pat, payload)
                            if not m:
                                continue
                            uid = str(m.group(1) or "").strip()
                            if uid:
                                self._user_id = uid
                                self._persist_user_id(uid)
                                logger.info(f"[USER-ID] discovered from cookie:{name} ...{uid[-8:]}")
                                return uid
                finally:
                    conn.close()
        except Exception:
            pass
        return ""

    def _save_last_table(self) -> None:
        """最後のテーブルIDをファイルに保存 (再起動後の自動再入場用)。Speed/Turboは保存しない。"""
        if not self._profile_dir or not self._table_id:
            return
        tname_lower = (self._table_name or self._table_id).lower()
        if "speed" in tname_lower or "turbo" in tname_lower:
            logger.info(f"[LIVE] last_table skip (fast table): {self._table_name}")
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
    def is_bet_in_flight(self) -> bool:
        """BET送信済み・結果待ち中かどうか。True の間は新 decision を受け付けない。"""
        return bool(self._sent_bet_ids) or self._bet_send_in_progress

    @property
    def is_ready(self) -> bool:
        return self._phase in ("ready", "betting")

    def can_switch(self) -> bool:
        """テーブル切替を受け付けるか。
        waiting フェーズ（ロビー待機中）のみ切替可。
        ready/betting（テーブル入場済み）では切替しない。
        """
        if self._switch_in_progress or self._pending_bet:
            return False
        if self._multi_lobby_mode:
            return True
        return self._phase == "waiting"

    def is_on_table(self, table_id: str) -> bool:
        return bool(table_id) and str(table_id).strip() == str(self._table_id).strip()
