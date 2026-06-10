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
import socket
import sqlite3
import ssl
import struct
import threading
import time
import base64
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Any

# bot と同じロガーを使うことでログファイルへの書き込みを保証する
logger = logging.getLogger("dual_line.bot")

PRAGMATIC_BACCARAT_LOBBY_URL = os.getenv(
    "BACOPY_PRAGMATIC_LOBBY_URL",
    "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat",
)

# ── 生CDP WebSocket ヘルパ（stdlibのみ） ──────────────────────────────
# Pragmatic の lobby2 は cross-origin OOPIF。Playwright の page.on("websocket")
# は OOPIF の dga WS を page に上げず、また hook 前から開いている WS を取り逃す
# (= 過去シャドウが継続しなかった真因)。CDP /json は iframe を独立 target として
# webSocketDebuggerUrl 付きで露出するので、そこへ生CDPで接続し Network.enable →
# webSocketFrameReceived を購読すれば dga gameResult を確実かつ継続的に拾える
# (_cdp_ws_monitor.py で 60s/109件・10分/1094件 と実証済み)。読み取り専用。
_CDP_HOST = os.getenv("BACOPY_CDP_HOST", "127.0.0.1")
_CDP_PORT = int(os.getenv("BACOPY_CDP_PORT", "9222") or 9222)


def _cdp_pick_pragmatic_target():
    """Return the CDP target (dict) for the Pragmatic dga frame, or None.
    Prefers the persistent lobby2 frame (continuous dga) over a transient
    multibaccarat frame."""
    raw = urllib.request.urlopen(f"http://{_CDP_HOST}:{_CDP_PORT}/json", timeout=6).read()
    targets = json.loads(raw)
    prag = [t for t in targets if "pragmaticplaylive" in (t.get("url") or "")
            and t.get("webSocketDebuggerUrl")]
    if not prag:
        return None
    lobby = [t for t in prag if "lobby2" in (t.get("url") or "")]
    return (lobby or prag)[0]


def _cdp_ws_connect(ws_url: str):
    path = ws_url.split(f"{_CDP_HOST}:{_CDP_PORT}", 1)[1]
    s = socket.create_connection((_CDP_HOST, _CDP_PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {_CDP_HOST}:{_CDP_PORT}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            raise RuntimeError("cdp ws handshake closed")
        buf += c
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError("cdp ws handshake failed")
    return s


def _cdp_ws_send(s, text: str):
    p = text.encode("utf-8")
    h = bytearray([0x81])
    n = len(p)
    mask = os.urandom(4)
    if n < 126:
        h.append(0x80 | n)
    elif n < 65536:
        h.append(0x80 | 126); h += struct.pack(">H", n)
    else:
        h.append(0x80 | 127); h += struct.pack(">Q", n)
    h += mask
    s.sendall(bytes(h) + bytes(b ^ mask[i % 4] for i, b in enumerate(p)))


def _cdp_ws_recv_into(s, recv_exact):
    data = b""
    while True:
        b0 = recv_exact(s, 1)[0]
        fin = b0 & 0x80
        op = b0 & 0x0F
        b1 = recv_exact(s, 1)[0]
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", recv_exact(s, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", recv_exact(s, 8))[0]
        mk = recv_exact(s, 4) if masked else b""
        pl = recv_exact(s, ln) if ln else b""
        if masked:
            pl = bytes(b ^ mk[i % 4] for i, b in enumerate(pl))
        if op == 0x8:
            raise RuntimeError("cdp ws server close")
        if op in (0x9, 0xA):
            continue
        data += pl
        if fin:
            return data.decode("utf-8", "replace")


# ── 直結 dga lobby WS（ブラウザ不要・Stakeセッション不要） ─────────────
# Pragmatic の dga lobby WS は casinoId だけで購読でき、Stakeログイン/Cookie 不要
# (= 第2セッションにならない → アンチアビューズの懸念なし)。betting frame が
# multibaccarat (結果フィード無し) でも、ここから全卓の gameResult を継続受信
# できる。プロトコルは実機キャプチャ (pragmatic_dumps) から復元:
#   -> {"type":"statistics"}
#   -> {"type":"available","casinoId":...}
#   -> {"type":"subscribe","isDeltaEnabled":true,"casinoId":...,"key":[...],"currency":"USD"}
#   -> {"type":"ping","pingTime":<ms>}  (~5秒ごと)
# bafather実測: 60秒/164 gameResult/61卓 (_dga_direct_test.py)。read-only。
_DGA_WS_HOST = os.getenv("BACOPY_DGA_WS_HOST", "dga.pragmaticplaylive.net")
_DGA_WS_ORIGIN = os.getenv("BACOPY_DGA_WS_ORIGIN", "https://client.pragmaticplaylive.net")
_DGA_CASINO_ID = os.getenv("BACOPY_DGA_CASINO_ID", "ppcds00000003709")
_DGA_TABLE_KEYS = [
    "007", "415", "440", "488", "489", "490", "461", "468", "466", "455", "454",
    "402", "442", "403", "404", "441", "401", "4511", "412", "4512", "413", "421",
    "424", "422", "405", "414", "438", "467", "411", "450", "431", "427", "481",
    "425", "434", "436", "851", "435", "428", "432", "433", "430", "459", "451",
    "452", "439", "482", "426", "458", "449", "483", "456", "2101", "476", "460",
    "480", "479", "499", "484", "477", "496", "453",
]


def _dga_direct_connect():
    """Open a TLS WebSocket to the Pragmatic dga lobby endpoint and complete the
    handshake. Returns the connected ssl socket (ready for _cdp_ws_send / the
    _cdp_ws_recv_into framing). Cert verification is skipped: read-only PUBLIC
    game-result data, the same endpoint the betting browser already trusts;
    avoids Windows CA-store issues on bafather."""
    raw = socket.create_connection((_DGA_WS_HOST, 443), timeout=12)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = ctx.wrap_socket(raw, server_hostname=_DGA_WS_HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        f"GET /ws HTTP/1.1\r\nHost: {_DGA_WS_HOST}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Origin: {_DGA_WS_ORIGIN}\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            raise RuntimeError("dga handshake closed")
        buf += c
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError("dga handshake not 101")
    return s

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

_LOBBY_SCROLL_PROBE_JS = r"""
async (args) => {
  // Evidence-only probe: do not change scroll. Reports every plausible
  // scroll container and any element using transform translateY (which is
  // how React-Window / react-virtualized drives offset without scrollTop).
  const qpid = String((args && args.qpid) || '').trim();
  const out = {
    href: String(location.href || '').slice(0, 160),
    scrollingElement: null,
    documentScrollTop: 0,
    windowScroll: { x: window.scrollX || 0, y: window.scrollY || 0 },
    tile: null,
    scrollAncestors: [],
    transformElements: [],
    virtualizerHints: [],
  };
  try {
    const se = document.scrollingElement || document.documentElement;
    out.scrollingElement = {
      tag: se && se.tagName,
      scrollTop: Number((se && se.scrollTop) || 0),
      scrollHeight: Number((se && se.scrollHeight) || 0),
      clientHeight: Number((se && se.clientHeight) || 0),
    };
    out.documentScrollTop = Number((document.documentElement || {}).scrollTop || 0);
  } catch (_) {}

  let tile = null;
  try {
    if (qpid) tile = document.getElementById('TileHeight-' + qpid);
    if (!tile) tile = document.querySelector('[id^="TileHeight-"]');
  } catch (_) {}
  if (tile) {
    try {
      const r = tile.getBoundingClientRect();
      out.tile = {
        id: tile.id,
        rect: { top: r.top, left: r.left, width: r.width, height: r.height },
      };
    } catch (_) {}
  }

  function describe(el, depth) {
    try {
      const cs = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        depth,
        tag: el.tagName,
        id: el.id || '',
        cls: (el.className && String(el.className).slice(0, 80)) || '',
        scrollTop: Number(el.scrollTop || 0),
        scrollHeight: Number(el.scrollHeight || 0),
        clientHeight: Number(el.clientHeight || 0),
        overflowY: cs.overflowY,
        overflowX: cs.overflowX,
        transform: cs.transform && cs.transform !== 'none' ? cs.transform.slice(0, 120) : '',
        rect: { top: r.top, left: r.left, width: r.width, height: r.height },
      };
    } catch (_) {
      return null;
    }
  }

  if (tile) {
    let cur = tile;
    let d = 0;
    while (cur && d < 25) {
      const desc = describe(cur, d);
      if (desc) {
        if (desc.scrollHeight > desc.clientHeight + 2) out.scrollAncestors.push(desc);
        if (desc.transform && desc.transform.indexOf('translate') >= 0) out.transformElements.push(desc);
      }
      cur = cur.parentElement;
      d += 1;
    }
  }

  try {
    const candidates = document.querySelectorAll(
      '[class*="virtual"],[class*="Virtual"],[class*="list"],[class*="List"],[role="grid"],[role="list"]'
    );
    let n = 0;
    for (const el of candidates) {
      if (n >= 8) break;
      const desc = describe(el, -1);
      if (desc && (desc.scrollHeight > desc.clientHeight + 2 || desc.transform)) {
        out.virtualizerHints.push(desc);
        n += 1;
      }
    }
  } catch (_) {}

  return out;
}
"""

_MULTI_LOBBY_FOCUS_JS = r"""
async (args) => {
  const qpid = String((args && args.qpid) || '').trim();
  const click = !!(args && args.click);
  const maxScroll = Math.max(1, Number((args && args.maxScroll) || 48));
  const hintIndex = Number.isFinite(Number(args && args.hintIndex)) ? Number(args && args.hintIndex) : -1;
  const hintTotal = Math.max(0, Number((args && args.hintTotal) || 0));
  const hintScrollTop = Number.isFinite(Number(args && args.hintScrollTop)) ? Number(args && args.hintScrollTop) : -1;
  const hintScrollRatio = Number.isFinite(Number(args && args.hintScrollRatio)) ? Number(args && args.hintScrollRatio) : -1;
  const candidates = Array.isArray(args && args.candidates) ? args.candidates : [];
  const holdOnly = !!(args && args.holdOnly);
  const norm = (s) => String(s || '').replace(/\s+/g, '').replace(/[$￥¥]/g, '').toLowerCase();
  const candNorm = candidates.map(norm).filter(Boolean);
  const maxMsRaw = Number(args && args.maxMs);
  const maxMs = Number.isFinite(maxMsRaw) && maxMsRaw > 0 ? maxMsRaw : 6000;
  const deadline = Date.now() + Math.max(500, maxMs);
  const href = String(location.href || '');
  const isMultiBaccaratDom =
    href.includes('/desktop/multibaccarat') || !!document.querySelector('[id^="TileHeight-"]');
  if (!isMultiBaccaratDom) {
    return {
      ok: true, found: false, clicked: false, reason: 'not_multi_baccarat_dom',
      href: href.slice(0, 120), matchIndex: -1, totalNodes: 0
    };
  }

  function containsQpid(v) {
    const s = String(v || '');
    if (!qpid || !s) return false;
    return s.includes('/snaps/' + qpid + '/') || s.includes('tableId=' + qpid) || s.includes('tableid=' + qpid) || s === qpid || s.includes('TileHeight-' + qpid);
  }
  function isCandidateText(text) {
    const t = norm(text);
    if (!t) return false;
    for (const c of candNorm) {
      if (t === c) return true;
      // A table such as "Thai Baccarat 1" must not match the chip button "1".
      if (t.length >= 4 && c.length >= 4 && (t.includes(c) || c.includes(t))) return true;
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
  function hasBetCells(el) {
    if (!el || !el.querySelectorAll) return false;
    let count = 0;
    for (const cell of el.querySelectorAll('.ym_qA')) {
      try {
        const r = cell.getBoundingClientRect();
        if (r && r.width > 10 && r.height > 10) count += 1;
      } catch(_) {}
      if (count >= 3) return true;
    }
    return false;
  }
  function qpidTarget(el) {
    const target = clickableAncestor(el);
    const tile = String((el && el.id) || '').startsWith('TileHeight-') ? el : (target || el);
    const assistStatus = String((args && args.assistStatus) || '').toUpperCase();
    if (assistStatus && String((tile && tile.id) || '').startsWith('TileHeight-')) return target || tile;
    if (String((tile && tile.id) || '').startsWith('TileHeight-') && !hasBetCells(tile)) return null;
    return target;
  }
  function centerInScroller(el, sc, deadband) {
    if (!el || !sc) return false;
    // Global calm gate: if the tile is already comfortably within the scroller
    // viewport, do NOT scroll at all. Only an off-screen/clipped tile triggers a
    // scroll (the wanted "scroll-to" behavior). This is what stops the constant
    // re-centering that made the surrounding view drift while watching.
    if (typeof isTileVisibleEnough === 'function' && isTileVisibleEnough(el, sc, 30)) return false;
    // deadband: only re-center when the tile drifts MORE than this many px from
    // the scroller center. A large deadband keeps the page visually calm during
    // the maintenance loop (the user watches the tile; constant micro-snapping
    // makes it impossible to read). Default 8 for explicit/initial centering.
    const db = Math.max(2, Number(deadband) || 8);
    try {
      const outer = snapshotOuterScroll();
      const er = el.getBoundingClientRect();
      const sr = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {top:0, left:0, width:window.innerWidth, height:window.innerHeight};
      const dy = (er.top + er.height / 2) - (sr.top + sr.height / 2);
      const dx = (er.left + er.width / 2) - (sr.left + sr.width / 2);
      let moved = false;
      if (Math.abs(dy) > db) { sc.scrollTop = Math.max(0, Number(sc.scrollTop || 0) + dy); moved = true; }
      if (Math.abs(dx) > db && typeof sc.scrollLeft === 'number') { sc.scrollLeft = Math.max(0, Number(sc.scrollLeft || 0) + dx); moved = true; }
      restoreOuterScroll(outer);
      return moved;
    } catch(_) {
      return false;
    }
  }
  // True when the tile is already comfortably inside the scroller viewport, so
  // the maintenance hold can SKIP scrolling entirely (no jitter). Only when the
  // tile is clipped/out of view do we re-center. pad = px of slack at edges.
  function isTileVisibleEnough(el, sc, pad) {
    try {
      const er = el.getBoundingClientRect();
      const sr = (sc && sc.getBoundingClientRect)
        ? sc.getBoundingClientRect()
        : {top: 0, left: 0, bottom: window.innerHeight, right: window.innerWidth};
      const p = Number.isFinite(Number(pad)) ? Number(pad) : 6;
      return (er.top >= sr.top - p) && (er.bottom <= sr.bottom + p) &&
             (er.left >= sr.left - p) && (er.right <= sr.right + p);
    } catch(_) {
      return false;
    }
  }
  function installAssistScrollLock(tile, sc, ttl) {
    try {
      if (!tile || !sc) return false;
      const qid = String((tile.id || '').replace(/^TileHeight-/, '') || qpid || '');
      const until = Date.now() + Math.max(2000, Number(ttl) || 15000);
      // Maintenance deadband + cadence. Bigger/slower = calmer page so the
      // operator can actually watch the tile. The lock's real job is only to
      // stop the React virtualizer from UNMOUNTING the target tile (which
      // breaks the bet click), not to pixel-lock it. Tunable from Python env.
      const recenterDeadband = Math.max(8, Number(args && args.recenterDeadband) || 48);
      const windowDrift = Math.max(recenterDeadband, Number(args && args.windowDrift) || recenterDeadband);
      // scrollFight=false (default): install NO scroll-fighting handlers. The
      // lock then only centers the tile ONCE (visibility-gated). This stops the
      // wheel/pointer blocking, onScroll snap-back and window.scrollTo restore
      // that were fighting the operator's own scrolling ("outer keeps moving").
      const scrollFight = String((args && args.scrollFight) || '') === '1';
      let rafPending = false;
      let recenterBusy = false;
      const recenter = () => {
        try {
          const lock = window.__bacopyAssistScrollLock || {};
          if (!lock || Number(lock.until || 0) <= Date.now()) return;
          const target = lock.qpid
            ? document.getElementById('TileHeight-' + lock.qpid)
            : document.querySelector('.bacopy-assist-tile');
          const lockScroller = lock.scroller || sc;
          if (target && lockScroller) {
            recenterBusy = true;
            // While the tile is on screen, DO NOTHING — no scroll at all. This
            // is the "outer keeps moving / hard to watch" fix: the lock only
            // acts when the tile has actually left the viewport.
            if (!isTileVisibleEnough(target, lockScroller, recenterDeadband)) {
              const moved = centerInScroller(target, lockScroller, 8);
              if (moved) {
                lock.scrollTop = Number(lockScroller.scrollTop || 0);
                lock.scrollLeft = Number(lockScroller.scrollLeft || 0);
              }
            }
            recenterBusy = false;
          } else if (lockScroller) {
            // If a user scrolls far enough to unmount the virtualized tile,
            // the target cannot be found anymore. Snap the real scroller back
            // to the last centered position so the tile remounts.
            recenterBusy = true;
            try { lockScroller.scrollTop = Number(lock.scrollTop || 0); } catch(_) {}
            try { lockScroller.scrollLeft = Number(lock.scrollLeft || 0); } catch(_) {}
            recenterBusy = false;
          }
          // Do not fight tiny window-scroll drift; only restore on large jumps
          // (e.g. the page auto-scrolled the tile out of view).
          if (Math.abs((window.scrollX || 0) - Number(lock.windowX || 0)) > windowDrift ||
              Math.abs((window.scrollY || 0) - Number(lock.windowY || 0)) > windowDrift) {
            window.scrollTo(Number(lock.windowX || 0), Number(lock.windowY || 0));
          }
        } catch(_) {
          recenterBusy = false;
        }
      };
      const scheduleRecenter = () => {
        if (rafPending) return;
        rafPending = true;
        window.requestAnimationFrame(() => {
          rafPending = false;
          recenter();
        });
      };
      const active = () => {
        const lock = window.__bacopyAssistScrollLock || {};
        return lock && Number(lock.until || 0) > Date.now();
      };
      const block = (ev) => {
        if (!active()) return;
        try { ev.preventDefault(); } catch(_) {}
        try { ev.stopPropagation(); } catch(_) {}
        scheduleRecenter();
      };
      const onScroll = () => {
        if (!active() || recenterBusy) return;
        try {
          const lock = window.__bacopyAssistScrollLock || {};
          const lockScroller = lock.scroller;
          const target = lock.qpid ? document.getElementById('TileHeight-' + lock.qpid) : document.querySelector('.bacopy-assist-tile');
          if (lockScroller && !target) {
            lockScroller.scrollTop = Number(lock.scrollTop || 0);
            lockScroller.scrollLeft = Number(lock.scrollLeft || 0);
          }
        } catch(_) {}
        scheduleRecenter();
      };
      const onKey = (ev) => {
        if (!active()) return;
        const k = String(ev.key || '');
        if (['ArrowDown','ArrowUp','PageDown','PageUp','Home','End',' '].includes(k)) block(ev);
      };
      window.__bacopyAssistScrollLock = {
        qpid: qid,
        scroller: sc,
        until: until,
        windowX: window.scrollX || 0,
        windowY: window.scrollY || 0,
        scrollTop: Number(sc.scrollTop || 0),
        scrollLeft: Number(sc.scrollLeft || 0),
      };
      // Block pointer/mouse down events on the scroll container background so
      // React's virtualizer cannot receive the "scroll to clicked row" gesture.
      // Clicks originating FROM the target tile are allowed through.
      const blockPointer = (ev) => {
        if (!active()) return;
        try {
          const lock = window.__bacopyAssistScrollLock || {};
          const targetTile = lock.qpid
            ? document.getElementById('TileHeight-' + lock.qpid)
            : document.querySelector('.bacopy-assist-tile');
          if (targetTile) {
            let cur = ev.target;
            while (cur) {
              if (cur === targetTile) return; // click on our tile → allow
              cur = cur.parentElement;
            }
          }
          try { ev.preventDefault(); } catch(_) {}
          try { ev.stopPropagation(); } catch(_) {}
          scheduleRecenter();
        } catch(_) {}
      };
      if (scrollFight && !window.__bacopyAssistScrollLockInstalled) {
        window.__bacopyAssistScrollLockInstalled = true;
        window.addEventListener('wheel', block, {capture:true, passive:false});
        document.addEventListener('wheel', block, {capture:true, passive:false});
        window.addEventListener('touchmove', block, {capture:true, passive:false});
        document.addEventListener('touchmove', block, {capture:true, passive:false});
        window.addEventListener('keydown', onKey, {capture:true, passive:false});
        document.addEventListener('keydown', onKey, {capture:true, passive:false});
        window.addEventListener('scroll', onScroll, {capture:true, passive:true});
        document.addEventListener('scroll', onScroll, {capture:true, passive:true});
        // Block click-scroll on the lobby background (React virtualizer "scroll to
        // clicked row" gesture). Must be capture+non-passive to preventDefault.
        window.addEventListener('pointerdown', blockPointer, {capture:true, passive:false});
        document.addEventListener('pointerdown', blockPointer, {capture:true, passive:false});
        window.addEventListener('mousedown', blockPointer, {capture:true, passive:false});
        document.addEventListener('mousedown', blockPointer, {capture:true, passive:false});
        window.setInterval(() => {
          scheduleRecenter();
        }, Math.max(120, Number(args && args.recenterMs) || 800));
      }
      if (scrollFight) {
        try { sc.addEventListener('wheel', block, {capture:true, passive:false}); } catch(_) {}
        try { sc.addEventListener('touchmove', block, {capture:true, passive:false}); } catch(_) {}
        try { sc.addEventListener('pointerdown', blockPointer, {capture:true, passive:false}); } catch(_) {}
        try { sc.addEventListener('mousedown', blockPointer, {capture:true, passive:false}); } catch(_) {}
        try { sc.addEventListener('scroll', () => {
          const lock = window.__bacopyAssistScrollLock || {};
          if (Number(lock.until || 0) > Date.now()) {
            if (!recenterBusy) {
              const target = lock.qpid ? document.getElementById('TileHeight-' + lock.qpid) : document.querySelector('.bacopy-assist-tile');
              if (!target) {
                try { sc.scrollTop = Number(lock.scrollTop || 0); } catch(_) {}
                try { sc.scrollLeft = Number(lock.scrollLeft || 0); } catch(_) {}
              }
            }
            scheduleRecenter();
          }
        }, {capture:true, passive:true}); } catch(_) {}
      }
      recenter();
      return true;
    } catch(_) {
      return false;
    }
  }
  function clickEl(el) {
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
  function applyAssistOverlay(el, sc) {
    const status = String((args && args.assistStatus) || '').toUpperCase();
    if (!status) return false;
    const side = String((args && args.side) || '').toUpperCase();
    const amount = Number(args && args.amount);
    const tile = (() => {
      let cur = el;
      for (let i = 0; i < 8 && cur; i++) {
        if (String(cur.id || '').startsWith('TileHeight-')) return cur;
        cur = cur.parentElement;
      }
      return el;
    })();
    if (!tile || !tile.getBoundingClientRect) return false;
    try {
      if (status !== 'NOW') {
        const activeNow = document.querySelector('.bacopy-assist-tile.bacopy-assist-now');
        if (activeNow) {
          const until = Number(activeNow.getAttribute('data-bacopy-assist-until') || 0);
          if (!until || Date.now() < until) {
            return {
              ok: true,
              preservedNow: true,
              status,
              activeText: String(activeNow.innerText || activeNow.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
            };
          }
        }
      }
      if (!document.getElementById('bacopy-manual-assist-style')) {
        const st = document.createElement('style');
        st.id = 'bacopy-manual-assist-style';
        st.textContent = `
          @keyframes bacopyAssistPulse {
            0%,100% { box-shadow: 0 0 0 3px var(--bc-ring), 0 0 14px var(--bc-glow); }
            50% { box-shadow: 0 0 0 5px var(--bc-ring), 0 0 30px var(--bc-glow); }
          }
          .bacopy-assist-tile {
            position: relative !important;
            border-radius: 10px !important;
            outline: 4px solid var(--bc-ring) !important;
            outline-offset: -5px !important;
          }
          .bacopy-assist-tile.bacopy-assist-now {
            animation: bacopyAssistPulse .72s ease-in-out infinite !important;
          }
          .bacopy-assist-badge {
            position: absolute !important;
            z-index: 2147483647 !important;
            bottom: 6px !important;
            left: 50% !important;
            transform: translateX(-50%) !important;
            pointer-events: none !important;
            min-width: 72px !important;
            max-width: calc(100% - 18px) !important;
            padding: 4px 8px !important;
            border-radius: 999px !important;
            font: 800 13px/1.1 Arial, sans-serif !important;
            letter-spacing: 0 !important;
            color: #fff !important;
            text-align: center !important;
            background: var(--bc-bg) !important;
            border: 1px solid var(--bc-ring) !important;
            text-shadow: 0 1px 2px rgba(0,0,0,.55) !important;
            opacity: .82 !important;
          }
        `;
        document.head.appendChild(st);
      }
      for (const oldTile of document.querySelectorAll('.bacopy-assist-tile')) {
        if (oldTile !== tile) {
          try {
            oldTile.classList.remove('bacopy-assist-tile', 'bacopy-assist-now', 'bacopy-assist-ready');
            oldTile.style.removeProperty('--bc-ring');
            oldTile.style.removeProperty('--bc-glow');
            oldTile.style.removeProperty('--bc-bg');
            for (const oldBadge of oldTile.querySelectorAll(':scope > .bacopy-assist-badge')) oldBadge.remove();
          } catch(_) {}
        }
      }
      tile.classList.remove('bacopy-assist-tile', 'bacopy-assist-now', 'bacopy-assist-ready');
      tile.style.removeProperty('--bc-ring');
      tile.style.removeProperty('--bc-glow');
      tile.style.removeProperty('--bc-bg');
      for (const old of tile.querySelectorAll(':scope > .bacopy-assist-badge')) old.remove();
      let ring = 'rgba(255,204,0,.94)';
      let glow = 'rgba(255,204,0,.45)';
      let bg = 'rgba(25,18,0,.70)';
      if (status === 'NOW' && side === 'P') {
        ring = 'rgba(45,145,255,.98)';
        glow = 'rgba(45,145,255,.62)';
        bg = 'rgba(0,38,86,.72)';
      } else if (status === 'NOW' && side === 'B') {
        ring = 'rgba(255,58,92,.98)';
        glow = 'rgba(255,58,92,.62)';
        bg = 'rgba(82,0,18,.72)';
      }
      tile.style.setProperty('--bc-ring', ring);
      tile.style.setProperty('--bc-glow', glow);
      tile.style.setProperty('--bc-bg', bg);
      tile.classList.add('bacopy-assist-tile');
      tile.classList.add(status === 'NOW' ? 'bacopy-assist-now' : 'bacopy-assist-ready');
      const badge = document.createElement('div');
      badge.className = 'bacopy-assist-badge';
      const sideLabel = side === 'P' ? 'PLAYER' : side === 'B' ? 'BANKER' : 'READY';
      const amt = Number.isFinite(amount) && amount > 0 ? '$' + amount.toFixed(0) : '';
      badge.textContent = status === 'NOW' ? `${sideLabel} ${amt}`.trim() : `READY ${sideLabel}`;
      tile.appendChild(badge);
      const nowTtl = Math.max(30000, Number((args && args.nowTtlMs) || 65000));
      const ttl = status === 'NOW' ? nowTtl : 90000;
      const token = String(Date.now()) + ':' + Math.random();
      tile.setAttribute('data-bacopy-assist-token', token);
      tile.setAttribute('data-bacopy-assist-status', status);
      tile.setAttribute('data-bacopy-assist-until', String(Date.now() + ttl));
      installAssistScrollLock(tile, sc, ttl);
      window.setTimeout(() => {
        try {
          if (tile.getAttribute('data-bacopy-assist-token') !== token) return;
          tile.classList.remove('bacopy-assist-tile', 'bacopy-assist-now', 'bacopy-assist-ready');
          tile.style.removeProperty('--bc-ring');
          tile.style.removeProperty('--bc-glow');
          tile.style.removeProperty('--bc-bg');
          tile.removeAttribute('data-bacopy-assist-token');
          tile.removeAttribute('data-bacopy-assist-status');
          tile.removeAttribute('data-bacopy-assist-until');
          for (const old of tile.querySelectorAll(':scope > .bacopy-assist-badge')) old.remove();
        } catch(_) {}
      }, ttl);
      return true;
    } catch(_) {
      return false;
    }
  }
  function diagOf(el) {
    const out = [];
    let cur = el;
    for (let i = 0; i < 6 && cur; i++) {
      try {
        const text = String(cur.innerText || cur.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 180);
        const buttons = Array.from(cur.querySelectorAll('button, [role="button"]')).slice(0, 8)
          .map((b) => String(b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40));
        out.push({
          depth: i,
          tag: String(cur.tagName || ''),
          cls: String(cur.className || '').slice(0, 80),
          text: text,
          buttons: buttons,
        });
      } catch(_) {}
      cur = cur.parentElement;
    }
    return out;
  }
  function findTarget() {
    if (qpid) {
      try {
        const tile = document.getElementById('TileHeight-' + qpid);
        const assistStatus = String((args && args.assistStatus) || '').toUpperCase();
        if (tile && (assistStatus || hasBetCells(tile))) {
          return { el: clickableAncestor(tile), idx: 0, total: 1 };
        }
      } catch(_) {}
    }
    const sels = '[id^="TileHeight-"], [role="button"], button, a, div[role="button"], [data-testid*="table"], [data-testid*="lobby"]';
    const nodes = document.querySelectorAll(sels);
    let idx = 0;
    for (const el of nodes) {
      try {
        if (qpid) {
          let qt = null;
          if (containsQpid(el.getAttribute && el.getAttribute('data-qpid'))) qt = qpidTarget(el);
          if (!qt && containsQpid(el.getAttribute && el.getAttribute('data-table-id'))) qt = qpidTarget(el);
          if (!qt && containsQpid(el.getAttribute && el.getAttribute('data-tableid'))) qt = qpidTarget(el);
          if (!qt && containsQpid(el.getAttribute && el.getAttribute('id'))) qt = qpidTarget(el);
          if (!qt && containsQpid(el.getAttribute && el.getAttribute('href'))) qt = qpidTarget(el);
          if (!qt && containsQpid(el.getAttribute && el.getAttribute('src'))) qt = qpidTarget(el);
          if (!qt && containsQpid(el.getAttribute && el.getAttribute('style'))) qt = qpidTarget(el);
          try {
            const cs = getComputedStyle(el);
            if (!qt && containsQpid(cs && cs.backgroundImage)) qt = qpidTarget(el);
          } catch(_) {}
          try {
            if (!qt && containsQpid(el.innerHTML || '')) qt = qpidTarget(el);
          } catch(_) {}
          if (qt) return { el: qt, idx, total: nodes.length };
          if (candNorm.length && isCandidateText(textOf(el))) {
            return { el: clickableAncestor(el), idx, total: nodes.length, textFallback: true };
          }
          idx += 1;
          continue;
        }
        if (isCandidateText(textOf(el))) return { el: clickableAncestor(el), idx, total: nodes.length };
      } catch(_) {}
      idx += 1;
    }
    return { el: null, idx: -1, total: nodes.length };
  }
  function currentAssistTarget() {
    try {
      if (qpid) {
        const exact = document.getElementById('TileHeight-' + qpid);
        if (exact) return exact;
      }
    } catch(_) {}
    try {
      const nodes = document.querySelectorAll('.bacopy-assist-now, .bacopy-assist-tile');
      for (const node of nodes) {
        const id = String((node && node.id) || '');
        if (!id.startsWith('TileHeight-')) continue;
        if (!qpid || id === ('TileHeight-' + qpid)) return node;
      }
    } catch(_) {}
    return null;
  }
  function pickScroller() {
    // Prefer the closest scrollable ancestor of a real TileHeight- tile.
    // The multi-baccarat lobby contains several nested scrollable wrappers,
    // and the largest one (picked by the previous "biggest delta" heuristic)
    // is not necessarily the virtualizer's parent. Scrolling the wrong element
    // moves nothing in the virtual list, the off-screen tiles never mount,
    // and findTarget keeps returning null until the deadline expires.
    try {
      const samples = document.querySelectorAll('[id^="TileHeight-"]');
      for (const sample of samples) {
        let cur = sample.parentElement;
        while (cur && cur !== document.body) {
          try {
            const cs = getComputedStyle(cur);
            if (/(auto|scroll)/.test(cs.overflowY || '')) {
              const delta = (cur.scrollHeight || 0) - (cur.clientHeight || 0);
              if (delta > 50) return cur;
            }
          } catch(_) {}
          cur = cur.parentElement;
        }
        if (sample) break;
      }
    } catch(_) {}
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
  function snapshotOuterScroll() {
    const se = document.scrollingElement || document.documentElement || document.body;
    return {
      x: Number(window.scrollX || 0),
      y: Number(window.scrollY || 0),
      seTop: Number((se && se.scrollTop) || 0),
      bodyTop: Number((document.body && document.body.scrollTop) || 0),
      docTop: Number((document.documentElement && document.documentElement.scrollTop) || 0),
    };
  }
  function restoreOuterScroll(s) {
    if (!s) return;
    try {
      if (Math.abs((window.scrollX || 0) - Number(s.x || 0)) > 1 ||
          Math.abs((window.scrollY || 0) - Number(s.y || 0)) > 1) {
        window.scrollTo(Number(s.x || 0), Number(s.y || 0));
      }
    } catch(_) {}
    try {
      if (document.scrollingElement) document.scrollingElement.scrollTop = Number(s.seTop || 0);
    } catch(_) {}
    try { if (document.body) document.body.scrollTop = Number(s.bodyTop || 0); } catch(_) {}
    try { if (document.documentElement) document.documentElement.scrollTop = Number(s.docTop || 0); } catch(_) {}
  }
  function mountedQpidSample(limit) {
    const out = [];
    try {
      const tiles = document.querySelectorAll('[id^="TileHeight-"]');
      const max = Math.min(tiles.length, Math.max(1, Number(limit) || 8));
      for (let i = 0; i < max; i++) {
        const id = String((tiles[i] && tiles[i].id) || '');
        if (id) out.push(id.replace(/^TileHeight-/, ''));
      }
    } catch(_) {}
    return out;
  }
  function scrollMeta(sc) {
    if (!sc) return {scrollTop: 0, scrollHeight: 0, clientHeight: 0, scrollRatio: 0};
    const top = Number(sc.scrollTop || 0);
    const height = Number(sc.scrollHeight || 0);
    const client = Number(sc.clientHeight || 0);
    const span = Math.max(1, height - client);
    return {
      scrollTop: Math.round(top),
      scrollHeight: Math.round(height),
      clientHeight: Math.round(client),
      scrollRatio: Math.max(0, Math.min(1, top / span)),
    };
  }
  function scrollStep(sc, down=true) {
    if (!sc) return;
    const outer = snapshotOuterScroll();
    const delta = Math.max(240, (sc.clientHeight || 500) * 0.8) * (down ? 1 : -1);
    try { sc.scrollTop = Math.max(0, Math.min((sc.scrollHeight || 99999), (sc.scrollTop || 0) + delta)); } catch(_) {}
    try {
      const r = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
      const ev = new WheelEvent('wheel', {deltaY: delta, bubbles:false, cancelable:true, composed:false, clientX:r.left+r.width/2, clientY:r.top+r.height/2});
      sc.dispatchEvent(ev);
    } catch(_) {}
    restoreOuterScroll(outer);
  }
  function setScrollTop(sc, top) {
    if (!sc) return;
    const outer = snapshotOuterScroll();
    try {
      const span = Math.max(0, (sc.scrollHeight || 0) - (sc.clientHeight || 0));
      sc.scrollTop = Math.max(0, Math.min(span, Math.floor(top)));
    } catch(_) {}
    try {
      const r = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
      const ev = new WheelEvent('wheel', {deltaY: 1, bubbles:false, cancelable:true, composed:false, clientX:r.left+r.width/2, clientY:r.top+r.height/2});
      sc.dispatchEvent(ev);
    } catch(_) {}
    restoreOuterScroll(outer);
  }
  function scanPositions(sc, steps) {
    if (!sc) return [0];
    let span = 0;
    let cur = 0;
    try {
      span = Math.max(0, (sc.scrollHeight || 0) - (sc.clientHeight || 0));
      cur = Math.max(0, Math.min(span, Number(sc.scrollTop || 0)));
    } catch(_) {}
    if (span <= 0) return [0];
    const n = Math.max(4, Number(steps) || 16);
    const positions = [];
    const add = (v) => {
      const x = Math.max(0, Math.min(span, Math.floor(v)));
      if (!positions.some((p) => Math.abs(p - x) < 8)) positions.push(x);
    };
    add(cur);
    // Sweep from the current area to the bottom first; preposition normally
    // follows the latest visible lobby area, so this keeps nearby targets fast.
    const startRatio = cur / Math.max(1, span);
    for (let i = 1; i <= n; i++) {
      const ratio = startRatio + (1 - startRatio) * (i / n);
      add(span * ratio);
    }
    // Then cover the top-to-current range. This makes qpid search deterministic
    // for virtualized lists whose target tile is not mounted yet.
    for (let i = 0; i <= n; i++) {
      const ratio = startRatio * (i / n);
      add(span * ratio);
    }
    add(span);
    add(0);
    return positions;
  }
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const initScroller = pickScroller();
  if (initScroller && hintScrollTop >= 0) {
    try {
      const span = Math.max(0, (initScroller.scrollHeight || 0) - (initScroller.clientHeight || 0));
      initScroller.scrollTop = Math.max(0, Math.min(span, Math.floor(hintScrollTop)));
    } catch(_) {}
  } else if (initScroller && hintScrollRatio >= 0) {
    try {
      const ratio = Math.max(0, Math.min(1, hintScrollRatio));
      const span = Math.max(0, (initScroller.scrollHeight || 0) - (initScroller.clientHeight || 0));
      initScroller.scrollTop = Math.max(0, Math.min(span, Math.floor(span * ratio)));
    } catch(_) {}
  } else if (initScroller && hintIndex >= 0 && hintTotal > 0) {
    try {
      const ratio = Math.max(0, Math.min(1, hintIndex / Math.max(1, hintTotal)));
      const span = Math.max(0, (initScroller.scrollHeight || 0) - (initScroller.clientHeight || 0));
      initScroller.scrollTop = Math.max(0, Math.min(span, Math.floor(span * ratio)));
    } catch(_) {}
  }

  const cachedScroller = initScroller || pickScroller();
  if (holdOnly) {
    const target = currentAssistTarget();
    if (target) {
      // Only re-center when the tile actually drifted out of view. While it is
      // visible, do NOT touch scroll — this is what made the "outer" jitter on
      // every hold cycle. Tunable slack via args.holdVisiblePad.
      const pad = Number.isFinite(Number(args && args.holdVisiblePad)) ? Number(args.holdVisiblePad) : 24;
      if (!isTileVisibleEnough(target, cachedScroller, pad)) {
        centerInScroller(target, cachedScroller, 8);
      }
      await sleep(40);
      const assisted = applyAssistOverlay(target, cachedScroller);
      return {
        ok:true, found:true, clicked:false, assisted: assisted,
        matchIndex: 0, totalNodes: 1, scroll: scrollMeta(cachedScroller),
        diag: diagOf(target), scanMode: 'hold_only'
      };
    }
    return {
      ok:true, found:false, clicked:false, matchIndex:-1, totalNodes:0,
      scroll: scrollMeta(cachedScroller), mountedQpids: mountedQpidSample(8),
      scanMode: 'hold_only', reason: 'target_not_mounted_no_scan'
    };
  }
  if (qpid) {
    const positions = scanPositions(cachedScroller, maxScroll);
    for (let i = 0; i < positions.length && Date.now() < deadline; i++) {
      setScrollTop(cachedScroller, positions[i]);
      await sleep(90);
      const t = findTarget();
      if (t && t.el) {
        centerInScroller(t.el, cachedScroller);
        await sleep(70);
        const assisted = applyAssistOverlay(t.el, cachedScroller);
        if (click) clickEl(t.el);
        return {
          ok:true, found:true, clicked: !!click,
          matchIndex: Number(t.idx), totalNodes: Number(t.total),
          scroll: scrollMeta(cachedScroller), diag: diagOf(t.el),
          scanMode: 'absolute_qpid', scanStep: i, scanTotal: positions.length,
          assisted: assisted
        };
      }
    }
  } else {
    for (let i = 0; i < maxScroll && Date.now() < deadline; i++) {
      const t = findTarget();
      if (t && t.el) {
        centerInScroller(t.el, cachedScroller);
        await sleep(70);
        const assisted = applyAssistOverlay(t.el, cachedScroller);
        if (click) clickEl(t.el);
        return { ok:true, found:true, clicked: !!click, assisted: assisted, matchIndex: Number(t.idx), totalNodes: Number(t.total), scroll: scrollMeta(cachedScroller), diag: diagOf(t.el) };
      }
      scrollStep(cachedScroller, true);
      await sleep(70);
    }
    for (let i = 0; i < maxScroll && Date.now() < deadline; i++) {
      const t = findTarget();
      if (t && t.el) {
        centerInScroller(t.el, cachedScroller);
        await sleep(70);
        const assisted = applyAssistOverlay(t.el, cachedScroller);
        if (click) clickEl(t.el);
        return { ok:true, found:true, clicked: !!click, assisted: assisted, matchIndex: Number(t.idx), totalNodes: Number(t.total), scroll: scrollMeta(cachedScroller), diag: diagOf(t.el) };
      }
      scrollStep(cachedScroller, false);
      await sleep(70);
    }
  }
  for (let i = 0; i < Math.max(3, Math.floor(maxScroll / 4)) && Date.now() < deadline; i++) {
    const t = findTarget();
    if (t && t.el) {
      centerInScroller(t.el, cachedScroller);
      await sleep(70);
      const assisted = applyAssistOverlay(t.el, cachedScroller);
      if (click) clickEl(t.el);
      return { ok:true, found:true, clicked: !!click, assisted: assisted, matchIndex: Number(t.idx), totalNodes: Number(t.total), scroll: scrollMeta(cachedScroller), diag: diagOf(t.el) };
    }
    scrollStep(cachedScroller, false);
    await sleep(70);
  }
  const timedOut = Date.now() >= deadline;
  const finalTarget = findTarget();
  if (finalTarget && finalTarget.el) {
    centerInScroller(finalTarget.el, cachedScroller);
    await sleep(70);
    const assisted = applyAssistOverlay(finalTarget.el, cachedScroller);
    if (click) clickEl(finalTarget.el);
    return { ok:true, found:true, clicked: !!click, assisted: assisted, matchIndex: Number(finalTarget.idx), totalNodes: Number(finalTarget.total), scroll: scrollMeta(cachedScroller), diag: diagOf(finalTarget.el), reason: timedOut ? 'deadline_late_match' : undefined };
  }
  return { ok:true, found:false, clicked:false, matchIndex:-1, totalNodes:Number(finalTarget.total || 0), scroll: scrollMeta(cachedScroller), mountedQpids: mountedQpidSample(8), reason: timedOut ? 'deadline' : undefined };
}
"""

_MULTI_TILE_SNAPSHOT_JS = r"""
() => {
  const tiles = [];
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  function hasClass(el, part) {
    try { return String(el.className || '').includes(part); } catch(_) { return false; }
  }
  for (const el of document.querySelectorAll('[id^="TileHeight-"]')) {
    try {
      const id = String(el.id || '');
      const qpid = id.replace(/^TileHeight-/, '');
      if (!qpid) continue;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 40 || r.height < 40) continue;
      const visible = r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
      const buttons = Array.from(el.querySelectorAll('.ym_qA, [role="button"], button')).filter((b) => {
        try {
          const br = b.getBoundingClientRect();
          return br && br.width > 8 && br.height > 8;
        } catch(_) { return false; }
      });
      const hasP = buttons.some((b) => hasClass(b, 'ym_yp') || hasClass(b, 'ym_yP') || /player|プレイヤー/i.test(String(b.textContent || '')));
      const hasB = buttons.some((b) => hasClass(b, 'ym_yr') || hasClass(b, 'ym_yQ') || /banker|バンカー/i.test(String(b.textContent || '')));
      tiles.push({
        qpid, visible,
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height),
        cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2),
        hasP, hasB,
        text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
      });
    } catch(_) {}
  }
  return {ok:true, ts:Date.now(), viewport:{w:vw,h:vh}, tiles};
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

# READ-ONLY DOM probe to identify a reliable ordered-result source from the
# betting page's OWN multibaccarat tile DOM (single session, no 2nd Pragmatic
# page). For each of the first few tiles it reports: innerText (aggregate
# counters), whether the roadmap is canvas/svg/div-rendered, and a structural
# sample of small "cell-sized" elements (candidate road circles) with their
# computed colours + position so we can decide if/how to parse an ordered
# P/B/T sequence. NO clicks, NO state change. Used by _maybe_dom_result_probe.
_DOM_RESULT_PROBE_JS = r"""
() => {
  const tiles = Array.from(document.querySelectorAll('[id^="TileHeight-"]')).slice(0, 3);
  return tiles.map((t) => {
    const qpid = String(t.id || '').replace(/^TileHeight-/, '');
    const text = String(t.innerText || t.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 220);
    let canvases = 0, svgs = 0;
    try { canvases = t.querySelectorAll('canvas').length; } catch (_) {}
    try { svgs = t.querySelectorAll('svg, svg *').length; } catch (_) {}
    const cells = [];
    let all = [];
    try { all = Array.from(t.querySelectorAll('*')); } catch (_) { all = []; }
    for (const e of all) {
      let r; try { r = e.getBoundingClientRect(); } catch (_) { continue; }
      if (!r || r.width < 3 || r.width > 24 || r.height < 3 || r.height > 24) continue;
      let bg = '', bc = '', col = '';
      try { const cs = getComputedStyle(e); bg = cs.backgroundColor || ''; bc = cs.borderColor || ''; col = cs.color || ''; } catch (_) {}
      cells.push({
        tag: e.tagName,
        cls: (e.getAttribute && (e.getAttribute('class') || '')) || '',
        bg: bg, bc: bc, col: col,
        title: (e.getAttribute && (e.getAttribute('title') || e.getAttribute('aria-label') || e.getAttribute('data-type') || e.getAttribute('data-result') || '')) || '',
        txt: String(e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 6),
        x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
      });
    }
    return { qpid: qpid, text: text, canvases: canvases, svgs: svgs, cellCount: cells.length, cells: cells.slice(0, 80) };
  });
}
"""

# 「再接続しています」テキストを全フレームのテキストノードから検索する。
# 見つかった場合は true を返す。
_RECONNECTING_DETECT_JS = r"""
() => {
  const NEEDLES = [
    '再接続', '再接続しています', '接続を回復', '接続が失われ',
    'reconnecting', 'connection lost', 'restoring connection'
  ];
  function hasText(doc) {
    try {
      const walker = doc.createTreeWalker(doc.body || doc, 0x4 /* NodeFilter.SHOW_TEXT */);
      let node;
      while ((node = walker.nextNode())) {
        const t = String(node.nodeValue || '').toLowerCase();
        if (t && NEEDLES.some((k) => t.includes(String(k).toLowerCase()))) return true;
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

_AUTO_RECOVER_IDLE_JS = r"""
() => {
  const dialogNeedles = [
    '非アクティブ', '操作がありません', '接続が失われ', '再接続',
    '再接続しています', '接続を回復', '長時間セッション', '長時間操作',
    'しばらく操作', 'セッションがありません', 'ログアウト',
    '他の場所でセッション', 'inactivity', 'are you still there',
    'session elsewhere', 'reconnecting', 'connection lost',
    'カスタマーサポート', 'カスタマーサポートに連絡してください', 'カスタマーサポートへ連絡してください',
    'カスタマーサポートにお問い合わせ', 'カスタマーサポートへお問い合わせ',
    'お問い合わせください', 'customer support',
    '連絡してください', 'サポートに連絡', 'please contact', 'contact support', 'technical issue',
    'something went wrong', 'try again later', 'エラーが発生', 'error has occurred',
    'セッションが終了', 'session has ended', 'session expired', 'no active session'
  ];
  const buttonNeedles = [
    'continue', 'stay', 'close', 'remain', 'dismiss', 'ok', 'okay',
    'got it', 'understand', 'retry', 'reload', 'refresh', 'confirm',
    '続行', '閉じる', 'ここに残る', '再開', '戻る', '再試行', '更新',
    'はい', '確認', '了解', 'オーケー', 'ゲームに戻る', 'ロビーに戻る', '確認しました'
  ];
  const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 20 && r.height > 12 && cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity || 1) > 0.02;
    } catch(_) { return false; }
  };
  const allElements = () => {
    const out = [];
    const walk = (root) => {
      try {
        for (const el of root.querySelectorAll('*')) {
          out.push(el);
          if (el.shadowRoot) walk(el.shadowRoot);
        }
      } catch(_) {}
    };
    walk(document);
    return out;
  };
  const click = (el) => {
    try { el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
    try {
      const r = el.getBoundingClientRect();
      const x = r.left + Math.max(1, r.width / 2);
      const y = r.top + Math.max(1, r.height / 2);
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y, view:window}));
      }
      return true;
    } catch(_) {}
    try { el.click(); return true; } catch(_) {}
    return false;
  };
  const clickAt = (x, y) => {
    try {
      const el = document.elementFromPoint(x, y);
      if (!el || !visible(el)) return false;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y, view:window}));
      }
      return true;
    } catch(_) { return false; }
  };
  const dialogs = [];
  const dialogSelector = [
    '[role="dialog"]', '[aria-modal="true"]', '.modal', '.dialog', '.popup',
    '[data-testid*="modal"]', '[data-testid*="dialog"]',
    '[class*="modal"]', '[class*="dialog"]', '[class*="popup"]', '[class*="overlay"]'
  ].join(',');
  for (const r of document.querySelectorAll(dialogSelector)) {
    try {
      if (!visible(r)) continue;
      const t = norm((r.innerText || r.textContent || '').slice(0, 500));
      if (t && dialogNeedles.some((k) => t.includes(k))) dialogs.push(r);
    } catch(_) {}
  }
  for (const r of allElements()) {
    try {
      if (!visible(r)) continue;
      const rect = r.getBoundingClientRect();
      const cs = getComputedStyle(r);
      const z = parseInt(cs.zIndex || '0', 10) || 0;
      const fixedLike = cs.position === 'fixed' || cs.position === 'sticky' || (cs.position === 'absolute' && z >= 10);
      if (!fixedLike && z < 100) continue;
      if (rect.width < 160 || rect.height < 60) continue;
      if (rect.bottom < 0 || rect.right < 0 || rect.top > window.innerHeight || rect.left > window.innerWidth) continue;
      const t = norm((r.innerText || r.textContent || '').slice(0, 800));
      if (t && dialogNeedles.some((k) => t.includes(k)) && !dialogs.includes(r)) dialogs.push(r);
    } catch(_) {}
  }
  if (!dialogs.length) {
    try {
      const bodyText = norm((document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 1200));
      if (bodyText && dialogNeedles.some((k) => bodyText.includes(k))) dialogs.push(document.body);
    } catch(_) {}
  }
  let clicked = 0;
  let fallbackClicked = 0;
  for (const d of dialogs) {
    const candidates = Array.from(d.querySelectorAll(
      'button, input[type="button"], input[type="submit"], [role="button"], [aria-label], [data-testid*="close"], [data-testid*="ok"], [class*="close"], [class*="button"], [class*="Button"], a, div[tabindex], span[tabindex]'
    ));
    for (const el of candidates) {
      try {
        if (!visible(el)) continue;
        const t = norm((
          el.innerText || el.textContent || el.getAttribute('aria-label') ||
          el.getAttribute('value') || el.getAttribute('title') ||
          el.getAttribute('data-testid') || el.className || ''
        ).slice(0, 160));
        const isOkLike = t === 'ok' || t === 'okay' || t === '確認' || t === '了解' || t === '閉じる';
        if (t && (isOkLike || buttonNeedles.some((k) => t.includes(k))) && click(el)) clicked += 1;
        if (clicked >= 3) break;
      } catch(_) {}
    }
    if (!clicked) {
      for (const el of candidates) {
        try {
          if (!visible(el)) continue;
          const t = norm((el.innerText || el.textContent || el.getAttribute('aria-label') || '').slice(0, 160));
          const shortButton = t.length <= 18 || /^(ok|okay|確認|了解|閉じる)$/i.test(t);
          if (!shortButton && (t.includes('support') || t.includes('サポート') || t.includes('customer'))) continue;
          if (click(el)) { fallbackClicked += 1; break; }
        } catch(_) {}
      }
    }
    if (!clicked && !fallbackClicked) {
      try {
        const r = d === document.body
          ? {left:0, top:0, width:window.innerWidth, height:window.innerHeight}
          : d.getBoundingClientRect();
        const points = [
          [r.left + r.width / 2, r.top + r.height * 0.78],
          [r.left + r.width / 2, r.top + r.height * 0.86],
          [r.left + r.width * 0.72, r.top + r.height * 0.78],
          [r.left + r.width * 0.28, r.top + r.height * 0.78],
        ];
        for (const [x, y] of points) {
          if (x > 0 && y > 0 && x < window.innerWidth && y < window.innerHeight && clickAt(x, y)) {
            fallbackClicked += 1;
            break;
          }
        }
      } catch(_) {}
    }
    if (clicked >= 3) break;
  }
  if (dialogs.length && !clicked && !fallbackClicked) {
    try {
      document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', bubbles:true}));
      document.dispatchEvent(new KeyboardEvent('keyup', {key:'Escape', code:'Escape', bubbles:true}));
    } catch(_) {}
  }
  return { ok:true, clicked, fallbackClicked, dialogs:dialogs.length };
}
"""

_DISMISS_BETSLIP_SETTINGS_JS = r"""
() => {
  const dialogNeedles = ['ベットスリップ設定', 'bet slip settings', 'betslip settings'];
  const closeNeedles = ['閉じる', 'キャンセル', 'cancel', 'close', 'skip', 'not now', '後で'];
  const primaryNeedles = ['ok', '確認', 'continue', '続行'];
  const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 20 && r.height > 12 && cs.visibility !== 'hidden' && cs.display !== 'none';
    } catch(_) { return false; }
  };
  const click = (el) => { try { el.click(); return true; } catch(_) { return false; } };
  const dialogs = [];
  for (const r of document.querySelectorAll('[role="dialog"], .modal, .dialog, .popup, [data-testid*="modal"], [class*="modal"]')) {
    try {
      if (!visible(r)) continue;
      const t = norm((r.innerText || r.textContent || '').slice(0, 600));
      if (t && dialogNeedles.some((k) => t.includes(k))) dialogs.push(r);
    } catch(_) {}
  }
  let closed = 0;
  for (const d of dialogs) {
    const buttons = d.querySelectorAll('button, [role="button"], [aria-label], [data-testid*="close"]');
    let done = false;
    for (const b of buttons) {
      const t = norm(b.innerText || b.textContent || b.getAttribute('aria-label') || '');
      if (visible(b) && closeNeedles.some((k) => t.includes(k)) && click(b)) { closed += 1; done = true; break; }
    }
    if (!done) {
      for (const b of buttons) {
        const t = norm(b.innerText || b.textContent || b.getAttribute('aria-label') || '');
        if (visible(b) && primaryNeedles.some((k) => t.includes(k)) && click(b)) { closed += 1; break; }
      }
    }
  }
  return { ok:true, found:dialogs.length, closed };
}
"""

# ── ヘルパー ──────────────────────────────────────────────────────────

def _side_to_bc(side: str) -> str:
    return "B" if side.upper() in ("B", "BANKER") else "P"


def _build_lpbet_xml(*, table_id: str, game_id: str, user_id: str,
                     bc: str, amount: float) -> str:
    ck = str(int(time.time() * 1000))
    amt = str(int(amount)) if float(amount).is_integer() else str(amount)
    # game module 名: マルチエリア(マルチテーブル)では Pragmatic client が
    # gm="mtb_desktop" を送る(実機キャプチャ 2026-06-06 で確認)。単一卓の
    # "baccarat_desktop" を流用すると Stake がBETを受理せず残高が動かない
    # (= 過去 WS transport が封印された真因)。env で上書き可。
    gm = (os.getenv("BACOPY_LPBET_GM", "") or "mtb_desktop").strip() or "mtb_desktop"
    # bc は数値コード(実機キャプチャ 2026-06-06): Player=0 / Banker=1。
    # 単一卓用の文字 "B"/"P" を流用すると Stake が弾く or 別の側へ着弾するため
    # ここで数値へ変換する。既に数値なら素通し。
    _bc = str(bc).strip().upper()
    if _bc in ("B", "BANKER", "1"):
        bc_num = "1"
    elif _bc in ("P", "PLAYER", "0"):
        bc_num = "0"
    else:
        bc_num = _bc
    # 実機の lpbet 開きタグは ck="..." 直後に '>'(空白なし)。バイト一致させる。
    return (
        f'<command channel="table-{table_id}">'
        f'<lpbet gm="{gm}" gId="{game_id}" uId="{user_id}" ck="{ck}">'
        f'<bet amt="{amt}" bc="{bc_num}" ck="{ck}"/>'
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
        self._last_session_ended_check_at: float = 0.0
        self._last_session_elsewhere_check_at: float = 0.0
        self._last_reconnecting_check_at: float = 0.0
        self._last_idle_recover_check_at: float = 0.0
        self._last_betslip_check_at: float = 0.0
        self._last_manual_assist_recover_at: float = 0.0
        self._last_lobby_recover_at: float = 0.0
        self._reconnecting_first_at: float = 0.0   # 再接続しています 最初の検出時刻
        self._manual_assist_watch_until: float = 0.0
        self._manual_assist_watch_target: str = ""
        self._user_id: str = ""              # userId (for lpbet)
        self._game_id: str = ""              # current game id
        self._phase: str = "waiting"         # waiting | ready | betting

        # betsopen 状態
        self._bets_open_game_id: str = ""
        self._bets_closed_game_id: str = ""
        self._last_bets_open_at: float = 0.0
        # セッション自動復旧: contact-support/長時間セッションモーダルを閉じた後、または
        # betsopen フィードが長時間途絶した時に、ロビーへ自動再入場してセッションを復旧する
        # (手動でロビーに行く手間を無くす)。tick の安全な文脈で実行。
        self._session_recover_pending: bool = False
        self._last_lobby_recover_at: float = 0.0

        # BET 予約
        self._pending_bet: dict | None = None
        self._switch_request: dict | None = None
        self._switch_in_progress: bool = False
        self._active_switch_request: dict | None = None
        self._switch_target_table_id: str = ""
        self._multi_lobby_mode: bool = os.getenv("BACOPY_MULTI_LOBBY_MODE", "1") != "0"
        self._multi_diagnostic_only: bool = (
            os.getenv("BACOPY_MULTI_DIAGNOSTIC_ONLY", "0") == "1"
        )
        self._multi_bet_transport: str = (
            os.getenv("BACOPY_MULTI_BET_TRANSPORT", "click").strip().lower() or "click"
        )
        if self._multi_bet_transport not in ("ws", "click"):
            self._multi_bet_transport = "click"
        if (
            self._multi_bet_transport == "ws"
            and os.getenv("BACOPY_ALLOW_WS_BET_TRANSPORT", "0").strip() != "1"
        ):
            logger.warning(
                "[EXEC-SETUP] BACOPY_MULTI_BET_TRANSPORT=ws ignored; "
                "forcing click transport because WS send did not prove real Stake balance acceptance"
            )
            self._multi_bet_transport = "click"
        browser_mode_now = (
            os.getenv("BACOPY_BROWSER", "")
            or os.getenv("BACOPY_DUAL_LINE_BROWSER", "")
            or ""
        ).strip().lower()
        if (
            self._multi_bet_transport == "ws"
            and browser_mode_now in ("chrome_attach", "chrome-cdp", "cdp")
            and os.getenv("BACOPY_ENABLE_WS_REAL_BET", "0").strip() != "1"
        ):
            logger.warning(
                "[EXEC-SETUP] chrome_attach WS bet transport disabled; "
                "forcing click transport. Set BACOPY_ENABLE_WS_REAL_BET=1 only "
                "after WS lpbet proves real Stake acceptance."
            )
            self._multi_bet_transport = "click"
        self._diag_probe_qpid: str = os.getenv("BACOPY_MULTI_DIAG_PROBE_QPID", "").strip()
        self._diag_probe_name: str = os.getenv("BACOPY_MULTI_DIAG_PROBE_NAME", "").strip()
        self._diag_probe_side: str = os.getenv("BACOPY_MULTI_DIAG_PROBE_SIDE", "B").strip().upper()
        if self._diag_probe_side not in ("P", "B"):
            self._diag_probe_side = "B"
        self._diag_probe_queued: bool = False
        self._diag_prepared_side: str = self._diag_probe_side
        self._diag_last_open_probe_gid: str = ""
        self._prepared_table_id: str = ""
        self._prepared_at: float = 0.0

        # lpbet WS 確認（JS click 後に Pragmatic game client が送る <lpbet> メッセージ）
        self._last_lpbet_gid: str = ""
        self._last_lpbet_at: float = 0.0
        # 卓(game)ごとの送出 <lpbet> 累計回数。多チップBETで「計画枚数だけ着弾したか」を
        # 検証し、不足分だけ再クリックするために使う（1チップ着弾＝1 lpbet）。
        self._lpbet_count_by_gid: dict[str, int] = {}
        self._last_bet_modal_recover_at: float = 0.0
        self._table_states: dict[str, dict[str, Any]] = {}
        self._game_to_table_id: dict[str, str] = {}
        self._saw_per_table_betsopen: bool = False
        self._last_multi_area_ensure_at: float = 0.0
        self._multi_area_ready: bool = False
        self._last_multi_dom_recover_at: float = 0.0
        self._last_multi_fallback_join_at: float = 0.0
        # マルチプレイ画面から脱落(tab NOT FOUND かつ multi_ws=False)した状態が
        # 続くと、defer/fallback だけでは復帰できず NOW 昇格不能なデッドロックに
        # 陥る(2026-05-31 実機: 12:36 脱落→以降 focusmiss 連発で実BETゼロ)。
        # 連続 NOT FOUND を数えて閾値を超えたらページreload復旧を強制するための状態。
        self._multi_tab_missing_since: float = 0.0   # 最初に NOT FOUND を観測した時刻(0=未観測)
        self._multi_tab_missing_count: int = 0        # 連続 NOT FOUND 回数
        self._last_multi_deadlock_recover_at: float = 0.0
        self._sent_bet_ids: set[str] = set()
        self._confirmed_bets: dict[str, dict[str, Any]] = {}
        self._failed_bet_ids: dict[str, dict[str, Any]] = {}
        self._bet_send_in_progress: bool = False
        self._last_bet_sent_at: float = 0.0
        self._stake_balance_by_currency: dict[str, float] = {}
        self._stake_balance_delta_by_currency: dict[str, float] = {}
        self._last_stake_balance_at: float = 0.0
        self._last_game_bet_confirm: dict[str, Any] = {}
        self._last_game_bet_confirm_at: float = 0.0
        self._chip_plan_cache: dict[tuple[tuple[float, ...], int], list[float]] = {}
        self._chip_plan_prewarmed_keys: set[tuple[float, ...]] = set()
        self._preselected_chip: dict[str, Any] = {}
        self._visible_bet_hold: dict[str, Any] = {}
        self._last_visible_bet_center_at: float = 0.0
        self._active_now_bet_hold: dict[str, Any] = {}
        self._last_active_now_bet_center_at: float = 0.0
        self._assist_focus_hold: dict[str, Any] = {}
        self._last_assist_focus_center_at: float = 0.0
        # 機能①(HOLD): この QPID を固定中は他卓へ focus を移さず中央保持を続ける。
        self._pinned_qpid: str = ""
        # 機能①拡張(HOLD): 枠が無くてもスクロール凍結(卓切替/再センタリングを全停止)。
        self._scroll_frozen: bool = False
        # Bot-owned NOW lock mirror. Source of truth lives in dual_line_pragmatic_bot;
        # the executor only reads it to drop competing focus/auto-fire while a NOW
        # decision is locked to a single table.
        self._bot_now_lock: dict[str, Any] = {}
        # Bot-registered callback fired when the NOW-locked hand ends (detected via
        # betsopen gameId advance). Speed/multiplay tables never deliver a winner-
        # bearing hand to the bot's _on_new_hand, so betsopen is the only reliable
        # hand-boundary signal for auto-clearing the red/blue assist overlay.
        self._now_lock_release_cb = None
        self._last_lobby_scroll_probe_at: float = 0.0
        self._multi_tile_snapshot: dict[str, Any] = {}
        self._multi_tile_snapshot_at: float = 0.0
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
        self._preposition_focus_block_until: dict[str, float] = {}
        self._last_click_bet_error: dict[str, Any] = {}
        # Playwright route_web_socket プロキシ（multi-lobby BET 送信用）
        self._ws_proxy_server: Any = None      # WebSocketRoute server side
        self._ws_proxy_route: Any = None       # WebSocketRoute client side
        self._ws_proxy_servers: dict[str, Any] = {}

    def _bet_signal_age(self, bet: dict[str, Any]) -> float:
        captured_at = str((bet or {}).get("captured_at") or "").strip()
        if captured_at:
            try:
                dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
            except Exception:
                pass
        queued_at = float((bet or {}).get("queued_at") or time.time())
        return max(0.0, time.time() - queued_at)

    def _max_bet_signal_age_sec(self, bet: dict[str, Any] | None = None) -> float:
        # 追従/TIEプッシュBETは前ハンド決済時に置くため、次のベット窓(最大~30s先)まで
        # 保持できるよう通常より長い上限にする(VPS NOWは20sのまま=古いNOWを弾く)。
        if isinstance(bet, dict) and bet.get("is_follow"):
            return float(os.getenv("BACOPY_FOLLOW_MAX_BET_SIGNAL_AGE_SEC", "45") or 45)
        return float(os.getenv("BACOPY_MAX_BET_SIGNAL_AGE_SEC", "20") or 20)

    def _mark_bet_failed(self, bet: dict[str, Any] | None, reason: str, phase: str = "bet_failed", **extra: Any) -> None:
        if not isinstance(bet, dict):
            return
        bid = str(bet.get("bet_id") or "").strip()
        if not bid:
            return
        payload = {
            "reason": str(reason or "bet_failed"),
            "phase": str(phase or "bet_failed"),
            "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "table_id": str(bet.get("table_id") or ""),
            "side": str(bet.get("side") or ""),
            "amount": float(bet.get("amount") or 0.0),
            "decision_id": str(bet.get("decision_id") or ""),
        }
        payload.update(extra)
        self._failed_bet_ids[bid] = payload
        logger.warning(
            f"[BET-FAILED] bet_id={bid} reason={payload['reason']} "
            f"phase={payload['phase']} table={payload['table_id']}"
        )
        self._clear_active_now_bet_hold(
            bet_id=bid,
            table_id=str(bet.get("table_id") or ""),
            decision_id=str(bet.get("decision_id") or ""),
            reason=str(reason or "bet_failed"),
        )

    def _mark_bet_confirmed(self, bet: dict[str, Any], confirm: dict[str, Any]) -> None:
        bid = str((bet or {}).get("bet_id") or "").strip()
        if not bid:
            return
        payload = {
            "bet_id": bid,
            "decision_id": str((bet or {}).get("decision_id") or ""),
            "table_id": str((bet or {}).get("table_id") or ""),
            "table_name": str((bet or {}).get("table_name") or ""),
            "side": str((bet or {}).get("side") or ""),
            "amount": float((bet or {}).get("amount") or 0.0),
            "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        payload.update(confirm or {})
        self._confirmed_bets[bid] = payload
        self._sent_bet_ids.add(bid)
        logger.info(
            f"[BET-CONFIRMED] bet_id={bid} type={payload.get('confirm_type')} "
            f"table={payload.get('table_id')} game={payload.get('game_id') or '-'}"
        )

    def _update_stake_balance_from_msg(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data:
            return
        updated = False
        for field in ("availableBalances", "vaultBalances"):
            raw = data.get(field)
            if isinstance(raw, dict):
                items = [raw]
            elif isinstance(raw, list):
                items = [x for x in raw if isinstance(x, dict)]
            else:
                items = []
            for item in items:
                bal = item.get("balance") if isinstance(item.get("balance"), dict) else {}
                cur = str(bal.get("currency") or "").strip().upper()
                if not cur:
                    continue
                try:
                    if bal.get("amount") is not None:
                        self._stake_balance_by_currency[cur] = float(bal.get("amount"))
                        updated = True
                    if item.get("amount") is not None:
                        self._stake_balance_delta_by_currency[cur] = float(item.get("amount"))
                        updated = True
                except Exception:
                    continue
        if updated:
            self._last_stake_balance_at = time.time()

    def _on_proxy_ws_message(self, route_url: str, msg: Any, *, is_recv: bool) -> None:
        try:
            data = msg.decode("utf-8", errors="replace") if isinstance(msg, (bytes, bytearray)) else str(msg or "")
        except Exception:
            return
        low_url = str(route_url or "").lower()
        if "stake" not in low_url and "stake" not in data.lower():
            return
        try:
            obj = json.loads(data)
        except Exception:
            return
        if isinstance(obj, dict):
            self._update_stake_balance_from_msg(obj)

    def _trusted_bet_confirm_since(
        self,
        *,
        start: float,
        amount: float,
        game_id: str,
        table_id: str = "",
        before_balances: dict[str, float],
    ) -> dict[str, Any] | None:
        try:
            now_ts = time.time()
            game_age = (now_ts - self._last_game_bet_confirm_at) if self._last_game_bet_confirm_at else -1.0
            lpbet_age = (now_ts - self._last_lpbet_at) if self._last_lpbet_at else -1.0
            stake_age = (now_ts - self._last_stake_balance_at) if self._last_stake_balance_at else -1.0
            logger.info(
                f"[CONFIRM-PROBE] amount=${float(amount):.2f} game={game_id or '-'} "
                f"table={table_id or '-'} stake_delta_keys={list(self._stake_balance_delta_by_currency.keys())} "
                f"balance_keys={list(self._stake_balance_by_currency.keys())} "
                f"stake_age={stake_age:.1f}s game_confirm_age={game_age:.1f}s lpbet_age={lpbet_age:.1f}s"
            )
        except Exception:
            pass
        # Prefer Stake balance delta/drop, matching the existing modes' strongest
        # confirmation signal.
        for cur, delta in list(self._stake_balance_delta_by_currency.items()):
            try:
                if self._last_stake_balance_at >= start - 0.1 and float(delta) <= -max(0.0, float(amount) * 0.9):
                    return {
                        "confirm_type": "stake_delta",
                        "currency": cur,
                        "delta": float(delta),
                        "confirmed_amount": abs(float(delta)),
                        "game_id": str(game_id or ""),
                        "table_id": str(table_id or ""),
                    }
            except Exception:
                continue
        for cur, before in list(before_balances.items()):
            try:
                after = self._stake_balance_by_currency.get(cur)
                if after is not None and self._last_stake_balance_at >= start - 0.1:
                    drop = float(before) - float(after)
                    if drop >= max(0.0, float(amount) * 0.9):
                        return {
                            "confirm_type": "stake_balance_drop",
                            "currency": cur,
                            "before": float(before),
                            "after": float(after),
                            "confirmed_amount": drop,
                            "game_id": str(game_id or ""),
                            "table_id": str(table_id or ""),
                        }
            except Exception:
                continue
        if self._last_game_bet_confirm_at >= start - 0.1:
            detail = dict(self._last_game_bet_confirm or {})
            detail_table = str(
                detail.get("table")
                or detail.get("tableId")
                or detail.get("tableid")
                or detail.get("sourceTable")
                or detail.get("sourceTableId")
                or detail.get("sourcetableId")
                or ""
            ).strip()
            expected_table = str(table_id or "").strip()
            if expected_table:
                if detail_table and detail_table != expected_table:
                    logger.warning(
                        f"[LIVE] ignore game_ws_bet confirm for other table: "
                        f"expected={expected_table} actual={detail_table}"
                    )
                    return None
                if not detail_table and self._multi_lobby_mode:
                    logger.warning(
                        f"[LIVE] ignore game_ws_bet confirm without table id in multi mode: "
                        f"expected={expected_table} detail={detail}"
                    )
                    return None
            actual_amount = 0.0
            try:
                raw_amount = (
                    detail.get("amount")
                    or detail.get("stake")
                    or detail.get("betAmount")
                    or detail.get("bet_amount")
                    or 0
                )
                actual_amount = float(raw_amount or 0.0)
            except Exception:
                actual_amount = 0.0
            if actual_amount <= 0:
                actual_amount = float(amount)
            partial = actual_amount > 0 and abs(actual_amount - float(amount)) > 0.01
            if partial:
                logger.warning(
                    f"[LIVE] partial bet confirm: planned=${float(amount):.2f} "
                    f"actual=${actual_amount:.2f} detail={detail}"
                )
            return {
                "confirm_type": "game_ws_bet",
                "confirmed_amount": actual_amount,
                "planned_amount": float(amount),
                "partial_bet": partial,
                "game_id": str(game_id or ""),
                "table_id": expected_table,
                "confirm_table_id": detail_table,
                "detail": detail,
            }
        return None

    # ── setup ────────────────────────────────────────────────────────

    def setup(self, context: Any, lobby_page: Any, bet_page: Any | None = None) -> None:
        self._context = context
        self._lobby_page = lobby_page
        self._bet_page = bet_page or lobby_page
        logger.info(
            f"[EXEC-SETUP] setup called. multi_lobby={self._multi_lobby_mode} "
            f"diagnostic_only={self._multi_diagnostic_only} "
            f"multi_bet_transport={self._multi_bet_transport} "
            f"lobby_page={getattr(lobby_page,'url','?')[:80]}"
        )
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
            lobby_url = PRAGMATIC_BACCARAT_LOBBY_URL
            logger.info("[EXEC-SETUP] multi-lobby: loading lobby so bridge pre-installs before multi-table WS opens")
            try:
                cur_url = str(getattr(lobby_page, "url", "") or "")
                if cur_url.startswith("about:") or "stake.com" not in cur_url:
                    logger.info(f"[EXEC-SETUP] current page is not lobby ({cur_url[:80]}); goto lobby")
                    lobby_page.goto(lobby_url, wait_until="domcontentloaded", timeout=45000)
                else:
                    lobby_page.reload(wait_until="domcontentloaded", timeout=30000)
                lobby_page.wait_for_timeout(5000)
                logger.info("[EXEC-SETUP] lobby load/reload complete")
            except Exception as e:
                logger.warning(f"[EXEC-SETUP] lobby reload failed: {e}")
                try:
                    logger.info("[EXEC-SETUP] retry goto lobby after reload failure")
                    lobby_page.goto(lobby_url, wait_until="domcontentloaded", timeout=45000)
                    lobby_page.wait_for_timeout(5000)
                    logger.info("[EXEC-SETUP] lobby goto retry complete")
                except Exception as e2:
                    logger.warning(f"[EXEC-SETUP] lobby goto retry failed: {e2}")
            logger.info(
                "[EXEC-SETUP] multi-lobby mode: defer multi-play entry to tick "
                "so VPS polling can start without blocking on lobby join"
            )
            self._multi_area_ready = False
            self._last_multi_area_ensure_at = 0.0
        logger.info("[EXEC-SETUP] lobby monitoring ready; waiting for VPS whitelist decisions")

    def _attach_page(self, page: Any) -> None:
        """既存/新規ページへ bridge + websocket hook を1回だけ設定。"""
        # The passive dga observer page must NOT receive the betting WS handler;
        # it only forwards dga frames via its own minimal _on_dga_observer_ws.
        if getattr(self, "_dga_observer_opening", False) or page is getattr(self, "_dga_observer_page", None):
            logger.info("[DGA-OBS] skip betting attach for observer page")
            return
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
            existing_ws = []
            try:
                ws_list = list(getattr(page, "websockets", []) or [])
                existing_ws = [str(getattr(w, "url", "") or "")[:120] for w in ws_list]
            except Exception:
                existing_ws = []
            page.on("websocket", self._on_ws_event)
            logger.info(
                f"[EXEC-ATTACH] websocket hook registered page={getattr(page,'url','?')[:60]} "
                f"existing_ws={len(existing_ws)}"
            )
            if existing_ws:
                logger.info(
                    f"[WS-HOOK] page={getattr(page,'url','?')[:60]} existing={existing_ws[:6]}"
                )
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

    def _reset_multi_lobby_runtime(self, reason: str) -> None:
        if self._prepared_table_id:
            logger.warning(
                f"[PREPARED-CLEAR] reason={reason} prev_target={self._prepared_table_id!r} "
                f"age={time.time() - (self._prepared_at or 0.0):.1f}s"
            )
        self._prepared_table_id = ""
        self._prepared_at = 0.0
        self._table_states.clear()
        self._game_to_table_id.clear()
        self._saw_per_table_betsopen = False
        self._multi_tile_snapshot = {}
        self._multi_tile_snapshot_at = 0.0
        self._multi_area_ready = False
        self._is_multi_table_ws = False
        self._last_multi_area_ensure_at = 0.0
        self._game_ws_url = ""
        self._last_game_ws_recv_at = 0.0

    def _recover_pragmatic_lobby(self, reason: str, *, force: bool = False) -> bool:
        """Return Chrome to the Pragmatic baccarat lobby and rebuild multi-table state."""
        if not self._multi_lobby_mode:
            return False
        now = time.time()
        if not force and now - self._last_lobby_recover_at < 8.0:
            return False
        self._last_lobby_recover_at = now
        page = self._bet_page or self._lobby_page
        if page is None:
            return False
        try:
            cur_url = str(getattr(page, "url", "") or "")
        except Exception:
            cur_url = ""
        logger.warning(
            f"[LIVE-RECOVER] goto pragmatic lobby reason={reason} "
            f"from={cur_url[:100] or '(unknown)'}"
        )
        self._reset_multi_lobby_runtime(f"recover:{reason}")
        try:
            if cur_url and "pragmatic-play-live-lobby-baccarat" in cur_url:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            else:
                page.goto(PRAGMATIC_BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            self._inject_all(page)
            self._ensure_multi_area(force=True)
            logger.info(f"[LIVE-RECOVER] pragmatic lobby recovery complete reason={reason}")
            return True
        except Exception as ex:
            logger.warning(f"[LIVE-RECOVER] pragmatic lobby recovery failed reason={reason}: {ex}")
            return False

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
        max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
        return 0 < age < max_age

    def _refresh_multi_tile_snapshot(self, force: bool = False) -> dict[str, Any]:
        if not self._multi_lobby_mode:
            return {}
        if threading.get_ident() != self._owner_thread_id:
            # Playwright objects are thread-bound. Polling threads may ask for an
            # antenna check while handling VPS decisions; use the latest owner
            # thread snapshot instead of touching the DOM from the wrong thread.
            return self._multi_tile_snapshot or {}
        now = time.time()
        if (not force) and self._multi_tile_snapshot and now - self._multi_tile_snapshot_at < 0.7:
            return self._multi_tile_snapshot
        frames: list[Any] = []
        page = self._bet_page or self._lobby_page
        try:
            pages = [page] + list(self._context.pages or [])
        except Exception:
            pages = [page]
        seen: set[int] = set()
        for p in pages:
            if p is None:
                continue
            try:
                candidates = [p] + list(p.frames or [])
            except Exception:
                candidates = [p]
            for fr in candidates:
                marker = id(fr)
                if marker in seen:
                    continue
                seen.add(marker)
                try:
                    u = str(getattr(fr, "url", "") or "")
                except Exception:
                    u = ""
                if "pragmaticplaylive" in u or "qpidreoxcc.net" in u or "/desktop/multibaccarat" in u:
                    frames.append(fr)
        best: dict[str, Any] = {}
        for fr in frames:
            try:
                snap = fr.evaluate(_MULTI_TILE_SNAPSHOT_JS)
            except Exception:
                continue
            if not isinstance(snap, dict):
                continue
            tiles = snap.get("tiles") or []
            if not isinstance(tiles, list) or not tiles:
                continue
            visible_count = sum(1 for t in tiles if isinstance(t, dict) and t.get("visible"))
            if visible_count > sum(1 for t in (best.get("tiles") or []) if isinstance(t, dict) and t.get("visible")):
                best = snap
                best["_frame"] = fr
        if best:
            self._multi_tile_snapshot = best
            self._multi_tile_snapshot_at = now
            try:
                vis = [str(t.get("qpid") or "") for t in (best.get("tiles") or []) if isinstance(t, dict) and t.get("visible")]
                logger.debug(f"[ANTENNA] snapshot visible={len(vis)} sample={vis[:8]}")
            except Exception:
                pass
        return best or self._multi_tile_snapshot

    def _multi_tile_distance(self, source_qpid: str, target_qpid: str) -> dict[str, Any]:
        src = str(source_qpid or "").strip()
        tgt = str(target_qpid or "").strip()
        if not src or not tgt:
            return {"ok": False, "reason": "missing_qpid"}
        snap = self._refresh_multi_tile_snapshot(force=False)
        tiles = {
            str(t.get("qpid") or ""): t
            for t in (snap.get("tiles") or [])
            if isinstance(t, dict) and str(t.get("qpid") or "")
        }
        a = tiles.get(src)
        b = tiles.get(tgt)
        if not a or not b:
            return {"ok": False, "reason": "tile_not_visible", "source_found": bool(a), "target_found": bool(b)}
        widths = [float(t.get("w") or 0) for t in tiles.values() if float(t.get("w") or 0) > 40]
        heights = [float(t.get("h") or 0) for t in tiles.values() if float(t.get("h") or 0) > 40]
        cell_w = sorted(widths)[len(widths) // 2] if widths else max(float(a.get("w") or 1), 1.0)
        cell_h = sorted(heights)[len(heights) // 2] if heights else max(float(a.get("h") or 1), 1.0)
        dx = abs(float(a.get("cx") or 0) - float(b.get("cx") or 0)) / max(cell_w * 0.85, 1.0)
        dy = abs(float(a.get("cy") or 0) - float(b.get("cy") or 0)) / max(cell_h * 0.85, 1.0)
        grid = max(int(round(dx)), int(round(dy)))
        return {"ok": True, "grid": grid, "dx": round(dx, 2), "dy": round(dy, 2), "source": a, "target": b}

    def is_table_in_antenna_zone(self, table_id: str, side: str = "") -> bool:
        target = str(table_id or "").strip()
        if not target:
            return False
        prepared = str(self._prepared_table_id or "").strip()
        if prepared and prepared == target:
            return True
        radius = int(os.getenv("BACOPY_MULTI_ANTENNA_RADIUS", "2") or 2)
        if radius <= 0 or not prepared:
            return False
        dist = self._multi_tile_distance(prepared, target)
        if not dist.get("ok"):
            logger.info(f"[ANTENNA] target not in zone target={target} prepared={prepared} detail={dist}")
            return False
        grid = int(dist.get("grid") or 99)
        target_tile = dist.get("target") if isinstance(dist.get("target"), dict) else {}
        visible = bool(target_tile.get("visible"))
        side_u = str(side or "").upper()
        has_side = (side_u == "P" and bool(target_tile.get("hasP"))) or (side_u == "B" and bool(target_tile.get("hasB"))) or side_u not in ("P", "B")
        ok = visible and has_side and grid <= radius
        logger.info(
            f"[ANTENNA] prepared={prepared} target={target} side={side_u or '-'} "
            f"grid={grid} radius={radius} visible={visible} has_side={has_side} ok={ok} "
            f"dx={dist.get('dx')} dy={dist.get('dy')}"
        )
        return ok

    def _focus_table_in_multi(self, req: dict) -> bool:
        page = self._bet_page or self._lobby_page
        if page is None:
            logger.warning("[FOCUS] FAIL: no page (bet_page and lobby_page both None)")
            return False
        try:
            page_url = page.url[:80]
        except Exception:
            page_url = "(unknown)"
        table_id = str(req.get("table_id") or "").strip()
        qpid = str(req.get("qpid") or "").strip()
        table_name = str(req.get("table_name") or "").strip()
        intent = str(req.get("intent") or "preposition").strip().lower()
        focus_started = time.time()
        # Do not let a forecast focus request own the costly multi-area entry path.
        # At startup that path can take tens of seconds; if a live signal arrives
        # meanwhile, the owner thread cannot process the BET until it is stale.
        # Let the normal tick enter/repair multi-area, then focus the forecast on
        # the next preposition poll after the multi-table WS is alive.
        if self._multi_lobby_mode and intent == "preposition" and not self._is_multi_table_ws:
            logger.info(
                f"[FOCUS] defer preposition until multi-WS ready: "
                f"table={table_name or table_id or qpid or '-'}"
            )
            self._multi_area_ready = False
            self._last_multi_area_ensure_at = 0.0
            return False
        # A live multi-table WS proves that the multi-play view is already active.
        # Re-clicking its tab during a preposition remounts the table grid and makes
        # the target disappear during the short betting window.
        self._ensure_multi_area(force=False)
        preposition_click = os.getenv("BACOPY_PREPOSITION_CLICK_TILE", "1") != "0"
        click = intent in ("prepare", "decision", "bet", "manual_assist") or (intent == "preposition" and preposition_click)
        side = str(req.get("side") or "").upper()
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
        logger.info(
            f"[AUTO-PROBE] focus start intent={intent} table={table_name or table_id or qpid or '-'} "
            f"qpid={qpid or table_id or '-'} click={click} side={side or '-'} "
            f"preselect=${float(req.get('preselect_amount') or req.get('amount') or 0.0):.2f} "
            f"multi={self._multi_lobby_mode} multi_ws={self._is_multi_table_ws}"
        )

        # Diagnostic mode must not spend the preposition window trying to
        # navigate an empty lobby frame. Probe every Pragmatic frame directly;
        # the shared multi-table frame is where side buttons were observed.
        if self._multi_diagnostic_only and side in ("P", "B") and (qpid or table_id):
            tid = str(qpid or table_id)
            side_class = {"P": "ym_yP", "B": "ym_yQ"}[side]
            probe_frames: list[Any] = []
            seen_frames: set[int] = set()
            try:
                pages = [page] + list(self._context.pages or [])
            except Exception:
                pages = [page]
            for candidate_page in pages:
                try:
                    frames = [candidate_page] + list(candidate_page.frames or [])
                except Exception:
                    frames = [candidate_page]
                for frame in frames:
                    marker = id(frame)
                    if marker in seen_frames:
                        continue
                    seen_frames.add(marker)
                    try:
                        frame_url = str(getattr(frame, "url", "") or "")
                    except Exception:
                        frame_url = ""
                    if "pragmaticplaylive" in frame_url or "qpidreoxcc.net" in frame_url:
                        probe_frames.append(frame)
            logger.info(f"[ML-DIAG] direct frame probe count={len(probe_frames)} table={tid} side={side}")
            seek_frames: list[tuple[int, Any]] = []
            for i, probe_frame in enumerate(probe_frames):
                try:
                    probe = probe_frame.evaluate(
                        self._JS_BET_COORDS,
                        {"qpid": tid, "tableName": table_name, "sideClass": side_class, "sideCode": side},
                    )
                    visible_count = int((probe or {}).get("visibleCount") or 0) if isinstance(probe, dict) else 0
                    if visible_count > 0:
                        seek_frames.append((i, probe_frame))
                    if bool((probe or {}).get("ok")) or visible_count > 0:
                        logger.info(f"[ML-DIAG-PROBE] direct frame[{i}] table={tid} side={side} coords={probe}")
                    if isinstance(probe, dict) and probe.get("ok"):
                        return True
                except Exception as ex:
                    logger.warning(f"[ML-DIAG-PROBE] direct frame[{i}] failed table={tid}: {ex}")
            # Multi tiles are virtualized: if the requested qpid is off-screen,
            # scroll the actual multi-game frame until its TileHeight-qpid card
            # is mounted, then probe the side button inside that exact tile.
            seek_args = {
                "qpid": tid,
                "click": False,
                "maxScroll": int(os.getenv("BACOPY_MULTI_SCROLL_MAX", "48") or "48"),
                "candidates": [table_name, tid],
                "hintIndex": -1,
                "hintTotal": 0,
            }
            for i, probe_frame in (seek_frames or list(enumerate(probe_frames))):
                try:
                    seek = probe_frame.evaluate(_MULTI_LOBBY_FOCUS_JS, seek_args)
                    logger.info(f"[ML-DIAG-SEEK] direct frame[{i}] table={tid} result={seek}")
                    if not (isinstance(seek, dict) and seek.get("found")):
                        continue
                    self._prepared_table_id = tid
                    self._prepared_at = time.time()
                    self._diag_prepared_side = side
                    self._refresh_multi_tile_snapshot(force=True)
                    logger.info(f"[ML-DIAG-PREPARED] exact tile mounted table={tid} side={side}")
                    self._hover_multi_tile(probe_frame, tid)
                    probe = probe_frame.evaluate(
                        self._JS_BET_COORDS,
                        {"qpid": tid, "tableName": table_name, "sideClass": side_class, "sideCode": side},
                    )
                    logger.info(f"[ML-DIAG-PROBE] mapped frame[{i}] table={tid} side={side} coords={probe}")
                    if isinstance(probe, dict) and probe.get("ok"):
                        return True
                except Exception as ex:
                    logger.warning(f"[ML-DIAG-SEEK] direct frame[{i}] failed table={tid}: {ex}")
            logger.warning(f"[ML-DIAG-PROBE] no mapped side button found table={tid} side={side}")
            return False

        frames = [page]
        direct_frame = None
        if self._multi_lobby_mode:
            direct_frame = self._find_pragmatic_frame(target_qpid=str(qpid or table_id))
            if direct_frame is None:
                logger.warning(
                    "[FOCUS] multi-baccarat frame not found; force multi-area re-entry before focus"
                )
                self._multi_area_ready = False
                self._is_multi_table_ws = False
                self._last_multi_dom_recover_at = time.time()
                self._ensure_multi_area(force=True)
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                direct_frame = self._find_pragmatic_frame(target_qpid=str(qpid or table_id))
            if direct_frame is None:
                logger.warning(
                    f"[FOCUS] FAIL: no multi-baccarat DOM frame for table={table_name or table_id or qpid or '-'} "
                    f"intent={intent}; will wait for next poll/recovery"
                )
                return False
            frames = [direct_frame]
            logger.info("[FOCUS] using direct multi-baccarat frame only")
        else:
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
            "maxScroll": int(
                os.getenv(
                    "BACOPY_MULTI_PREPOSITION_SCROLL_MAX" if intent in ("preposition", "manual_assist") else "BACOPY_MULTI_SCROLL_MAX",
                    "48" if intent in ("preposition", "manual_assist") else "18",
                )
                or ("48" if intent in ("preposition", "manual_assist") else "18")
            ),
            "candidates": candidates,
            "hintIndex": -1,
            "hintTotal": 0,
            "hintScrollTop": -1,
            "hintScrollRatio": -1,
            "maxMs": int(
                os.getenv(
                    "BACOPY_MULTI_PREPOSITION_FOCUS_MS" if intent in ("preposition", "manual_assist") else "BACOPY_MULTI_FOCUS_MS",
                    "9000" if intent in ("preposition", "manual_assist") else "12000",
                )
                or ("9000" if intent in ("preposition", "manual_assist") else "12000")
            ),
            "assistStatus": "NOW" if intent == "manual_assist" else ("READY" if intent == "preposition" else ""),
            "side": side,
            "amount": float(req.get("preselect_amount") or req.get("amount") or 0.0),
            # NOW overlay lifetime ≈ one hand (default 70s). Covers betsopen→result
            # + operator reaction, then auto-clears so a skipped NOW does not block
            # the view forever. Matches the NOW-lock window (which suppresses scroll
            # for the hand). Tunable via BACOPY_ASSIST_NOW_TTL_MS.
            "nowTtlMs": int(os.getenv("BACOPY_ASSIST_NOW_TTL_MS", "70000") or 70000),
            # Scroll-lock maintenance tuning (calmer = easier to watch the tile).
            # The lock only needs to keep the tile MOUNTED, not pixel-locked.
            "recenterMs": int(os.getenv("BACOPY_ASSIST_RECENTER_MS", "400") or 400),
            "recenterDeadband": int(os.getenv("BACOPY_ASSIST_RECENTER_DEADBAND_PX", "48") or 48),
            "windowDrift": int(os.getenv("BACOPY_ASSIST_WINDOW_DRIFT_PX", "120") or 120),
        }
        cache_key = str(qpid or table_id or "").strip()
        cached = self._table_focus_cache.get(cache_key) if cache_key else None
        if isinstance(cached, dict):
            if cached.get("table_name") and cached.get("table_name") not in args["candidates"]:
                args["candidates"].append(str(cached.get("table_name")))
            try:
                args["hintIndex"] = int(cached.get("match_index", -1))
                args["hintTotal"] = int(cached.get("total_nodes", 0))
                args["hintScrollTop"] = int(cached.get("scroll_top", -1))
                args["hintScrollRatio"] = float(cached.get("scroll_ratio", -1))
            except Exception:
                args["hintIndex"] = -1
                args["hintTotal"] = 0
                args["hintScrollTop"] = -1
                args["hintScrollRatio"] = -1

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
            if isinstance(res, dict) and res.get("reason") == "not_multi_baccarat_dom":
                logger.warning(
                    f"[FOCUS] frame[{i}] is not multi-baccarat DOM; reset multi flags and request re-entry"
                )
                self._multi_area_ready = False
                self._is_multi_table_ws = False
                self._last_multi_area_ensure_at = 0.0
                self._last_multi_dom_recover_at = time.time()
                if self._multi_lobby_mode:
                    try:
                        self._ensure_multi_area(force=True)
                    except Exception as ex:
                        logger.warning(f"[FOCUS] multi-area re-entry failed after DOM mismatch: {ex}")
                continue
            if isinstance(res, dict) and res.get("found"):
                tid = str(qpid or table_id or "")
                if tid:
                    st = self._ensure_table_state(tid)
                    if table_name and not st.get("table_name"):
                        st["table_name"] = table_name
                if cache_key:
                    try:
                        scroll = res.get("scroll") if isinstance(res.get("scroll"), dict) else {}
                        self._table_focus_cache[cache_key] = {
                            "table_name": str(table_name or cached.get("table_name") if isinstance(cached, dict) else table_name or ""),
                            "frame_index": int(i),
                            "match_index": int(res.get("matchIndex") or -1),
                            "total_nodes": int(res.get("totalNodes") or 0),
                            "scroll_top": int(scroll.get("scrollTop") or 0),
                            "scroll_height": int(scroll.get("scrollHeight") or 0),
                            "client_height": int(scroll.get("clientHeight") or 0),
                            "scroll_ratio": float(scroll.get("scrollRatio") or 0.0),
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
                logger.info(
                    f"[AUTO-PROBE] focus result=found intent={intent} "
                    f"table={table_name or table_id or qpid or '-'} qpid={qpid or table_id or '-'} "
                    f"clicked={bool(res.get('clicked'))} elapsed_ms={(time.time() - focus_started) * 1000:.0f} "
                    f"match_index={res.get('matchIndex') if isinstance(res, dict) else '-'} "
                    f"total_nodes={res.get('totalNodes') if isinstance(res, dict) else '-'}"
                )
                if tid and intent in ("preposition", "manual_assist", "prepare", "decision"):
                    self._start_assist_focus_hold(tid, table_name or table_id or qpid or tid, intent=intent)
                if click and res.get("clicked") and tid:
                    try:
                        page.wait_for_timeout(750)
                    except Exception:
                        pass
                    self._prepared_table_id = tid
                    self._prepared_at = time.time()
                    self._refresh_multi_tile_snapshot(force=True)
                    logger.info(f"[ML-PREPARED] active target ready for BET table={tid}")
                    if intent == "manual_assist":
                        watch_sec = float(os.getenv("BACOPY_MANUAL_ASSIST_WATCH_SEC", "90") or 90)
                        self._manual_assist_watch_until = time.time() + max(15.0, watch_sec)
                        self._manual_assist_watch_target = tid
                        logger.info(
                            f"[MANUAL-ASSIST-WATCH] armed target={tid} "
                            f"sec={max(15.0, watch_sec):.1f}"
                        )
                if side in ("P", "B") and tid:
                    side_class = {"P": "ym_yP", "B": "ym_yQ"}[side]
                    probe_frame = self._find_pragmatic_frame(target_qpid=tid)
                    if probe_frame is not None:
                        try:
                            probe = probe_frame.evaluate(
                                self._JS_BET_COORDS,
                                {
                                    "qpid": tid,
                                    "tableName": table_name,
                                    "sideClass": side_class,
                                    "sideCode": side,
                                },
                            )
                            logger.info(
                                f"[ML-DIAG-PROBE] table={tid} side={side} "
                                f"clicked={bool(res.get('clicked'))} coords={probe}"
                            )
                        except Exception as ex:
                            logger.warning(f"[ML-DIAG-PROBE] failed table={tid}: {ex}")
                return True
        logger.warning(
            f"[FOCUS] NOT FOUND table={table_name or table_id or qpid or '-'} intent={intent} candidates={candidates}"
        )
        logger.warning(
            f"[AUTO-PROBE] focus result=not_found intent={intent} "
            f"table={table_name or table_id or qpid or '-'} qpid={qpid or table_id or '-'} "
            f"elapsed_ms={(time.time() - focus_started) * 1000:.0f} candidates={candidates}"
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
                        if self._recover_pragmatic_lobby("multi_ws_silent", force=True):
                            logger.info("[MULTI-AREA] lobby recovery complete — WS state reset")
                except Exception as _re:
                    logger.warning(f"[MULTI-AREA] lobby recovery failed: {_re}")
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
                # マルチプレイ画面に復帰できた → デッドロック検知カウンタをリセット
                self._multi_tab_missing_since = 0.0
                self._multi_tab_missing_count = 0
                return
        if not found_tab:
            logger.warning(f"[MULTI-AREA] multi-play tab NOT FOUND in page — may be on wrong page or UI changed")
            # ── デッドロック検知 → ページreload復旧 ──────────────────────
            # tab NOT FOUND かつ multi_ws=False が継続する場合、defer(L2198)で
            # preposition が毎回 return し、_is_multi_table_ws は WS 到達でしか
            # True にならないため永久に NOW 昇格できない(chicken-and-egg)。
            # fallback join(_join_table)で復帰できないケースの最終手段として、
            # 連続 NOT FOUND が閾値を超えたら _recover_pragmatic_lobby で
            # ページreload→マルチプレイ再入場を強制する。
            if self._multi_tab_missing_since <= 0.0:
                self._multi_tab_missing_since = now
                self._multi_tab_missing_count = 1
            else:
                self._multi_tab_missing_count += 1
            deadlock_enable = os.getenv("BACOPY_MULTI_DEADLOCK_RELOAD_ENABLE", "1").strip() != "0"
            deadlock_secs = float(os.getenv("BACOPY_MULTI_DEADLOCK_RELOAD_SEC", "90") or 90)
            deadlock_cooldown = float(os.getenv("BACOPY_MULTI_DEADLOCK_RELOAD_COOLDOWN_SEC", "120") or 120)
            missing_for = now - self._multi_tab_missing_since
            if (
                deadlock_enable
                and not self._is_multi_table_ws
                and missing_for >= deadlock_secs
                and (now - self._last_multi_deadlock_recover_at) >= deadlock_cooldown
            ):
                self._last_multi_deadlock_recover_at = now
                logger.warning(
                    f"[MULTI-AREA] DEADLOCK: multi-play tab missing for {missing_for:.0f}s "
                    f"(count={self._multi_tab_missing_count}, multi_ws=False) "
                    f"— forcing lobby reload recovery"
                )
                try:
                    if self._recover_pragmatic_lobby("multi_tab_deadlock", force=True):
                        logger.info("[MULTI-AREA] deadlock reload recovery complete — multi state reset")
                        # 復旧を試みたのでカウンタをリセット(再確立は WS 到達で確定)
                        self._multi_tab_missing_since = 0.0
                        self._multi_tab_missing_count = 0
                        return
                    logger.warning("[MULTI-AREA] deadlock reload recovery did not complete; will retry after cooldown")
                except Exception as _dre:
                    logger.warning(f"[MULTI-AREA] deadlock reload recovery error: {_dre}")
                return
            allow_fallback_join = os.getenv("BACOPY_MULTI_ALLOW_BLOCKING_FALLBACK_JOIN", "1").strip() != "0"
            try:
                busy = bool(
                    self._switch_request
                    or self._pending_bet
                    or self._switch_in_progress
                    or self.is_bet_in_flight()
                )
            except Exception:
                busy = bool(self._switch_request or self._pending_bet or self._switch_in_progress)
            should_fallback_join = bool(
                force or (
                    allow_fallback_join
                    and self._multi_lobby_mode
                    and not self._is_multi_table_ws
                    and not busy
                )
            )
            if busy and not force and not self._is_multi_table_ws:
                logger.warning("[MULTI-AREA] fallback join deferred while switch/BET is active")
                return
            if not should_fallback_join and not force and not self._is_multi_table_ws:
                logger.warning(
                    "[MULTI-AREA] skip blocking fallback join; keep owner tick alive "
                    "(set BACOPY_MULTI_ALLOW_BLOCKING_FALLBACK_JOIN=0 to disable)"
                )
                return
            fallback_cooldown = float(os.getenv("BACOPY_MULTI_FALLBACK_JOIN_COOLDOWN_SEC", "45") or 45)
            if should_fallback_join and (now - self._last_multi_fallback_join_at < max(5.0, fallback_cooldown)):
                logger.info(
                    f"[MULTI-AREA] fallback join cooldown "
                    f"{max(0.0, max(5.0, fallback_cooldown) - (now - self._last_multi_fallback_join_at)):.1f}s"
                )
                return
            if should_fallback_join:
                self._last_multi_fallback_join_at = now
                try:
                    from bacopy_executor_pragmatic_ws_live import _join_table
                    logger.info(
                        f"[MULTI-AREA] fallback join BACCARAT_MULTIPLAY via existing lobby matcher "
                        f"force={force} multi_ws={self._is_multi_table_ws}"
                    )
                    _join_table(
                        page,
                        table_substr="BACCARAT_MULTIPLAY",
                        auto_click_wait_sec=int(os.getenv("BACOPY_MULTI_JOIN_WAIT_SEC", "18") or "18"),
                        state=None,
                        on_tick=None,
                        is_initial=False,
                        qpid_table_id="",
                    )
                    self._multi_area_ready = True
                    logger.info("[MULTI-AREA] fallback join BACCARAT_MULTIPLAY complete")
                except Exception as ex:
                    logger.warning(f"[MULTI-AREA] fallback join BACCARAT_MULTIPLAY failed: {ex}")

    # ── game WS 検出 ─────────────────────────────────────────────────

    def set_dga_result_callback(self, cb) -> None:
        """Register a callback to receive raw dga lobby WS frames (the gameResult
        feed) for local signal computation. When unset (default), /dga stays
        skipped (v3 behaviour, byte-identical).

        Result source = a DIRECT TLS WS to the Pragmatic dga lobby endpoint
        (headless, casinoId-keyed, no browser, NO Stake session) on a background
        thread (proven: 164 gameResult / 60s / 61 tables). Fully decoupled from
        the betting Chrome, so it streams even while the betting frame sits in
        multibaccarat (which carries NO gameResult). The old passive-observer-page
        approach opened a 2nd Pragmatic lobby session and triggered Stake's "no
        games" anti-abuse block (2026-06-01) — retired; used only if
        BACOPY_DGA_LEGACY_OBSERVER=1.
        """
        self._dga_result_callback = cb
        if os.getenv("BACOPY_DGA_LEGACY_OBSERVER", "").strip().lower() in ("1", "true", "on"):
            try:
                self._start_dga_observer_page()
            except Exception as e:
                logger.warning(f"[DGA-OBS] legacy observer start failed: {e}")
            return
        try:
            self._start_dga_direct_source()
        except Exception as e:
            logger.warning(f"[DGA-DIRECT] source start failed: {e}")

    def _start_dga_direct_source(self) -> None:
        """Start the background thread that maintains a DIRECT dga lobby WS
        (headless, no browser, no Stake session) and forwards every gameResult
        frame to the local-signal callback. Fully decoupled from the betting
        Chrome — works regardless of whether the betting frame is lobby2 or
        multibaccarat. Idempotent. Disable with BACOPY_DGA_DIRECT_SOURCE=0."""
        if os.getenv("BACOPY_DGA_DIRECT_SOURCE", "1").strip().lower() in ("0", "false", "off", "no"):
            logger.info("[DGA-DIRECT] disabled via BACOPY_DGA_DIRECT_SOURCE=0")
            return
        if getattr(self, "_dga_direct_thread", None) is not None:
            return
        self._dga_direct_stop = False
        t = threading.Thread(target=self._dga_direct_loop, name="dga-direct-source", daemon=True)
        self._dga_direct_thread = t
        t.start()
        logger.info("[DGA-DIRECT] background direct dga WS source started")

    def _dga_direct_loop(self) -> None:
        """Maintain a direct TLS WS to the Pragmatic dga lobby: subscribe to all
        tables, keepalive-ping (~5s, separate thread), and forward gameResult
        frames to _dga_result_callback. Auto-reconnects on drop. Read-only."""
        def recv_exact(s, n):
            o = b""
            while len(o) < n:
                c = s.recv(n - len(o))
                if not c:
                    raise RuntimeError("dga ws closed")
                o += c
            return o

        backoff = 2.0
        while not getattr(self, "_dga_direct_stop", False):
            s = None
            ping_stop = {"v": False}
            try:
                s = _dga_direct_connect()
                _cdp_ws_send(s, json.dumps({"type": "statistics"}))
                _cdp_ws_send(s, json.dumps({"type": "available", "casinoId": _DGA_CASINO_ID}))
                _cdp_ws_send(s, json.dumps({
                    "type": "subscribe", "isDeltaEnabled": True,
                    "casinoId": _DGA_CASINO_ID, "key": _DGA_TABLE_KEYS, "currency": "USD",
                }))
                logger.info(f"[DGA-DIRECT] connected + subscribed ({len(_DGA_TABLE_KEYS)} tables)")
                backoff = 2.0

                def _ping(_s=s, _stop=ping_stop):
                    n = 0
                    while not _stop["v"] and not getattr(self, "_dga_direct_stop", False):
                        try:
                            _cdp_ws_send(_s, json.dumps({"type": "ping", "pingTime": int(time.time() * 1000)}))
                        except Exception:
                            return
                        n += 1
                        time.sleep(5)
                threading.Thread(target=_ping, name="dga-direct-ping", daemon=True).start()

                s.settimeout(45)
                while not getattr(self, "_dga_direct_stop", False):
                    msg = _cdp_ws_recv_into(s, recv_exact)
                    if "gameResult" in msg or "tableName" in msg:
                        cb = getattr(self, "_dga_result_callback", None)
                        if cb is not None:
                            try:
                                cb(msg)
                            except Exception as _e:
                                logger.debug(f"[DGA-DIRECT] callback error: {_e}")
            except Exception as e:
                logger.info(f"[DGA-DIRECT] stream ended ({e}); reconnecting in {backoff:.0f}s")
            finally:
                ping_stop["v"] = True
                try:
                    if s is not None:
                        s.close()
                except Exception:
                    pass
            if getattr(self, "_dga_direct_stop", False):
                break
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 20.0)
        logger.info("[DGA-DIRECT] source loop stopped")

    def _start_dga_observer_page(self) -> None:
        """Ensure a passive regular-lobby page whose dga data feed streams continuous
        gameResult for all tables (like the VPS collector). Frames are forwarded to
        the local-signal callback. Idempotent and restart-safe: reuses an existing
        orphaned lobby page (chrome_attach keeps tabs across engine restarts) instead
        of accumulating tabs. Disable with BACOPY_DGA_OBSERVER_PAGE=0.
        """
        if os.getenv("BACOPY_DGA_OBSERVER_PAGE", "1").strip().lower() in ("0", "false", "off", "no"):
            return
        existing = getattr(self, "_dga_observer_page", None)
        if existing is not None:
            try:
                if not existing.is_closed():
                    return
            except Exception:
                return
            self._dga_observer_page = None
        if self._context is None:
            logger.info("[DGA-OBS] context not ready; observer deferred")
            return
        # Reuse an extra/orphaned lobby page (NOT the betting page) so engine
        # restarts under chrome_attach do not pile up tabs.
        betting_ids = set()
        for bp in (self._lobby_page, self._bet_page):
            try:
                if bp is not None:
                    betting_ids.add(id(bp))
            except Exception:
                pass
        reuse = None
        try:
            for p in (self._context.pages or []):
                try:
                    if id(p) in betting_ids or p.is_closed():
                        continue
                    u = str(getattr(p, "url", "") or "")
                except Exception:
                    continue
                if "pragmatic-play-live-lobby-baccarat" in u:
                    reuse = p
                    break
        except Exception:
            reuse = None
        if reuse is not None:
            self._dga_observer_page = reuse
            try:
                reuse.add_init_script("window.open = function(){ return null; };")
            except Exception:
                pass
            self._hook_observer_page_ws(reuse)
            logger.info(f"[DGA-OBS] reusing existing lobby page as observer (no new tab): {str(getattr(reuse,'url','') or '')[:60]}")
            return
        # No reusable page → open a fresh one.
        self._dga_observer_opening = True
        try:
            p2 = self._context.new_page()
        except Exception as e:
            self._dga_observer_opening = False
            logger.warning(f"[DGA-OBS] new_page failed: {e}")
            return
        self._dga_observer_page = p2
        self._dga_observer_opening = False
        try:
            # Neuter window.open so the lobby cannot spawn extra popup tabs.
            p2.add_init_script("window.open = function(){ return null; };")
        except Exception:
            pass
        self._hook_observer_page_ws(p2)
        try:
            p2.goto(PRAGMATIC_BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=30000)
            logger.info("[DGA-OBS] passive observer page opened (regular lobby, continuous dga)")
        except Exception as e:
            logger.warning(f"[DGA-OBS] observer goto slow/failed (WS may still attach): {e}")

    def _hook_observer_page_ws(self, page: Any) -> None:
        """Hook the dga data WS on the observer page (future + already-open)."""
        try:
            page.on("websocket", self._on_dga_observer_ws)
        except Exception:
            pass
        try:
            for ws in list(getattr(page, "websockets", []) or []):
                self._on_dga_observer_ws(ws)
        except Exception:
            pass

    def _ensure_dga_observer(self) -> None:
        """Reopen the passive observer page if it was closed (it is the result
        source). MUST be called from the Playwright main-loop thread.

        RETIRED: the local-signal result source is now the raw-CDP Network
        subscription (_start_dga_cdp_source), a single session. The observer page
        opened a 2nd Pragmatic lobby session and triggered Stake's "no games"
        anti-abuse block (2026-06-01). This main-loop hook is a no-op unless
        BACOPY_DGA_LEGACY_OBSERVER=1 — so it can NEVER reopen the 2nd session.
        """
        if os.getenv("BACOPY_DGA_LEGACY_OBSERVER", "").strip().lower() not in ("1", "true", "on"):
            return
        if getattr(self, "_dga_result_callback", None) is None:
            return
        p = getattr(self, "_dga_observer_page", None)
        if p is not None:
            try:
                if not p.is_closed():
                    return
            except Exception:
                pass
        now = time.time()
        if now - getattr(self, "_dga_obs_reopen_at", 0.0) < 20.0:
            return
        self._dga_obs_reopen_at = now
        self._dga_observer_page = None
        logger.info("[DGA-OBS] observer missing/closed — reopening")
        try:
            self._start_dga_observer_page()
        except Exception as e:
            logger.warning(f"[DGA-OBS] reopen failed: {e}")

    def _on_dga_observer_ws(self, ws: Any) -> None:
        """Minimal WS handler for the passive observer page: forward ONLY the dga
        data-feed frames to the local-signal callback. No betting/multiplay logic.
        """
        url = str(getattr(ws, "url", "") or "")
        if "dga.pragmaticplaylive" not in url:
            return
        cb = getattr(self, "_dga_result_callback", None)
        if cb is None:
            return

        def _fwd(f, _cb=cb):
            try:
                body = f if isinstance(f, str) else (getattr(f, "body", "") or "")
            except Exception:
                body = ""
            if body:
                try:
                    _cb(body)
                except Exception as _e:
                    logger.debug(f"[DGA-OBS] fwd error: {_e}")

        try:
            ws.on("framereceived", _fwd)
            logger.info(f"[DGA-OBS] hooked dga WS on observer page: {url[:60]}")
        except Exception:
            pass

    def _maybe_dom_result_probe(self) -> None:
        """READ-ONLY, env-gated DOM probe (BACOPY_DOM_RESULT_PROBE=1). Dumps the
        multibaccarat tile roadmap structure so we can design a reliable ordered
        result source from the betting page's OWN DOM (single session, no 2nd
        Pragmatic page). Off by default → v3 path byte-identical. Throttled +
        capped like the prior DGA-DIAG probe; performs NO clicks and changes no
        betting behaviour. MUST be called from the Playwright main-loop thread.
        """
        if os.getenv("BACOPY_DOM_RESULT_PROBE", "").strip().lower() not in ("1", "true", "on", "yes"):
            return
        now = time.time()
        if now - getattr(self, "_dom_probe_at", 0.0) < 20.0:
            return
        self._dom_probe_at = now
        if getattr(self, "_dom_probe_count", 0) >= 8:
            return
        try:
            frame = self._find_pragmatic_frame()
        except Exception as e:
            logger.info(f"[DOM-PROBE] frame lookup error: {e}")
            return
        if frame is None:
            logger.info("[DOM-PROBE] no multibaccarat frame yet")
            return
        try:
            res = frame.evaluate(_DOM_RESULT_PROBE_JS)
        except Exception as e:
            logger.warning(f"[DOM-PROBE] evaluate error: {e}")
            return
        self._dom_probe_count = getattr(self, "_dom_probe_count", 0) + 1
        try:
            tiles = len(res) if isinstance(res, list) else 0
            logger.info(
                f"[DOM-PROBE] #{self._dom_probe_count} tiles={tiles} "
                f"{json.dumps(res, ensure_ascii=False)[:3600]}"
            )
        except Exception:
            logger.info(f"[DOM-PROBE] #{self._dom_probe_count} {str(res)[:3000]}")

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
            # The local-signal result source is the dedicated DIRECT dga WS
            # (_dga_direct_loop), the SINGLE writer into _on_dga_frame. We do NOT
            # also forward the betting page's /dga here: a 2nd writer thread would
            # race on the per-table sequence dicts (GIL doesn't make get+append
            # atomic). Forward only in legacy-observer mode for backward compat.
            cb = getattr(self, "_dga_result_callback", None)
            if cb is not None and os.getenv("BACOPY_DGA_LEGACY_OBSERVER", "").strip().lower() in ("1", "true", "on"):
                def _dga_forward(f, _cb=cb):
                    try:
                        body = f if isinstance(f, str) else (getattr(f, "body", "") or "")
                    except Exception:
                        body = ""
                    if body:
                        try:
                            _cb(body)
                        except Exception as _e:
                            logger.debug(f"[DGA-FWD] callback error: {_e}")
                ws.on("framereceived", _dga_forward)
                logger.info("[WS-FILTER] /dga forwarded to local-signal callback (legacy)")
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
                        # Always refresh liveness so the multi-WS watchdog keeps
                        # seeing recent betsopen activity on the channel-host state.
                        st["last_bets_open_at"] = now
                        # Only adopt this game_id onto the channel-host state when the
                        # event carries no per-table id, or it belongs to the channel
                        # host itself. In multi-play, ALL tables' betsopen events flow
                        # through one channel (msg_tid), and the channel host is itself
                        # a real betting table. Writing every foreign table's game_id
                        # here clobbered the host's bets_open_game_id and caused
                        # game_id_mismatch when betting on the channel-host table.
                        if (not bo_table_id) or (bo_table_id == msg_tid):
                            st["bets_open_game_id"] = gid
                    if bo_table_id:
                        self._saw_per_table_betsopen = True
                        # betsopen に個別テーブル ID があれば per-table state も更新
                        per_st = self._ensure_table_state(bo_table_id)
                        per_st["bets_open_game_id"] = gid
                        per_st["last_bets_open_at"] = now
                        per_st.setdefault("multi_channel", msg_tid)
                        if self._game_ws_url and not per_st.get("ws_url"):
                            per_st["ws_url"] = self._game_ws_url
                        self._game_to_table_id[gid] = bo_table_id
                        if len(self._game_to_table_id) > 600:
                            for old_gid in list(self._game_to_table_id.keys())[:200]:
                                self._game_to_table_id.pop(old_gid, None)
                    if (not self._multi_lobby_mode) and gid != self._bets_open_game_id:
                        self._bets_open_game_id = gid
                        self._last_bets_open_at = now
                    logger.info(
                        f"[BETSOPEN] table={msg_tid} game={gid} bo_table={bo_table_id!r} "
                        f"bo_keys={list(bo.keys())} pending_bet={bool(self._pending_bet)}"
                    )
                    # ── Manual-assist: auto-clear the NOW (red/blue) overlay when the
                    # locked hand ends. Speed/multiplay tables never deliver a winner-
                    # bearing hand to the bot's _on_new_hand, so betsopen gameId
                    # advancement on the locked table is the only reliable hand-end
                    # signal. The bet hand = the operator's lpbet gid captured after the
                    # lock (else the first betsopen seen after the lock); once a DIFFERENT
                    # gameId opens, that hand resolved → ask the bot to release the lock.
                    # Visual only — the money/SEQ progression stays WIN/LOSE-driven.
                    try:
                        _blk = self._bot_now_lock or {}
                        _lk_tid = str(_blk.get("table_id") or "")
                        _bo_eff = bo_table_id or msg_tid
                        if _lk_tid and _bo_eff and _lk_tid == _bo_eff:
                            _set_at = float(_blk.get("set_at") or 0.0)
                            _bet_gid = str(_blk.get("bet_gid") or "")
                            if not _bet_gid:
                                if self._last_lpbet_gid and self._last_lpbet_at >= _set_at:
                                    _bet_gid = str(self._last_lpbet_gid)
                                else:
                                    _bet_gid = gid
                                _blk["bet_gid"] = _bet_gid
                                logger.info(
                                    f"[NOW-LOCK] bet-hand gid={_bet_gid} table={_lk_tid} "
                                    f"lpbet={self._last_lpbet_gid or '-'}"
                                )
                            elif gid != _bet_gid and (now - _set_at) > 2.0:
                                logger.info(
                                    f"[NOW-LOCK] hand-end via betsopen table={_lk_tid} "
                                    f"bet_gid={_bet_gid} new_gid={gid}"
                                )
                                # Clear the mirror first so a rapid follow-up betsopen
                                # cannot re-fire before the bot processes the release.
                                self._bot_now_lock = {}
                                _cb = self._now_lock_release_cb
                                if callable(_cb):
                                    try:
                                        _cb(str(_blk.get("decision_id") or ""), _lk_tid, gid)
                                    except Exception as _e:
                                        logger.debug(f"[NOW-LOCK] release cb failed: {_e}")
                    except Exception as _ex:
                        logger.debug(f"[NOW-LOCK] betsopen hand-end check failed: {_ex}")
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
                                # betsopen に個別 ID あり → 完全一致。
                                # Click mode must validate/click against the actual tile table id.
                                # WS mode still uses the multi-channel as the send channel.
                                ws_channel = msg_tid if is_multi_ws else effective_table
                                exec_table = bo_table_id if self._multi_bet_transport == "click" else ws_channel
                                logger.info(
                                    f"[BETSOPEN-HIT] per-table match: {bo_table_id} game={gid} "
                                    f"ws_channel={ws_channel} exec_table={exec_table}"
                                )
                                self._sync_active_from_table(exec_table)
                                self._try_execute_bet(gid, table_id=exec_table)
                            elif bo_table_id and pending_target != bo_table_id:
                                # send_bet is called from the API polling thread, so it cannot
                                # always click immediately even when the target table is already
                                # open for bets. While other tables keep emitting betsopen, use
                                # the still-open target window instead of waiting for the next
                                # target-specific betsopen cycle.
                                target_st = self._table_states.get(pending_target) or {}
                                target_gid = str(target_st.get("bets_open_game_id") or "")
                                if target_gid and self._is_table_bet_window_open(pending_target):
                                    logger.info(
                                        f"[BETSOPEN-PRIORITY] mismatch arrived={bo_table_id} but pending target "
                                        f"is already open: target={pending_target} game={target_gid}"
                                    )
                                    self._sync_active_from_table(pending_target)
                                    self._try_execute_bet(target_gid, table_id=pending_target)
                                else:
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
                    closed_table_id = str(self._game_to_table_id.get(gid) or "")
                    if closed_table_id:
                        closed_st = self._ensure_table_state(closed_table_id)
                        closed_st["bets_closed_game_id"] = gid
                    if (not self._multi_lobby_mode) or (msg_tid and msg_tid == self._table_id):
                        self._bets_closed_game_id = gid
                    logger.info(f"[BETSCLOSED] table={msg_tid} game={gid}")
                return

            bet_obj = obj.get("bet")
            if isinstance(bet_obj, dict):
                self._last_game_bet_confirm = dict(bet_obj)
                self._last_game_bet_confirm_at = time.time()
                logger.info(
                    f"[GAME-BET-CONFIRM] table={msg_tid or effective_tid or '-'} "
                    f"game={bet_obj.get('game') or bet_obj.get('gameId') or '-'} "
                    f"bc={bet_obj.get('bc') or bet_obj.get('betcode') or '-'} "
                    f"amount={bet_obj.get('amount') or '-'}"
                )
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
        _raw = repr(data[:400]) if data else "(empty)"
        logger.info(f"[WS-SENT-RAW] tid={ws_table_id} len={len(data) if data else 0} raw={_raw}")
        if not data:
            return
        # <lpbet> 検出: Pragmatic game client がクリック受理後に自動送信する BET確定メッセージ
        if "<lpbet" in data:
            m_gid = re.search(r'gId="([^"]+)"', data)
            gid = str(m_gid.group(1) if m_gid else "").strip()
            self._last_lpbet_gid = gid
            self._last_lpbet_at = time.time()
            if gid:
                # 1チップ着弾ごとに1回。多チップBETの満額検証に使う。
                self._lpbet_count_by_gid[gid] = self._lpbet_count_by_gid.get(gid, 0) + 1
            logger.info(f"[LPBET-CONFIRM] WS lpbet detected: gId={gid!r} table={ws_table_id!r}")
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

    def _maybe_fire_ws_test_bet(self, now: float) -> None:
        """検証専用ワンショット: BACOPY_WS_BET_TEST_ONCE=1 のとき、開窓中の卓へ
        $1(既定) のWS BETを1回だけ送る。money/SEQ モデルには一切触らず、send_bet の
        transport ゲートも経由せず _ws_send を直接叩く(= Stake が WS lpbet を受理して
        残高が動くかの素の検証)。1回撃ったら自動で武装解除する。"""
        if str(os.getenv("BACOPY_WS_BET_TEST_ONCE", "")).strip().lower() not in ("1", "true", "yes", "on"):
            return
        if getattr(self, "_ws_test_fired", False):
            return
        host = "cbcf6qas8fscb222"
        cand = None
        for tid, st in list(self._table_states.items()):
            if tid == host:
                continue
            gid = str(st.get("bets_open_game_id") or "")
            closed = str(st.get("bets_closed_game_id") or "")
            age = now - float(st.get("last_bets_open_at") or 0.0)
            if gid and gid != closed and 0.0 < age < 6.0 and st.get("ws_url"):
                cand = (tid, gid, st)
                break
        if not cand:
            return
        tile, gid, st = cand
        uid = str(self._user_id or st.get("user_id") or "")
        if not uid:
            logger.warning("[WS-BET-TEST] no user_id yet; will retry next tick")
            return
        self._ws_test_fired = True
        amount = float(os.getenv("BACOPY_WS_BET_TEST_AMT", "1") or 1)
        side = (os.getenv("BACOPY_WS_BET_TEST_SIDE", "P") or "P").strip().upper()
        bc = "B" if side in ("B", "BANKER") else "P"
        xml = _build_lpbet_xml(table_id=tile, game_id=gid, user_id=uid, bc=bc, amount=amount)
        try:
            before = dict(self._stake_balance_by_currency)
        except Exception:
            before = {}
        logger.info(
            f"[WS-BET-TEST] FIRING one-shot WS bet table={tile} gid={gid} side={bc} "
            f"amt=${amount} uid=...{uid[-6:]} balance_before={before} xml={xml!r}"
        )
        try:
            res = self._ws_send(tile, xml)
        except Exception as ex:
            logger.warning(f"[WS-BET-TEST] _ws_send raised: {ex}")
            res = {"ok": False, "reason": f"exception:{ex}"}
        logger.info(f"[WS-BET-TEST] _ws_send result={res}")

    def tick(self) -> None:
        now = time.time()
        page = self._lobby_page
        try:
            self._maybe_fire_ws_test_bet(now)
        except Exception as ex:
            logger.debug(f"[WS-BET-TEST] hook failed: {ex}")

        if self._multi_lobby_mode and page is not None:
            try:
                cur_url = str(getattr(page, "url", "") or "")
            except Exception:
                cur_url = ""
            wrong_page = (
                cur_url
                and "stake.com" in cur_url
                and "pragmatic-play-live-lobby-baccarat" not in cur_url
            )
            if wrong_page and now - self._last_lobby_recover_at >= 8.0:
                self._recover_pragmatic_lobby("wrong_page", force=True)

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

        if (
            self._multi_diagnostic_only
            and self._diag_probe_qpid
            and not self._diag_probe_queued
            and self._is_multi_table_ws
            and self._diag_probe_qpid in self._table_states
        ):
            self._diag_probe_queued = True
            logger.info(
                f"[ML-DIAG-ONESHOT] queued table={self._diag_probe_qpid} "
                f"name={self._diag_probe_name or '-'} side={self._diag_probe_side}"
            )
            self._request_switch(
                self._diag_probe_qpid,
                self._diag_probe_name or self._diag_probe_qpid,
                self._diag_probe_qpid,
                intent="preposition",
                side=self._diag_probe_side,
            )

        # A live decision can arrive while a decision switch request is queued.
        # If the exact target table already has an open BET window, spend the
        # owner-thread tick on the BET first; searching/focusing first can consume
        # the whole signal budget.
        if (
            self._multi_lobby_mode
            and self._multi_bet_transport == "click"
            and self._pending_bet
            and not self._switch_in_progress
            and not self._bet_send_in_progress
        ):
            try:
                with self._lock:
                    pending_target_first = str((self._pending_bet or {}).get("table_id") or "")
                if pending_target_first and self._is_table_bet_window_open(pending_target_first):
                    req = self._switch_request or {}
                    req_target = str(req.get("qpid") or req.get("table_id") or "")
                    req_intent = str(req.get("intent") or "").lower()
                    st_first = self._table_states.get(pending_target_first) or {}
                    gid_first = str(st_first.get("bets_open_game_id") or "")
                    target_ready = (
                        self._prepared_table_id == pending_target_first
                        or self.is_table_in_antenna_zone(
                            pending_target_first,
                            str((self._pending_bet or {}).get("side") or ""),
                        )
                    )
                    if gid_first and target_ready:
                        if req_target == pending_target_first and req_intent in ("decision", "prepare", "preposition"):
                            logger.info(
                                f"[TICK] clear queued {req_intent} switch because target is ready and exact BET window is open: "
                                f"table={pending_target_first}"
                            )
                            self._switch_request = None
                        logger.info(
                            f"[TICK] owner-thread priority BET before switch: "
                            f"table={pending_target_first} game={gid_first}"
                        )
                        self._try_execute_bet(gid_first, table_id=pending_target_first)
                    elif gid_first:
                        logger.info(
                            f"[TICK] exact BET window open but target not ready; keep switch first: "
                            f"table={pending_target_first} prepared={self._prepared_table_id or '-'} "
                            f"queued={req_intent or '-'}"
                        )
            except Exception as ex:
                logger.warning(f"[TICK] priority pending BET failed: {ex}")

        # switch 要求があれば先に処理（テーブル入場）
        if self._switch_request and self._switch_in_progress:
            req = self._switch_request
            logger.warning(
                f"[TICK-DIAG] switch_request blocked by in_progress: "
                f"target={req.get('qpid') or req.get('table_id')} "
                f"intent={req.get('intent')} active={self._active_switch_request}"
            )

        if self._switch_request and not self._switch_in_progress:
            req = self._switch_request
            logger.info(f"[TICK] processing switch_request: intent={req.get('intent')} table={req.get('table_id')} pending_bet={bool(self._pending_bet)}")
            self._switch_request = None
            self._switch_in_progress = True
            self._active_switch_request = dict(req)
            try:
                _sw0 = time.time()  # 計装
                self._perform_switch(req)
                _swdt = time.time() - _sw0
                if _swdt >= 2.0:
                    logger.warning(f"[TICK-SLOW] _perform_switch {_swdt:.1f}s intent={req.get('intent')}")
            except Exception as e:
                logger.warning(f"[TICK] switch failed: {e}")
            finally:
                self._active_switch_request = None
                self._switch_in_progress = False

        # send_bet は API polling thread から呼ばれるため、即時実行は owner thread の tick で拾い直す。
        if (
            self._multi_lobby_mode
            and self._multi_bet_transport == "click"
            and not self._switch_in_progress
            and not self._bet_send_in_progress
        ):
            try:
                with self._lock:
                    pending_target = str((self._pending_bet or {}).get("table_id") or "")
                if pending_target and self._prepared_table_id == pending_target and self._is_table_bet_window_open(pending_target):
                    st = self._table_states.get(pending_target) or {}
                    gid = str(st.get("bets_open_game_id") or "")
                    if gid:
                        logger.info(
                            f"[TICK] owner-thread resume pending BET: "
                            f"table={pending_target} game={gid}"
                        )
                        self._try_execute_bet(gid, table_id=pending_target)
                elif pending_target and self.is_table_in_antenna_zone(
                    pending_target,
                    str((self._pending_bet or {}).get("side") or ""),
                ) and self._is_table_bet_window_open(pending_target):
                    st = self._table_states.get(pending_target) or {}
                    gid = str(st.get("bets_open_game_id") or "")
                    if gid:
                        logger.info(
                            f"[TICK] antenna-zone resume pending BET: "
                            f"table={pending_target} prepared={self._prepared_table_id} game={gid}"
                        )
                        self._try_execute_bet(gid, table_id=pending_target)
            except Exception as ex:
                logger.warning(f"[TICK] owner-thread pending resume failed: {ex}")

        if self._multi_diagnostic_only and self._prepared_table_id:
            prepared_tid = self._prepared_table_id
            prepared_st = self._table_states.get(prepared_tid) or {}
            open_gid = str(prepared_st.get("bets_open_game_id") or "")
            if open_gid and open_gid != self._diag_last_open_probe_gid:
                self._diag_last_open_probe_gid = open_gid
                _pbf0 = time.time()  # 計装
                probe_frame = self._find_pragmatic_frame(target_qpid=prepared_tid)
                if probe_frame is not None:
                    side_class = {"P": "ym_yP", "B": "ym_yQ"}[self._diag_prepared_side]
                    self._hover_multi_tile(probe_frame, prepared_tid)
                    try:
                        _pbe0 = time.time()
                        open_probe = probe_frame.evaluate(
                            self._JS_BET_COORDS,
                                {
                                    "qpid": prepared_tid,
                                    "tableName": self._diag_probe_name or prepared_tid,
                                    "sideClass": side_class,
                                    "sideCode": self._diag_prepared_side,
                                },
                        )
                        _pbedt = time.time() - _pbe0  # 計装
                        _pbfdt = _pbe0 - _pbf0
                        if _pbfdt >= 2.0:
                            logger.warning(f"[TICK-SLOW] _find_pragmatic_frame(probe) {_pbfdt:.1f}s")
                        if _pbedt >= 2.0:
                            logger.warning(f"[TICK-SLOW] probe.evaluate(JS_BET_COORDS) {_pbedt:.1f}s")
                        logger.info(
                            f"[ML-DIAG-OPEN-PROBE] table={prepared_tid} gid={open_gid} "
                            f"side={self._diag_prepared_side} coords={open_probe}"
                        )
                        if not (isinstance(open_probe, dict) and open_probe.get("ok")):
                            reseek = probe_frame.evaluate(
                                _MULTI_LOBBY_FOCUS_JS,
                                {
                                    "qpid": prepared_tid,
                                    "click": False,
                                    "maxScroll": int(os.getenv("BACOPY_MULTI_SCROLL_MAX", "48") or "48"),
                                    "candidates": [self._diag_probe_name or prepared_tid, prepared_tid],
                                    "hintIndex": -1,
                                    "hintTotal": 0,
                                },
                            )
                            logger.info(
                                f"[ML-DIAG-OPEN-RESEEK] table={prepared_tid} gid={open_gid} result={reseek}"
                            )
                            if isinstance(reseek, dict) and reseek.get("found"):
                                self._hover_multi_tile(probe_frame, prepared_tid)
                                open_probe = probe_frame.evaluate(
                                    self._JS_BET_COORDS,
                                    {
                                        "qpid": prepared_tid,
                                        "tableName": self._diag_probe_name or prepared_tid,
                                        "sideClass": side_class,
                                        "sideCode": self._diag_prepared_side,
                                    },
                                )
                                logger.info(
                                    f"[ML-DIAG-OPEN-REPROBE] table={prepared_tid} gid={open_gid} "
                                    f"side={self._diag_prepared_side} coords={open_probe}"
                                )
                            if not (isinstance(open_probe, dict) and open_probe.get("ok")):
                                for retry_no in range(1, 9):
                                    time.sleep(0.3)
                                    reseek = probe_frame.evaluate(
                                        _MULTI_LOBBY_FOCUS_JS,
                                        {
                                            "qpid": prepared_tid,
                                            "click": False,
                                            "maxScroll": int(os.getenv("BACOPY_MULTI_SCROLL_MAX", "48") or "48"),
                                            "candidates": [self._diag_probe_name or prepared_tid, prepared_tid],
                                            "hintIndex": -1,
                                            "hintTotal": 0,
                                        },
                                    )
                                    if isinstance(reseek, dict) and reseek.get("found"):
                                        self._hover_multi_tile(probe_frame, prepared_tid)
                                        open_probe = probe_frame.evaluate(
                                            self._JS_BET_COORDS,
                                            {
                                                "qpid": prepared_tid,
                                                "tableName": self._diag_probe_name or prepared_tid,
                                                "sideClass": side_class,
                                                "sideCode": self._diag_prepared_side,
                                            },
                                        )
                                    ok_now = bool(isinstance(open_probe, dict) and open_probe.get("ok"))
                                    logger.info(
                                        f"[ML-DIAG-OPEN-RETRY] table={prepared_tid} gid={open_gid} "
                                        f"attempt={retry_no} ok={ok_now} "
                                        f"source={(open_probe or {}).get('source') if isinstance(open_probe, dict) else '-'}"
                                    )
                                    if ok_now:
                                        break
                    except Exception as ex:
                        logger.warning(
                            f"[ML-DIAG-OPEN-PROBE] failed table={prepared_tid} gid={open_gid}: {ex}"
                        )

        # pending bet が長時間残っている場合は解放（decision 詰まり防止）
        pending_age = 0.0
        pending_snapshot: dict[str, Any] = {}
        with self._lock:
            if isinstance(self._pending_bet, dict) and self._pending_bet:
                pending_age = self._bet_signal_age(self._pending_bet)
                pending_snapshot = dict(self._pending_bet)

        max_signal_age = self._max_bet_signal_age_sec(pending_snapshot)
        if pending_snapshot and pending_age > max_signal_age:
            logger.warning(
                f"[LIVE] drop stale signal pending bet "
                f"(age={pending_age:.1f}s > {max_signal_age:.1f}s, "
                f"table={pending_snapshot.get('table_id')})"
            )
            try:
                self._mark_bet_failed(
                    pending_snapshot,
                    "pending_signal_stale",
                    phase="bet_failed",
                    age=round(float(pending_age or 0.0), 3),
                    max_age=round(float(max_signal_age or 0.0), 3),
                    table_state_known=bool(
                        self._table_states.get(str(pending_snapshot.get("table_id") or "").strip())
                    ),
                )
            except Exception as ex:
                logger.debug(f"[LIVE] stale pending mark failed error: {ex}")
            try:
                self._clear_active_now_bet_hold(
                    bet_id=str(pending_snapshot.get("bet_id") or ""),
                    table_id=str(pending_snapshot.get("table_id") or ""),
                    decision_id=str(pending_snapshot.get("decision_id") or ""),
                    reason="pending_signal_stale",
                )
            except Exception:
                pass
            with self._lock:
                if isinstance(self._pending_bet, dict) and self._pending_bet:
                    self._pending_bet = None
            self._switch_request = None
            self._phase = "waiting"
            self._notify("⚠️ BET予約を破棄\nシグナルから時間が経過したためスキップ")
            pending_snapshot = {}
            pending_age = 0.0

        # multi-lobby: pending卓のWSが開かないケースでは、一定時間後に1回だけ click fallback を試す
        if self._multi_lobby_mode and pending_snapshot:
            try:
                target = str(pending_snapshot.get("table_id") or "").strip()
                attempted = bool(pending_snapshot.get("_prepare_attempted"))
                fallback_sec = float(os.getenv("BACOPY_MULTI_PREPARE_FALLBACK_SEC", "30") or 30)
                st = self._table_states.get(target) or {}
                target_last_open = float(st.get("last_bets_open_at") or 0.0)
                if self._is_multi_table_ws and not self._saw_per_table_betsopen and not target_last_open:
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
                if self._is_multi_table_ws and not self._saw_per_table_betsopen and not target_last_open:
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
                    os.getenv("BACOPY_PENDING_BET_MISMATCH_CLEAR_SEC", "45") or 45
                )
                if bool(pending_snapshot.get("_fallback_join_attempted")):
                    mismatch_clear_sec = max(
                        mismatch_clear_sec,
                        float(os.getenv("BACOPY_PENDING_BET_MISMATCH_CLEAR_AFTER_JOIN_SEC", "60") or 60),
                    )
                st = self._table_states.get(target) or {}
                target_last_open = float(st.get("last_bets_open_at") or 0.0)
                # マルチテーブル WS モードでは betsopen は multi-channel 側にしか来ない
                if self._is_multi_table_ws and not self._saw_per_table_betsopen and not target_last_open:
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

        max_pending_age = float(os.getenv("BACOPY_PENDING_BET_MAX_SEC", "20") or 20)
        if pending_age > max_pending_age:
            logger.warning(f"[LIVE] drop stuck pending bet (age={pending_age:.1f}s)")
            with self._lock:
                self._pending_bet = None
            self._switch_request = None
            self._phase = "waiting"
            self._notify("⚠️ BET予約を破棄\n送信待機が長すぎたためスキップ")

        bif_clear_sec = float(os.getenv("BACOPY_BIF_STUCK_CLEAR_SEC", "180") or 180)
        with self._lock:
            bif_stuck = bool(self._sent_bet_ids) and not self._bet_send_in_progress and not self._pending_bet
            last_sent = self._last_bet_sent_at
        if bif_stuck and last_sent > 0 and (now - last_sent) > bif_clear_sec:
            logger.warning(
                f"[TICK] BIF stuck clear: sent_bet_ids stale {now - last_sent:.0f}s > {bif_clear_sec}s"
            )
            with self._lock:
                self._sent_bet_ids.clear()
                self._confirmed_bets.clear()

        if self._multi_lobby_mode:
            try:
                _ema0 = time.time()  # 計装: tick内重DOM操作の特定
                self._ensure_multi_area()
                _emadt = time.time() - _ema0
                if _emadt >= 2.0:
                    logger.warning(f"[TICK-SLOW] _ensure_multi_area {_emadt:.1f}s")
            except Exception:
                pass
            try:
                self._maintain_active_now_bet_hold(now)
            except Exception as ex:
                logger.debug(f"[NOW-BET-HOLD] maintain failed: {ex}")
            try:
                self._maintain_visible_bet_hold(now)
            except Exception as ex:
                logger.debug(f"[VISIBLE-HOLD] maintain failed: {ex}")

        # 5秒ごとにブリッジを再注入（フレーム遷移・新フレームに備える）
        if now - self._last_bridge_inject > 5.0:
            self._last_bridge_inject = now
            _inj0 = time.time()  # 計装
            if page:
                self._inject_all(page)
            try:
                for p in (self._context.pages or []):
                    if p != page:
                        self._inject_all(p)
            except Exception:
                pass
            _injdt = time.time() - _inj0
            if _injdt >= 2.0:
                logger.warning(f"[TICK-SLOW] _inject_all {_injdt:.1f}s")

        # betsopen タイムアウト監視
        if self._multi_lobby_mode:
            max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
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
                max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
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

        # Pragmatic hard timeout: OK may be absent or unsafe. Existing live mode
        # recovers by going directly back to lobby; multi mode must re-enter the
        # multi area and discard stale WS/table state.
        if now - self._last_session_ended_check_at >= 10.0:
            self._last_session_ended_check_at = now
            try:
                from bacopy_executor_pragmatic_ws_live import _dismiss_session_ended_modal
                found = _dismiss_session_ended_modal(page, None)
                if found:
                    logger.warning("[LIVE] session-ended modal detected; reset multi state and re-enter multi area")
                    with self._lock:
                        self._pending_bet = None
                        self._sent_bet_ids.clear()
                        self._confirmed_bets.clear()
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
                    if self._prepared_table_id:
                        logger.warning(
                            f"[PREPARED-CLEAR] reason=session_ended_modal "
                            f"prev_target={self._prepared_table_id!r} "
                            f"age={time.time() - (self._prepared_at or 0.0):.1f}s"
                        )
                    self._prepared_table_id = ""
                    self._prepared_at = 0.0
                    self._table_states.clear()
                    self._game_to_table_id.clear()
                    self._saw_per_table_betsopen = False
                    self._multi_tile_snapshot = {}
                    self._multi_tile_snapshot_at = 0.0
                    self._multi_area_ready = False
                    self._last_multi_area_ensure_at = 0.0
                    try:
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    self._ensure_multi_area(force=True)
            except Exception as e:
                logger.debug(f"[LIVE] session-ended check error: {e}")

        if self._multi_lobby_mode and now - self._last_betslip_check_at >= 3.0:
            self._last_betslip_check_at = now
            try:
                rec = page.evaluate(_DISMISS_BETSLIP_SETTINGS_JS)
                if isinstance(rec, dict) and int(rec.get("closed") or 0) > 0:
                    logger.info(f"[LIVE] betslip settings modal dismissed closed={rec.get('closed')}")
            except Exception as e:
                logger.debug(f"[LIVE] betslip modal check error: {e}")

        idle_recover_interval = float(os.getenv("BACOPY_IDLE_RECOVER_CHECK_SEC", "0.5") or 0.5)
        if self._multi_lobby_mode and now - self._last_idle_recover_check_at >= max(0.2, idle_recover_interval):
            self._last_idle_recover_check_at = now
            self._auto_recover_idle_dialogs("tick")
            # セッション復旧: モーダル(長時間セッション/カスタマーサポート)を閉じた時だけ
            # ロビーへ自動再入場する。※フィード途絶ウォッチドッグは手動アシスト中に誤発動
            # (NOW/BET中にロビーへ飛ぶ)したため撤去した。トリガーはモーダル閉鎖のみ。
            try:
                self._maybe_recover_session_lobby(now)
            except Exception as ex:
                logger.debug(f"[SESSION-RECOVER] maybe failed: {ex}")

        if (
            self._multi_lobby_mode
            and self._manual_assist_watch_until > now
            and now - self._last_manual_assist_recover_at >= 0.35
        ):
            self._last_manual_assist_recover_at = now
            hits = self._auto_recover_idle_dialogs("manual-assist-watch")
            if hits:
                logger.info(
                    f"[MANUAL-ASSIST-WATCH] recovered dialogs target={self._manual_assist_watch_target or '-'} "
                    f"hits={hits} remaining={self._manual_assist_watch_until - now:.1f}s"
                )

        try:
            self._maintain_assist_focus_hold(now)
        except Exception as ex:
            logger.debug(f"[ASSIST-HOLD] maintain failed: {ex}")
        try:
            self._maintain_active_now_bet_hold(now)
        except Exception as ex:
            logger.debug(f"[NOW-BET-HOLD] maintain failed: {ex}")

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
                    self._recover_pragmatic_lobby("reconnecting_stuck", force=True)
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
        # 機能①拡張(HOLD): スクロール凍結中は、HOLD直前に既にキュー済の
        # スキャン系switch(予告/準備/手動アシスト)も「実行段階」で捨てる。
        # enqueueゲート(_request_switch)だけだと、凍結する直前にキューへ入った
        # 予告がそのまま処理され、稀に黄色枠が動いてしまう取りこぼしがあった。
        # 実BET(decision/bet/fallback_join)は止めない。
        if getattr(self, "_scroll_frozen", False) and intent in ("preposition", "prepare", "manual_assist"):
            logger.info(f"[ASSIST-HOLD] scroll-frozen: drop queued switch at perform intent={intent}")
            return
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
            target = str(qpid or table_id)
            bot_lock = self._bot_now_lock_active()
            lock_target = str((bot_lock or {}).get("table_id") or "")
            if (
                self._multi_lobby_mode
                and bot_lock
                and lock_target
                and target
                and target != lock_target
                and intent in ("preposition", "prepare", "manual_assist", "decision", "bet")
            ):
                logger.info(
                    f"[BOT-NOW-LOCK] abort active switch before focus: "
                    f"intent={intent} target={target} keep={lock_target} "
                    f"did={str(bot_lock.get('decision_id') or '-')[:12]}"
                )
                return
            # 同一卓への再FOCUSスキップ: 既に準備済みの卓へ予告(score更新3→2→1)や
            # manual_assistで再切替すると、FOCUS(重DOM)が5-15秒freezeしてbot全体を止め、
            # 決済/追従/NOWを妨げる(赤枠stuckの真因)。WS BETにFOCUSは不要なので既に居る卓は
            # スキップ。env BACOPY_SKIP_REFOCUS_PREPARED=0 で従来挙動に戻せる。
            if (
                intent in ("preposition", "manual_assist")
                and target
                and str(getattr(self, "_prepared_table_id", "") or "") == target
                and os.getenv("BACOPY_SKIP_REFOCUS_PREPARED", "1").strip() != "0"
            ):
                logger.info(f"[SWITCH] skip re-focus: already prepared target={target} intent={intent}")
                if intent in ("preposition", "manual_assist", "prepare", "decision"):
                    self._start_assist_focus_hold(target, table_name or target, intent=intent)
                return
            # WS-BET最適化: WS送信モードでは予告/手動アシストの卓FOCUS(重DOM)は新卓への
            # "初回"であっても不要(BETは共有ホストWSから全卓へ送れる・チップ選択も不要)。
            # 初回FOCUSの5-19s freezeがメインループを塞ぎ、その間に届いた勝敗の決済処理が
            # 遅れて「枠が残ったまま→遅れて消えて光る」になっていた。視覚(タイル中央寄せ
            # +枠描画)は軽量な assist-focus-hold(_center_multi_tile)が引き続き担う。
            # env BACOPY_WS_SKIP_FOCUS=0 で従来挙動(初回FOCUSあり)に戻せる。
            if (
                intent in ("preposition", "manual_assist")
                and self._multi_bet_transport == "ws"
                and os.getenv("BACOPY_WS_SKIP_FOCUS", "1").strip() != "0"
            ):
                logger.info(f"[SWITCH] skip focus (ws transport) target={target} intent={intent}")
                self._start_assist_focus_hold(target, table_name or target, intent=intent)
                return
            ok = self._focus_table_in_multi(req)
            logger.info(f"[SWITCH] _focus_table_in_multi result={ok}")
            if not ok and intent == "preposition" and target:
                cooldown = float(os.getenv("BACOPY_PREPOSITION_FOCUS_FAIL_COOLDOWN_SEC", "10") or 10)
                self._preposition_focus_block_until[target] = time.time() + max(5.0, cooldown)
                logger.info(
                    f"[SWITCH] preposition focus cooldown target={target} "
                    f"sec={max(5.0, cooldown):.1f}"
                )
            elif ok and target:
                self._preposition_focus_block_until.pop(target, None)
                if intent in ("preposition", "manual_assist", "prepare", "decision"):
                    self._start_assist_focus_hold(target, table_name or target, intent=intent)
            if ok and self._switch_target_table_id:
                self._switch_target_table_id = ""
            if ok and target and self._prepared_table_id == target:
                with self._lock:
                    pending_target = str((self._pending_bet or {}).get("table_id") or "")
                    pending_amount = float((self._pending_bet or {}).get("amount") or 0.0)
                preselect_amount = float(req.get("preselect_amount") or req.get("amount") or 0.0)
                bot_lock = self._bot_now_lock_active()
                lock_target = str((bot_lock or {}).get("table_id") or "")
                if (
                    self._multi_lobby_mode
                    and bot_lock
                    and pending_target
                    and lock_target
                    and pending_target != lock_target
                ):
                    logger.info(
                        f"[BOT-NOW-LOCK] discard stale pending bet during NOW lock: "
                        f"pending_table={pending_target} lock_table={lock_target}"
                    )
                    with self._lock:
                        self._pending_bet = None
                    pending_target = ""
                    pending_amount = 0.0
                if pending_target == target and self._is_table_bet_window_open(target):
                    st = self._table_states.get(target) or {}
                    gid = str(st.get("bets_open_game_id") or "")
                    if gid:
                        logger.info(f"[SWITCH] prepared target has open BET window; resume pending BET table={target}")
                        logger.info(
                            f"[AUTO-PROBE] final_click allowed=true reason=pending_bet_window_open "
                            f"target={target} game_id={gid} planned=${pending_amount:.2f}"
                        )
                        self._try_execute_bet(gid, table_id=target)
                elif pending_target == target and pending_amount > 0:
                    logger.info(
                        f"[AUTO-PROBE] preselect source=pending target={target} "
                        f"planned=${pending_amount:.2f} intent={intent}"
                    )
                    self._preselect_first_chip(target, pending_amount)
                elif intent in ("preposition", "manual_assist") and preselect_amount > 0:
                    logger.info(
                        f"[AUTO-PROBE] preselect source={intent} target={target} "
                        f"planned=${preselect_amount:.2f}"
                    )
                    self._preselect_first_chip(target, preselect_amount)
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

    def _auto_recover_idle_dialogs(self, reason: str = "tick", include_frames: bool = True) -> int:
        """Dismiss transient Stake/Pragmatic dialogs such as contact-support OK modals."""
        if not self._multi_lobby_mode:
            return 0
        pages: list[Any] = []
        for p in [self._bet_page, self._lobby_page]:
            if p is not None and p not in pages:
                pages.append(p)
        try:
            for p in (self._context.pages or []):
                if p is not None and p not in pages:
                    pages.append(p)
        except Exception:
            pass
        if not pages:
            return 0
        total = 0
        for page in pages:
            try:
                rec = page.evaluate(_AUTO_RECOVER_IDLE_JS)
                if isinstance(rec, dict):
                    hits = int(rec.get("clicked") or 0) + int(rec.get("fallbackClicked") or 0)
                    if hits:
                        total += hits
                        logger.info(
                            f"[LIVE] idle/support dialog auto-recovered (page/{reason}) "
                            f"url={getattr(page, 'url', '?')[:80]} "
                            f"clicks={rec.get('clicked')} fallback={rec.get('fallbackClicked')}"
                        )
                        # ダイアログ dismiss 後 = ゲームがリロードする可能性が高い。
                        # NOW-BET-HOLD と ASSIST-HOLD の再センタリングタイマーをリセットして
                        # リロード完了後できるだけ早くタイルを中央に戻す。
                        self._last_active_now_bet_center_at = 0.0
                        self._last_assist_focus_center_at = 0.0
            except Exception as e:
                logger.debug(f"[LIVE] idle recover page error ({reason}): {e}")
            if include_frames:
                try:
                    for frame in (page.frames or []):
                        try:
                            rec = frame.evaluate(_AUTO_RECOVER_IDLE_JS)
                            if isinstance(rec, dict):
                                hits = int(rec.get("clicked") or 0) + int(rec.get("fallbackClicked") or 0)
                                if hits:
                                    total += hits
                                    logger.info(
                                        f"[LIVE] idle/support dialog auto-recovered (frame/{reason}) "
                                        f"url={getattr(frame, 'url', '?')[:80]} "
                                        f"clicks={rec.get('clicked')} fallback={rec.get('fallbackClicked')}"
                                    )
                                    self._last_active_now_bet_center_at = 0.0
                                    self._last_assist_focus_center_at = 0.0
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"[LIVE] idle recover frame list error ({reason}): {e}")
        if total > 0:
            # 「長時間セッション/カスタマーサポート」モーダルを閉じた = セッションが死んでいる。
            # 閉じただけでは "長時間セッションがなかった" 状態が残るので、tick の安全な文脈で
            # ロビーへ自動再入場して復旧する(_maybe_recover_session_lobby)。
            self._session_recover_pending = True
        return total

    def _maybe_recover_session_lobby(self, now: float) -> None:
        """セッション復旧: モーダル閉鎖後 or フィード途絶で予約されたら、安全な時にロビーへ
        自動再入場(page.goto)してセッションを復旧する。手動でロビーに行く手間を無くす。"""
        if not getattr(self, "_session_recover_pending", False):
            return
        # 切替/BET中は触らない(その流れが終わってから)。
        if getattr(self, "_switch_in_progress", False) or getattr(self, "_switch_request", None):
            return
        try:
            bif = getattr(self, "is_bet_in_flight", None)
            if callable(bif) and bif():
                return
        except Exception:
            pass
        cooldown = float(os.getenv("BACOPY_LOBBY_RECOVER_COOLDOWN_SEC", "45") or 45)
        if now - float(getattr(self, "_last_lobby_recover_at", 0.0) or 0.0) < cooldown:
            return
        page = self._bet_page or self._lobby_page
        if page is None:
            return
        self._last_lobby_recover_at = now
        self._session_recover_pending = False
        try:
            logger.info("[SESSION-RECOVER] re-entering lobby to restore session")
            page.goto(PRAGMATIC_BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=45000)
            # リロード後できるだけ早くタイルを中央へ戻す。
            self._last_active_now_bet_center_at = 0.0
            self._last_assist_focus_center_at = 0.0
            self._last_bets_open_at = time.time()  # 直後の feed-dead 再トリガー防止
            logger.info("[SESSION-RECOVER] lobby re-entered (session restored)")
        except Exception as e:
            logger.warning(f"[SESSION-RECOVER] lobby re-enter failed: {e}")

    def send_bet(
        self,
        side: str,
        amount: float,
        table_id: str = "",
        bet_id: str = "",
        metadata: dict | None = None,
    ) -> bool:
        """BET を予約する。次の betsopen で送信。"""
        target_table = str(table_id or self._table_id or "").strip()
        md = metadata or {}
        on_owner_thread = threading.get_ident() == self._owner_thread_id
        logger.info(f"[SEND-BET] called: side={side} amount={amount} table={target_table!r} bet_id={bet_id!r}")
        if not target_table:
            logger.warning("[SEND-BET] SKIP: empty target table (table_id and _table_id both empty)")
            return False

        # multi-lobby WS transport receives all table windows; no DOM table selection is needed.
        if self._multi_lobby_mode:
            st = self._ensure_table_state(target_table)
            if self._table_name and not st.get("table_name"):
                st["table_name"] = self._table_name
            if self._multi_bet_transport == "click" and self._prepared_table_id != target_table:
                self._request_switch(target_table, self._table_name, target_table, intent="decision")
            elif self._multi_bet_transport == "click":
                logger.info(f"[BET-QUEUED] target already prepared table={target_table}")
            else:
                logger.info(f"[BET-QUEUED] multi WS transport selected; no DOM prepare table={target_table}")
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
                "decision_id": str(md.get("decision_id") or "").strip(),
                "captured_at": str(md.get("captured_at") or "").strip(),
                "signal_game_id": str(md.get("signal_game_id") or "").strip(),
                "signal_hand_count": int(md.get("signal_hand_count") or 0),
                "seq_at_predict": str(md.get("seq_at_predict") or ""),
                # 追従/TIEプッシュのBETは前ハンド決済時に置くため、次のベット窓まで
                # 通常(20s)より長く保持する(_max_bet_signal_age_sec が参照)。
                "is_follow": bool(md.get("is_follow") or str(md.get("source") or "") == "follow"),
            }
        logger.info(f"[BET-QUEUED] side={side} ${amount:.2f} table={target_table} known_tables={list(self._table_states.keys())[:6]}")
        if self._multi_lobby_mode and self._multi_bet_transport == "click" and on_owner_thread:
            self._preselect_first_chip(target_table, amount)
        elif self._multi_lobby_mode and self._multi_bet_transport == "click":
            logger.info("[CHIP-PRESELECT] deferred to owner thread")

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
        max_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
        return 0 < age < max_age

    def _try_execute_bet(self, game_id: str, table_id: str = "") -> None:
        with self._lock:
            bet = self._pending_bet
            if not bet:
                logger.info("[TRY-BET] called but no pending_bet — skip")
                return
            self._pending_bet = None
        logger.info(f"[TRY-BET] executing: side={bet.get('side')} table={bet.get('table_id')} game_id_arg={game_id!r}")
        self._start_active_now_bet_hold(bet, str(bet.get("table_name") or self._table_name or bet.get("table_id") or ""))

        age = self._bet_signal_age(bet)
        max_age = self._max_bet_signal_age_sec(bet)
        logger.info(
            f"[TRY-BET] signal age={age:.1f}s max={max_age:.1f}s "
            f"decision={bet.get('decision_id') or '-'} signal_game={bet.get('signal_game_id') or '-'}"
        )
        if age > max_age:
            logger.warning(
                f"[TRY-BET] DROP stale bet (age={age:.1f}s > {max_age:.1f}s) "
                f"decision={bet.get('decision_id') or '-'} table={bet.get('table_id')}"
            )
            self._mark_bet_failed(
                bet,
                "signal_too_old",
                "bet_skipped_stale",
                age_sec=round(age, 2),
                max_age_sec=round(max_age, 2),
            )
            self._clear_active_now_bet_hold(bet_id=str(bet.get("bet_id") or ""), reason="signal_too_old")
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
            self._clear_active_now_bet_hold(bet_id=str(bet.get("bet_id") or ""), reason="defer_no_table")
            return
        # multi-WS: chosen_table=aggregator channel, requested_table=actual table ID
        # XML channel must use actual table ID; WS send uses aggregator channel
        if requested_table and chosen_table != requested_table:
            bet_table_id = requested_table
            logger.info(f"[TRY-BET] multi-WS: ws_channel={chosen_table} bet_table={bet_table_id}")
        else:
            bet_table_id = chosen_table
        if self._multi_lobby_mode and self._multi_bet_transport == "click":
            # 安全ゲート: マルチプレイ WS が生きていない時はクリックしない。
            # multi_ws が死んだまま page がマルチエリアを外れると、見当違いの場所を
            # クリックして誤BET/偽確認(lpbet_chrome_attach)で SEQ が進む事故になる
            # (2026-05-30 夜の incident)。bet せず failed 扱いでスキップ。env で無効化可。
            if (
                not self._is_multi_table_ws
                and os.getenv("BACOPY_REQUIRE_MULTI_WS_FOR_BET", "1").strip() != "0"
            ):
                logger.warning(
                    f"[TRY-BET] SKIP: multi-table WS not alive (multiplay lost) "
                    f"table={bet_table_id} side={side} — refuse click to avoid wrong-area bet"
                )
                self._mark_bet_failed(
                    bet, "multi_ws_down", "bet_skipped_multi_ws_down", table_id=bet_table_id
                )
                self._clear_active_now_bet_hold(
                    bet_id=str(bet.get("bet_id") or ""), reason="multi_ws_down"
                )
                try:
                    now_n = time.time()
                    last_n = float(getattr(self, "_last_multi_ws_down_notify_at", 0.0) or 0.0)
                    if now_n - last_n >= 300.0:
                        self._last_multi_ws_down_notify_at = now_n
                        self._notify(
                            "⚠️ マルチプレイ未接続\n"
                            "multi-table WS が切断中のため BET を見送っています。\n"
                            "GUI でマルチプレイ画面に復帰してください。"
                        )
                except Exception:
                    pass
                return
            prepared = str(self._prepared_table_id or "")
            active_window = self._is_table_bet_window_open(bet_table_id)
            antenna_ok = self.is_table_in_antenna_zone(bet_table_id, str(side or ""))
            if prepared != bet_table_id and not antenna_ok:
                age_before_focus = self._bet_signal_age(bet)
                max_age_before_focus = self._max_bet_signal_age_sec(bet)
                if age_before_focus <= max_age_before_focus:
                    try:
                        st_for_prepare = self._table_states.get(bet_table_id) or {}
                        logger.info(
                            f"[TRY-BET-PREPARE-NOW] exact betsopen but target not prepared; "
                            f"focus immediately table={bet_table_id} prepared={prepared or '-'} "
                            f"age={age_before_focus:.1f}s"
                        )
                        ok_focus = self._focus_table_in_multi(
                            {
                                "table_id": bet_table_id,
                                "table_name": str(st_for_prepare.get("table_name") or self._table_name or bet_table_id),
                                "qpid": bet_table_id,
                                "intent": "bet",
                                "side": str(side or "").upper(),
                            }
                        )
                        prepared = str(self._prepared_table_id or "")
                        antenna_ok = self.is_table_in_antenna_zone(bet_table_id, str(side or ""))
                        logger.info(
                            f"[TRY-BET-PREPARE-NOW] result={ok_focus} "
                            f"prepared={prepared or '-'} antenna={antenna_ok}"
                        )
                    except Exception as ex:
                        logger.warning(f"[TRY-BET-PREPARE-NOW] focus failed table={bet_table_id}: {ex}")
                else:
                    logger.info(
                        f"[TRY-BET-PREPARE-NOW] skip focus because signal is already stale: "
                        f"age={age_before_focus:.1f}s max={max_age_before_focus:.1f}s table={bet_table_id}"
                    )
            if prepared != bet_table_id and not antenna_ok:
                logger.info(
                    f"[TRY-BET-DEFER] target not prepared: target={bet_table_id} "
                    f"prepared={prepared or '-'} switching={self._switch_in_progress}"
                )
                with self._lock:
                    self._pending_bet = bet
                if not self._switch_in_progress:
                    st_for_prepare = self._table_states.get(bet_table_id) or {}
                    self._request_switch(
                        bet_table_id,
                        str(st_for_prepare.get("table_name") or self._table_name or bet_table_id),
                        bet_table_id,
                        intent="prepare",
                )
                return
            if prepared != bet_table_id and antenna_ok:
                logger.info(
                    f"[TRY-BET] antenna-zone target accepted: target={bet_table_id} "
                    f"prepared={prepared or '-'}"
                )
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

        expected_signal_game_id = str(bet.get("signal_game_id") or "").strip()
        stale_tile_game_id_ok = ""
        if self._multi_lobby_mode and self._multi_bet_transport == "click":
            latest_open_gid = str(st.get("bets_open_game_id") or "").strip()
            latest_closed_gid = str(st.get("bets_closed_game_id") or "").strip()
            latest_open_at = float(st.get("last_bets_open_at") or 0.0)
            max_open_age = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
            latest_open_active = bool(
                latest_open_gid
                and latest_open_gid != latest_closed_gid
                and latest_open_at
                and 0 < (time.time() - latest_open_at) < max_open_age
            )
            if latest_open_active and latest_closed_gid and latest_closed_gid != latest_open_gid:
                # In the shared multi-baccarat frame, the compact tile text often
                # lags one hand behind the WS betsopen event. Allow that exact
                # previous hand ID through the DOM guard; lpbet confirmation below
                # still has to match game_id before we accept the BET as sent.
                stale_tile_game_id_ok = latest_closed_gid
            if expected_signal_game_id:
                if latest_open_active and latest_open_gid == expected_signal_game_id:
                    if game_id != expected_signal_game_id:
                        logger.info(
                            f"[TRY-BET] lock game_id to signal hand: "
                            f"{game_id!r} -> {expected_signal_game_id!r} table={bet_table_id}"
                        )
                        game_id = expected_signal_game_id
                elif latest_open_active and latest_closed_gid == expected_signal_game_id:
                    logger.warning(
                        f"[TRY-BET] DROP signal hand already closed before click: "
                        f"signal_game={expected_signal_game_id!r} next_open={latest_open_gid!r} "
                        f"table={bet_table_id}"
                    )
                    self._mark_bet_failed(
                        bet,
                        "hand_id_mismatch_before_click",
                        "bet_skipped_wrong_hand",
                        signal_game_id=expected_signal_game_id,
                        latest_open_game_id=latest_open_gid,
                        latest_closed_game_id=latest_closed_gid,
                        table_id=bet_table_id,
                    )
                    self._notify(
                        "⚠️ BET SKIP\n"
                        "hand id mismatch before click\n"
                        f"{tname if 'tname' in locals() else bet_table_id}"
                    )
                    return
                else:
                    def _gid_int(v: str) -> int:
                        try:
                            return int(re.sub(r"\D+", "", str(v or "")) or "0")
                        except Exception:
                            return 0

                    latest_i = _gid_int(latest_open_gid)
                    expected_i = _gid_int(expected_signal_game_id)
                    # If the GUI has not yet seen the VPS signal hand for this table,
                    # keep the pending bet until the matching per-table betsopen arrives.
                    # Only drop when the table has already advanced past the signal hand.
                    if (not latest_open_active) or (latest_i and expected_i and latest_i < expected_i):
                        logger.info(
                            f"[TRY-BET-DEFER] waiting for signal hand: "
                            f"signal_game={expected_signal_game_id!r} latest_open={latest_open_gid!r} "
                            f"closed={latest_closed_gid!r} table={bet_table_id}"
                        )
                        with self._lock:
                            self._pending_bet = bet
                        return
                    logger.warning(
                        f"[TRY-BET] DROP wrong-hand/stale click bet: "
                        f"signal_game={expected_signal_game_id!r} latest_open={latest_open_gid!r} "
                        f"closed={latest_closed_gid!r} table={bet_table_id}"
                    )
                    self._mark_bet_failed(
                        bet,
                        "hand_id_mismatch_before_click",
                        "bet_skipped_wrong_hand",
                        signal_game_id=expected_signal_game_id,
                        latest_open_game_id=latest_open_gid,
                        latest_closed_game_id=latest_closed_gid,
                        table_id=bet_table_id,
                    )
                    self._notify(
                        "⚠️ BET SKIP\n"
                        "hand id mismatch before click\n"
                        f"{tname if 'tname' in locals() else bet_table_id}"
                    )
                    return
            elif latest_open_active and latest_open_gid != game_id:
                logger.info(
                    f"[TRY-BET] refresh game_id for prepared target: "
                    f"{game_id!r} -> {latest_open_gid!r} table={bet_table_id}"
                )
                game_id = latest_open_gid
            elif not latest_open_active:
                logger.info(
                    f"[TRY-BET-DEFER] target bet window not active before click: "
                    f"table={bet_table_id} game={game_id!r} latest_open={latest_open_gid!r} "
                    f"closed={latest_closed_gid!r}"
                )
                with self._lock:
                    self._pending_bet = bet
                return

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
        lpbet_before_at = self._last_lpbet_at
        confirm_start_at = time.time()
        before_balances = dict(self._stake_balance_by_currency)
        self._stake_balance_delta_by_currency = {}
        self._last_game_bet_confirm = {}
        self._last_game_bet_confirm_at = 0.0
        self._auto_recover_idle_dialogs("pre-bet")

        # UI buttons cannot be safely mapped to a table in the shared multi frame.
        # Default to sending the targeted lpbet message over the established multi WS proxy.
        # WS transport: bet window validity check (no click-path guards ran above).
        # Sending lpbet after bets_closed → "カスタマーにお問い合わせください" (server reject).
        if (
            self._multi_lobby_mode
            and self._multi_bet_transport == "ws"
            and (self._is_multi_table_ws or bool(self._ws_proxy_server or self._ws_proxy_servers))
        ):
            st_for_ws = self._table_states.get(bet_table_id) or self._table_states.get(chosen_table) or {}
            ws_open_gid = str(st_for_ws.get("bets_open_game_id") or game_id or "").strip()
            ws_closed_gid = str(st_for_ws.get("bets_closed_game_id") or "").strip()
            ws_open_at = float(st_for_ws.get("last_bets_open_at") or 0.0)
            max_open_age_ws = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
            ws_bet_open = bool(
                ws_open_gid
                and (not ws_closed_gid or ws_closed_gid != ws_open_gid)
                and ws_open_at
                and 0 < (time.time() - ws_open_at) < max_open_age_ws
            )
            logger.info(
                f"[TRY-BET-WS-CHECK] window_open={ws_bet_open} "
                f"open_gid={ws_open_gid or '-'} closed_gid={ws_closed_gid or '-'} "
                f"game_id={game_id or '-'} "
                f"age={time.time()-ws_open_at:.1f}s table={bet_table_id}"
            )
            if not ws_bet_open:
                logger.warning(
                    f"[TRY-BET] DROP (WS transport): bet window not open at send time "
                    f"table={bet_table_id} open={ws_open_gid or '-'} closed={ws_closed_gid or '-'}"
                )
                self._mark_bet_failed(
                    bet,
                    "bet_window_closed_before_ws_send",
                    "bet_skipped_window_closed",
                    game_id=game_id,
                    table_id=bet_table_id,
                )
                self._clear_active_now_bet_hold(bet_id=bet_id, reason="window_closed")
                return

        if self._multi_lobby_mode and self._is_multi_table_ws and self._multi_bet_transport == "click":
            self._bet_send_in_progress = True
            try:
                click_ok = self._click_place_bet(
                    side,
                    amount,
                    target_qpid=bet_table_id,
                    target_table_name=tname,
                    expected_game_id=game_id,
                    stale_tile_game_id_ok=stale_tile_game_id_ok,
                )
            finally:
                self._bet_send_in_progress = False
            result = {"ok": click_ok, "method": "click", "click_error": self._last_click_bet_error}
            ok = click_ok
            logger.info(f"[TRY-BET] click_place_bet result={result}")
            self._auto_recover_idle_dialogs("post-click")
        else:
            self._bet_send_in_progress = True
            try:
                result = self._ws_send(chosen_table, xml)
            finally:
                self._bet_send_in_progress = False
            ok = isinstance(result, dict) and result.get("ok")
            logger.info(f"[TRY-BET] _ws_send result={result}")
            self._auto_recover_idle_dialogs("post-ws")

        if ok:
            # multi-lobby modeでは「クリック成功」や「WS send OK」だけでは不十分。
            # 既存モードと同じく、Stake残高delta/drop または game WS bet confirm を
            # trusted confirmation とし、LPBET単独は未確認扱いにする。
            lpbet_confirmed = False
            trusted_confirm: dict[str, Any] | None = None
            if self._multi_lobby_mode and self._is_multi_table_ws:
                if self._multi_bet_transport == "click":
                    _lpbet_wait = float(os.getenv("BACOPY_LPBET_CONFIRM_WAIT_SEC", "55.0") or 55.0)
                    _confirm_label = "click ok"
                    _notify_label = "BETクリック実行済みだが実BET未確認"
                else:
                    _lpbet_wait = float(os.getenv("BACOPY_WS_LPBET_CONFIRM_WAIT_SEC", "70.0") or 70.0)
                    _confirm_label = "ws send ok"
                    _notify_label = "BET WS送信済みだが実BET未確認"
                _lpbet_deadline = time.time() + _lpbet_wait
                # ★高速確証(根本対策): chrome_attach では lpbet 単独で確証可
                # (require_trusted 既定0)。従来は stake_delta(不安定な口座残高WS・実測
                # 7-10s/最大704s)を待ち、それが来ると先に確証→確証が遅れ、ハンド終了時に
                # dga 勝者へ間に合わず遅い決済経路に転落→追従が1手遅れ→テレコ逆張り。
                # 自分の lpbet(game_id 一致)を観測した時点で即確証すれば、確証が lpbet 直後
                # (~0.5s)に立ち、dga 勝者へ即マッチ→決済が速い→追従が間に合う。
                _bm_pre = (
                    os.getenv("BACOPY_BROWSER", "")
                    or os.getenv("BACOPY_DUAL_LINE_BROWSER", "")
                    or ""
                ).strip().lower()
                _chrome_attach_pre = _bm_pre in ("chrome_attach", "chrome-cdp", "cdp")
                _require_trusted_pre = (
                    os.getenv("BACOPY_DUAL_REQUIRE_TRUSTED_CONFIRM", "0" if _chrome_attach_pre else "1").strip()
                    != "0"
                )
                _fast_lpbet = (
                    os.getenv("BACOPY_FAST_LPBET_CONFIRM", "1").strip() != "0"
                    and not _require_trusted_pre
                )
                while time.time() < _lpbet_deadline:
                    if self._last_lpbet_at > lpbet_before_at and (
                        not game_id or self._last_lpbet_gid == game_id
                    ):
                        lpbet_confirmed = True
                    trusted_confirm = self._trusted_bet_confirm_since(
                        start=confirm_start_at,
                        amount=amount,
                        game_id=game_id,
                        table_id=bet_table_id,
                        before_balances=before_balances,
                    )
                    if trusted_confirm:
                        if lpbet_confirmed:
                            trusted_confirm["lpbet_observed"] = True
                            trusted_confirm["lpbet_game_id"] = self._last_lpbet_gid
                        break
                    # 自分の lpbet(game_id一致)が出たら stake_delta を待たず即確証する。
                    if _fast_lpbet and lpbet_confirmed:
                        trusted_confirm = {
                            "confirm_type": "lpbet_fast",
                            "confirmed_amount": float(amount),
                            "game_id": str(game_id or ""),
                            "table_id": str(bet_table_id or ""),
                            "lpbet_observed": True,
                            "lpbet_game_id": self._last_lpbet_gid,
                        }
                        logger.info(
                            f"[LPBET-FAST] confirm via own lpbet game={game_id or '-'} "
                            f"table={bet_table_id or '-'} (no wait for stake_delta)"
                        )
                        break
                    now_wait = time.time()
                    if now_wait - self._last_bet_modal_recover_at >= 0.4:
                        self._last_bet_modal_recover_at = now_wait
                        self._auto_recover_idle_dialogs("lpbet-wait")
                    time.sleep(0.1)
                browser_mode_now = (
                    os.getenv("BACOPY_BROWSER", "")
                    or os.getenv("BACOPY_DUAL_LINE_BROWSER", "")
                    or ""
                ).strip().lower()
                chrome_attach_active = browser_mode_now in ("chrome_attach", "chrome-cdp", "cdp")
                require_trusted_default = "0" if chrome_attach_active else "1"
                require_trusted = (
                    os.getenv("BACOPY_DUAL_REQUIRE_TRUSTED_CONFIRM", require_trusted_default).strip()
                    != "0"
                )
                logger.info(
                    f"[LPBET-DECIDE] lpbet={lpbet_confirmed} trusted={bool(trusted_confirm)} "
                    f"require_trusted={require_trusted} chrome_attach={chrome_attach_active} "
                    f"game={game_id or '-'} table={bet_table_id or '-'}"
                )
                if (not trusted_confirm) and lpbet_confirmed and not require_trusted:
                    trusted_confirm = {
                        "confirm_type": (
                            "lpbet_chrome_attach" if chrome_attach_active else "lpbet_only_untrusted"
                        ),
                        "confirmed_amount": float(amount),
                        "game_id": str(game_id or ""),
                        "lpbet_observed": True,
                        "lpbet_game_id": self._last_lpbet_gid,
                    }
                if not trusted_confirm:
                    logger.warning(
                        f"[LIVE] {_confirm_label} but NO trusted bet confirmation "
                        f"(lpbet={lpbet_confirmed} game={game_id}) — skipping confirmed ledger"
                    )
                    self._mark_bet_failed(
                        bet,
                        "trusted_bet_not_confirmed" if lpbet_confirmed else "lpbet_not_confirmed",
                        "bet_unconfirmed",
                        game_id=game_id,
                        table_id=bet_table_id,
                        lpbet_observed=bool(lpbet_confirmed),
                    )
                    self._notify(f"⚠️ {_notify_label}\n{tname}\n{side_name} ${amount:.2f}")
                else:
                    logger.info(
                        f"[LIVE] bet confirmed type={trusted_confirm.get('confirm_type')} "
                        f"ck={ck} gId={game_id!r} lpbet={lpbet_confirmed}"
                    )
            else:
                lpbet_confirmed = True  # single-table legacy path
                trusted_confirm = {
                    "confirm_type": "legacy_single_table",
                    "confirmed_amount": float(amount),
                    "game_id": str(game_id or ""),
                }
                logger.info(f"[LIVE] bet sent OK ck={ck}")

            # ── Partial-bet guard (multi-chip safety) ──
            # When SEQ progresses past $1 the chip plan needs several clicks
            # (e.g. $6 = $5 + $1). If a click misses, only part of the wager
            # lands. Compare the real Stake balance delta to the planned amount
            # and warn loudly (log + Telegram) on any mismatch — this also
            # catches the dangerous case where money DID move but the bet was
            # marked unconfirmed (delta below the 90% confirm threshold).
            try:
                _observed = 0.0
                for _cur, _d in (self._stake_balance_delta_by_currency or {}).items():
                    if abs(float(_d)) > _observed:
                        _observed = abs(float(_d))
                if _observed > 0.01 and abs(_observed - float(amount)) > 0.01:
                    logger.warning(
                        f"[PARTIAL-BET] amount mismatch: planned=${float(amount):.2f} "
                        f"observed_stake_delta=${_observed:.2f} table={tname} "
                        f"side={side_name} gId={game_id!r} confirmed={bool(trusted_confirm)} "
                        f"— some chip clicks did not land"
                    )
                    try:
                        self._notify(
                            f"⚠️ 部分BET検知\n{tname}\n{side_name} 計画${float(amount):.2f} → 実際${_observed:.2f}\n"
                            f"(一部のチップが乗っていません)"
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            self._consecutive_failures = 0
            if trusted_confirm and bet_id:
                self._mark_bet_confirmed(bet, trusted_confirm)
                self._last_bet_sent_at = time.time()
                self._start_visible_bet_hold(bet_id, bet_table_id, tname)
            # lpbet WS確認済みの場合のみ「BET送信」を通知（空振りクリックは通知しない）
            if trusted_confirm:
                self._notify(
                    f"📤 BET送信確定\n{tname}\n{side_name} ${amount:.2f}\n"
                    f"confirm: {trusted_confirm.get('confirm_type')}\n(結果待ち)"
                )
        else:
            logger.error(f"[LIVE] bet send FAILED: {result}")
            failure_reason = "bet_send_failed"
            failure_phase = "bet_send_failed"
            if (
                isinstance(result, dict)
                and isinstance(result.get("click_error"), dict)
                and str((result.get("click_error") or {}).get("reason") or "") == "game_id_mismatch"
            ):
                failure_reason = "hand_id_mismatch_before_click"
                failure_phase = "bet_skipped_wrong_hand"
            self._mark_bet_failed(
                bet,
                failure_reason,
                failure_phase,
                game_id=game_id,
                table_id=bet_table_id,
                result=str(result)[:300],
            )
            self._consecutive_failures += 1
            # ブリッジ再注入して次の betsopen でリトライ
            for _p in [self._bet_page, self._lobby_page]:
                if _p:
                    try:
                        self._inject_all(_p)
                    except Exception:
                        pass
            # シグナルは1ハンド1回限り。失敗しても再キューしない（次のシグナルを待つ）
            with self._lock:
                self._pending_bet = None
            self._notify(f"⚠️ BET失敗(このハンドはスキップ)\n{tname}\n{side_name} ${amount:.2f}")
            return

        if not self._multi_lobby_mode:
            # 従来モード: BET完了後にロビーへ戻す
            try:
                page = self._bet_page
                if page is not None:
                    page.goto(
                        PRAGMATIC_BACCARAT_LOBBY_URL,
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

    def _find_pragmatic_frame(self, target_qpid: str = ""):
        """Return the frame that owns Pragmatic controls.

        Multi-baccarat is a shared frame and often does not include qpid in
        its URL. Prefer /desktop/multibaccarat over the Stake/Pragmatic lobby
        shell; otherwise focus/click probes run against a lobby page that never
        contains TileHeight-* cards.
        """
        fallback = None
        multi_fallback = None
        try:
            pages = []
            for _p in [self._lobby_page, self._bet_page] + list(self._context.pages or []):
                if _p is not None and _p not in pages:
                    pages.append(_p)
            for _p in pages:
                for _fr in [_p] + list(_p.frames or []):
                    url = str(getattr(_fr, 'url', ''))
                    is_prag = (
                        'pragmaticplaylive' in url
                        or 'qpidreoxcc.net' in url
                        or '/desktop/multibaccarat' in url
                    )
                    if not is_prag:
                        continue
                    if target_qpid and target_qpid in url:
                        return _fr  # qpid一致フレームを最優先
                    if '/desktop/multibaccarat' in url:
                        multi_fallback = _fr
                        continue
                    if fallback is None:
                        fallback = _fr
        except Exception:
            pass
        if self._multi_lobby_mode:
            # In multi-lobby mode a lobby/shell Pragmatic frame is not actionable:
            # focusing it causes not_multi_baccarat_dom and consumes the short
            # signal window. Wait for the real /desktop/multibaccarat frame.
            return multi_fallback
        return multi_fallback or fallback

    # JS は btn.click() を呼ばず、ボタンの viewport 座標を返すだけにする。
    # Python 側で page.mouse.click(x, y) を使うことで isTrusted=true の
    # 本物のマウスイベントを発火させ、Pragmatic ゲームクライアントに認識させる。
    _JS_BET_COORDS = r"""
(args) => {
  const qpid = args.qpid || '';
  const tableName = String(args.tableName || '');
  const sideClass = args.sideClass || 'ym_yP';
  const sideCode = String(args.sideCode || '').toUpperCase();
  const expectedGameId = String(args.expectedGameId || '').replace(/\D+/g, '');
  const staleTileGameIdOk = String(args.staleTileGameIdOk || '').replace(/\D+/g, '');
  const sideClasses = [sideClass];
  // Pragmatic multi-baccarat uses different class names on the compact tile
  // buttons than on the larger wager panel.
  if (sideClass === 'ym_yP') sideClasses.push('ym_yp');
  if (sideClass === 'ym_yQ') sideClasses.push('ym_yr');
  const activeClass = 'ym_yn';
  const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, '').trim();
  const tableCandidates = new Set([norm(tableName)].filter(Boolean));
  let m = tableName.match(/^\s*Baccarat\s*(\d+)\s*$/i);
  if (m) {
    tableCandidates.add(norm('バカラ ' + m[1]));
    tableCandidates.add(norm('Baccarat ' + m[1]));
  }
  m = tableName.match(/^\s*Fortune\s*(\d+)\s*Baccarat\s*$/i);
  if (m) {
    tableCandidates.add(norm('フォーチュン ' + m[1] + ' バカラ'));
    tableCandidates.add(norm('Fortune ' + m[1] + ' Baccarat'));
  }

  function rectOk(r) {
    return r && r.width > 10 && r.height > 10 && r.right > 0 && r.bottom > 0;
  }
  function tileGameId(tile) {
    if (!tile) return '';
    const text = String(tile.innerText || tile.textContent || '');
    let m = text.match(/\bID:\s*(\d{6,})\b/i);
    if (m) return m[1];
    m = text.match(/\b(\d{8,})\b/);
    return m ? m[1] : '';
  }
  function guardGame(tile, src) {
    if (!expectedGameId) return null;
    const currentGameId = tileGameId(tile);
    if (currentGameId === expectedGameId) return null;
    return {
      ok: false,
      reason: 'game_id_mismatch',
      source: src,
      expectedGameId,
      currentGameId,
      tileText: String((tile && (tile.innerText || tile.textContent)) || '').replace(/\s+/g, ' ').trim().slice(0, 260),
    };
  }
  function btnCoords(btn, wasActive, src) {
    try { btn.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
    const r = btn.getBoundingClientRect();
    if (!rectOk(r)) return null;
    // Tag the exact, game-id-validated button so the Python side can drive a
    // Playwright locator.click() on it (trusted, DOM-requeried at click time).
    // guardGame() already ran in every caller before reaching here, so this
    // marker is only ever placed on a button whose tile matches expectedGameId.
    let tagged = false;
    try {
      for (const old of document.querySelectorAll('[data-bacopy-bet-btn]')) {
        old.removeAttribute('data-bacopy-bet-btn');
      }
      btn.setAttribute('data-bacopy-bet-btn', '1');
      tagged = true;
    } catch(_) { tagged = false; }
    return {ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
            wasActive: wasActive, source: src, btnW: r.width, btnH: r.height,
            tagged: tagged};
  }
  function geometryCoords(tile, src) {
    if (!tile) return null;
    if (String(args.geometryFallback || '1') === '0') return null;
    const gameGuard = guardGame(tile, src);
    if (gameGuard) return gameGuard;
    if (!['P', 'B', 'T', 'PP', 'BP'].includes(sideCode)) return null;
    try { tile.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
    const r = tile.getBoundingClientRect();
    if (!rectOk(r)) return null;
    const xRatio = ({PP: 0.16, P: 0.34, T: 0.50, B: 0.66, BP: 0.84})[sideCode];
    if (!xRatio) return null;
    // Some compact multi-baccarat cards render the wager controls without
    // stable ym_* DOM classes. In that case the qpid-matched card is still
    // trustworthy, so use the visual 5-button row near the bottom of the card.
    const y = r.top + Math.max(r.height * 0.68, r.height - 34);
    return {
      ok: true,
      x: r.left + r.width * xRatio,
      y: Math.min(r.bottom - 12, y),
      wasActive: false,
      source: src + '_geometry',
      tileW: r.width,
      tileH: r.height,
    };
  }

  function tryOrderedTileButtons(tile, src) {
    if (!tile) return null;
    const gameGuard = guardGame(tile, src);
    if (gameGuard) return gameGuard;
    const raw = Array.from(tile.querySelectorAll('.ym_qA'))
      .filter((btn) => {
        const r = btn.getBoundingClientRect();
        return rectOk(r) && r.x >= 0 && r.y >= 0;
      })
      .map((btn) => {
        const r = btn.getBoundingClientRect();
        const txt = norm(btn.innerText || btn.textContent || '');
        const cls = String(btn.className || '');
        return {btn, x: r.left, y: r.top, w: r.width, h: r.height, txt, cls};
      })
      .sort((a, b) => (a.y - b.y) || (a.x - b.x));
    if (raw.length < 3) return null;

    function explicitRole(item) {
      const t = item.txt || '';
      const c = item.cls || '';
      // On the compact multi-baccarat grid ym_yp/ym_yr are the main
      // Player/Banker buttons. Treat them as pair bets only when the tile text
      // explicitly says pair; otherwise BANKER signals can be rejected as
      // "not_found" even though the correct button is visible.
      if (t.includes('ペア') && (t.includes('プ') || t.includes('p'))) return 'PP';
      if (t.includes('ペア') && (t.includes('バ') || t.includes('b'))) return 'BP';
      if (c.includes('ym_yp')) return 'P';
      if (c.includes('ym_yr')) return 'B';
      if (c.includes('ym_yP') || t.includes('プレイヤー') || t === 'player') return 'P';
      if (c.includes('ym_yQ') || t.includes('バンカー') || t === 'banker') return 'B';
      if (c.includes('ym_yB') || t.includes('タイ') || t === 'tie') return 'T';
      return '';
    }

    const explicit = raw.filter((item) => explicitRole(item) === sideCode);
    if (explicit.length === 1) {
      return btnCoords(explicit[0].btn, explicit[0].btn.classList.contains(activeClass), src + '_explicit');
    }

    const rows = [];
    for (const item of raw) {
      let bucket = rows.find((row) => Math.abs(row.y - item.y) < Math.max(8, item.h * 0.35));
      if (!bucket) {
        bucket = {y: item.y, items: []};
        rows.push(bucket);
      }
      bucket.items.push(item);
    }
    const row = rows
      .map((bucket) => bucket.items.sort((a, b) => a.x - b.x))
      .sort((a, b) => {
        const scoreA = (a.length >= 5 ? 100 : a.length >= 3 ? 50 : 0) + a.length;
        const scoreB = (b.length >= 5 ? 100 : b.length >= 3 ? 50 : 0) + b.length;
        return scoreB - scoreA;
      })[0] || [];
    let idx = -1;
    if (row.length >= 5) {
      // Visual order in multi-baccarat tiles:
      // P Pair, Player, Tie, Banker, B Pair.
      idx = ({PP: 0, P: 1, T: 2, B: 3, BP: 4})[sideCode] ?? -1;
    } else if (row.length >= 3) {
      // Three visible buttons can also be only side bets: P Pair / Tie / B Pair.
      // Do not map P/B to the outer buttons unless the row actually contains
      // main Player/Banker controls; this caused PLAYER bets to hit P Pair.
      const rowRoles = row.map(explicitRole);
      if ((sideCode === 'P' || sideCode === 'B') && !rowRoles.includes(sideCode)) {
        return null;
      }
      idx = ({P: 0, T: 1, B: 2})[sideCode] ?? -1;
    }
    if (idx < 0 || idx >= row.length) return null;
    return btnCoords(row[idx].btn, row[idx].btn.classList.contains(activeClass), src);
  }

  function tryWithinTile(tile, src) {
    if (!tile) return null;
    const gameGuard = guardGame(tile, src);
    if (gameGuard) return gameGuard;
    const allBtns = sideClasses.flatMap((cls) => Array.from(tile.querySelectorAll('.' + cls)));
    const visibleBtns = allBtns.filter((b) => {
      const r = b.getBoundingClientRect();
      return rectOk(r) && r.x >= 0 && r.y >= 0;
    });
    const uniq = Array.from(new Set(visibleBtns));
    if (uniq.length !== 1) return null;
    const btn = uniq[0];
    return btnCoords(btn, btn.classList.contains(activeClass), src);
  }
  function tryActivePanelAfterTargetTile(tile) {
    if (!tile) return null;
    const gameGuard = guardGame(tile, 'active_panel_after_qpid_tile');
    if (gameGuard) return gameGuard;
    // In multi-baccarat the selected tile and the actual wager panel can be
    // rendered as separate DOM islands. Once the exact qpid tile is mounted,
    // prefer the large wager-panel button. Compact tile buttons can
    // select/focus a card without placing a real wager. Some builds do not
    // mark the large button with ym_yn until after chip hover/click, so search
    // by side class first and only use activeClass as a ranking signal.
    const candidates = sideClasses.flatMap((cls) => Array.from(document.querySelectorAll('.' + cls)))
      .filter((btn) => {
        const r = btn.getBoundingClientRect();
        if (!(rectOk(r) && r.x >= 0 && r.y >= 0)) return false;
        const tileOfBtn = enclosingTile(btn);
        return !tileOfBtn || tileOfBtn === tile;
      })
      .map((btn) => {
        const r = btn.getBoundingClientRect();
        const active = btn.classList.contains(activeClass);
        const tileOfBtn = enclosingTile(btn);
        const inTile = tileOfBtn === tile;
        return {
          btn,
          area: r.width * r.height,
          score: (active ? 1000000 : 0) + (!inTile ? 500000 : 0) + (r.width * r.height),
          x: r.left,
          y: r.top,
        };
      })
      .sort((a, b) => b.score - a.score);
    if (candidates.length < 1) return null;
    return btnCoords(
      candidates[0].btn,
      candidates[0].btn.classList.contains(activeClass),
      'active_panel_after_qpid_tile'
    );
  }
  function enclosingTile(el) {
    let node = el;
    for (let depth = 0; depth < 12 && node; depth++, node = node.parentElement) {
      if (String(node.id || '').startsWith('TileHeight-')) return node;
    }
    return null;
  }
  // Compact multi-baccarat tiles render the real bet zones as <div>s whose text
  // is exactly プレイヤー/バンカー/タイ (NOT ym_* classed buttons, NOT <button>).
  // Match by stable text and click the actual zone — this is the spot the human
  // taps to bet. This is the primary path for these tiles.
  function tryBetZoneByText(tile, src) {
    if (!tile) return null;
    const gameGuard = guardGame(tile, src);
    if (gameGuard) return gameGuard;
    const labelMap = {
      P: ['プレイヤー', 'player'],
      B: ['バンカー', 'banker'],
      T: ['タイ', 'tie'],
      PP: ['プレイヤーペア', 'ｐペア', 'pペア', 'playerpair', 'ppair'],
      BP: ['バンカーペア', 'ｂペア', 'bペア', 'bankerpair', 'bpair'],
    };
    const labels = (labelMap[sideCode] || []).map((s) => norm(s));
    if (!labels.length) return null;
    const matches = [];
    let nodes;
    try { nodes = tile.querySelectorAll('div, span, button'); } catch (_) { return null; }
    for (const e of nodes) {
      const t = norm(e.innerText || e.textContent || '');
      if (!labels.includes(t)) continue;
      const r = e.getBoundingClientRect();
      // Bet zones are sizeable (~60-120px). Exclude tiny labels and oversized wrappers.
      if (rectOk(r) && r.x >= 0 && r.y >= 0 && r.width >= 30 && r.height >= 30 && r.width <= 280 && r.height <= 220) {
        matches.push({ el: e, area: r.width * r.height });
      }
    }
    if (!matches.length) return null;
    // Largest exact-text match = the outermost interactive zone container.
    matches.sort((a, b) => b.area - a.area);
    return btnCoords(matches[0].el, false, src);
  }

  let targetTile = null;
  // 1) qpid と一致するタイル内部だけを対象にする。親グリッドへは広げない。
  if (qpid) {
    targetTile = document.getElementById('TileHeight-' + qpid);
    if (targetTile) {
      try { targetTile.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
      // Primary: click the real bet zone by its プレイヤー/バンカー/タイ text.
      const zoneResult = tryBetZoneByText(targetTile, 'qpid_tile_betzone');
      if (zoneResult) return zoneResult;
      const activePanelResult = tryActivePanelAfterTargetTile(targetTile);
      if (activePanelResult) return activePanelResult;
      const orderedResult = tryOrderedTileButtons(targetTile, 'qpid_tile_ordered');
      if (orderedResult) return orderedResult;
      const geomResult = geometryCoords(targetTile, 'qpid_tile');
      if (geomResult) return geomResult;
    }
  }

  // 2) 表示タイルのテーブル名を完全一致させ、そのタイル内の指定側ボタンだけを返す。
  //    マルチ画面では qpid が DOM に存在しないため、この経路が主経路となる。
  if (tableCandidates.size) {
    for (const el of document.querySelectorAll('*')) {
      try {
        if (!tableCandidates.has(norm(el.textContent))) continue;
        let hasExactChild = false;
        for (const child of el.children || []) {
          if (tableCandidates.has(norm(child.textContent))) { hasExactChild = true; break; }
        }
        if (hasExactChild) continue;
        const tile = enclosingTile(el);
        const zoneResult = tryBetZoneByText(tile, 'table_name_betzone');
        if (zoneResult) return zoneResult;
        const activePanelResult = tryActivePanelAfterTargetTile(tile);
        if (activePanelResult) return activePanelResult;
        const orderedResult = tryOrderedTileButtons(tile, 'table_name_tile_ordered');
        if (orderedResult) return orderedResult;
        const result = tryWithinTile(tile, 'table_name_tile');
        if (result) return result;
        if (qpid && tile && tile.id === ('TileHeight-' + qpid)) {
          const geomResult = geometryCoords(tile, 'table_name_qpid_tile');
          if (geomResult) return geomResult;
        }
      } catch(e) {}
    }
  }

  // Ambiguous button counts are diagnostic only. Never click a shared-frame
  // button unless it was bound to the requested table above.
  const activeBtns = sideClasses.flatMap((cls) => Array.from(document.querySelectorAll('.' + cls + '.' + activeClass)))
    .filter(b => rectOk(b.getBoundingClientRect()));

  const visibleBtns = sideClasses.flatMap((cls) => Array.from(document.querySelectorAll('.' + cls)))
    .filter(b => { const r = b.getBoundingClientRect(); return rectOk(r) && r.x >= 0 && r.y >= 0; });
  const buttonSamples = visibleBtns.slice(0, 12).map((btn, idx) => {
    const r = btn.getBoundingClientRect();
    const parents = [];
    let node = btn;
    for (let depth = 0; depth < 12 && node; depth++, node = node.parentElement) {
      const txt = String(node.innerText || node.textContent || '')
        .replace(/\s+/g, ' ').trim().slice(0, 220);
      const attrs = {};
      try {
        for (const attr of node.attributes || []) {
          const key = String(attr.name || '').toLowerCase();
          if (key.includes('table') || key.includes('qpid') || key === 'id' || key.includes('testid')) {
            attrs[key] = String(attr.value || '').slice(0, 100);
          }
        }
      } catch(_) {}
      parents.push({
        depth: depth,
        tag: String(node.tagName || ''),
        cls: String(node.className || '').slice(0, 70),
        text: txt,
        attrs: attrs,
      });
    }
    return {
      idx: idx,
      active: btn.classList.contains(activeClass),
      x: Math.round(r.left),
      y: Math.round(r.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
      parents: parents,
    };
  });
  // Class-agnostic diagnostic: dump every clickable-looking descendant of the
  // target tile (BUTTON / role=button / [data-testid] / small visible boxes) so
  // we can identify the compact multi-baccarat tile's bet buttons even when they
  // do NOT use ym_* classes. Captured once on not_found to design the selector.
  const targetDiag = targetTile ? (() => {
    const out = [];
    const seen = new Set();
    const all = Array.from(targetTile.querySelectorAll('*'));
    for (const el of all) {
      if (out.length >= 50) break;
      let r;
      try { r = el.getBoundingClientRect(); } catch(_) { continue; }
      if (!r || r.width < 12 || r.height < 12 || r.width > 600) continue;
      const tag = String(el.tagName || '');
      const cls = String(el.className || '');
      const testid = (el.getAttribute && (el.getAttribute('data-testid') || '')) || '';
      const role = (el.getAttribute && (el.getAttribute('role') || '')) || '';
      const aria = (el.getAttribute && (el.getAttribute('aria-label') || '')) || '';
      const clickable = tag === 'BUTTON' || role === 'button' || !!testid ||
        (el.onclick != null) || /btn|button|spot|bet|wager|cell|chip/i.test(cls);
      if (!clickable) continue;
      const key = tag + '|' + cls + '|' + Math.round(r.left) + ',' + Math.round(r.top);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        tag: tag,
        cls: cls.slice(0, 90),
        testid: String(testid).slice(0, 50),
        role: String(role).slice(0, 24),
        aria: String(aria).slice(0, 40),
        text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height),
      });
    }
    return out;
  })() : [];

  return {ok: false, reason: 'not_found',
          tableName: tableName, tableCandidates: Array.from(tableCandidates),
          sideClasses: sideClasses,
          sideBtnCount: sideClasses.reduce((n, cls) => n + document.querySelectorAll('.' + cls).length, 0),
          activeBtnCount: sideClasses.reduce((n, cls) => n + document.querySelectorAll('.' + cls + '.' + activeClass).length, 0),
          visibleCount: visibleBtns.length, buttonSamples: buttonSamples,
          targetTileFound: !!targetTile, targetDiag: targetDiag};
}
"""

    def _hover_multi_tile(self, frame: Any, qpid: str) -> bool:
        """Expose the wager controls for one exact multi-play card without betting."""
        tid = str(qpid or "").strip()
        if not tid:
            return False
        try:
            tile = frame.locator(f'[id="TileHeight-{tid}"]')
            tile.hover(timeout=2000, force=True)
            try:
                box = tile.bounding_box(timeout=1000)
                if box:
                    frame.page.mouse.move(
                        float(box["x"]) + float(box["width"]) / 2.0,
                        float(box["y"]) + float(box["height"]) / 2.0,
                        steps=8,
                    )
            except TypeError:
                pass
            except Exception as ex:
                logger.debug(f"[ML-HOVER] mouse move after hover failed table={tid}: {ex}")
            frame.page.wait_for_timeout(400)
            logger.info(f"[ML-HOVER] exact tile hovered table={tid}")
            return True
        except Exception as ex:
            logger.warning(f"[ML-HOVER] exact tile hover failed table={tid}: {ex}")
            return False

    _DECOY_CLICK_HEADER_JS = r"""
(args) => {
  const qpid = String((args && args.qpid) || '').trim();
  if (!qpid) return {ok: false, reason: 'no_qpid'};
  const tile = document.getElementById('TileHeight-' + qpid);
  if (!tile) return {ok: false, reason: 'not_mounted', qpid};
  try { tile.scrollIntoView({block: 'center', inline: 'center'}); } catch(_) {}
  const r = tile.getBoundingClientRect();
  if (!r || r.width < 20 || r.height < 20) return {ok: false, reason: 'no_rect', qpid};
  // Click the tile HEADER strip (top ~16px) where the table name lives.
  // The player/banker bet cells (.ym_qA) sit lower in the tile, so a header
  // click activates the tile selection without registering as a wager.
  const cx = r.left + Math.min(40, Math.max(8, r.width * 0.15));
  const cy = r.top + Math.min(14, Math.max(4, r.height * 0.05));
  const base = {bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy};
  try {
    if (window.PointerEvent) {
      tile.dispatchEvent(new PointerEvent('pointerdown', base));
      tile.dispatchEvent(new PointerEvent('pointerup', base));
    }
    tile.dispatchEvent(new MouseEvent('mousedown', base));
    tile.dispatchEvent(new MouseEvent('mouseup', base));
    tile.dispatchEvent(new MouseEvent('click', base));
  } catch(_) {}
  // Read back tile text after click so the caller can see what game_id the
  // clicked tile is presently subscribed to.
  const tileText = String((tile.innerText || tile.textContent || '')).replace(/\s+/g, ' ').trim();
  let gameId = '';
  try {
    let m = tileText.match(/(?:ID|GameId|Game)[^\d]{0,4}(\d{8,})/i);
    if (m) gameId = m[1];
    if (!gameId) {
      m = tileText.match(/\b(\d{8,})\b/);
      if (m) gameId = m[1];
    }
  } catch(_) {}
  return {ok: true, qpid, x: Math.round(cx), y: Math.round(cy),
          w: Math.round(r.width), h: Math.round(r.height),
          gameId, tileText: tileText.slice(0, 220)};
}
"""

    def _decoy_click_to_resubscribe(
        self,
        frame: Any,
        target_qpid: str,
        target_table_name: str = "",
    ) -> bool:
        """Force Pragmatic to re-subscribe the target tile to the latest round.

        Re-clicking an already-active multi-baccarat tile is a no-op for the
        Pragmatic React component: the panel keeps its previous game_id and
        does not request the new round data. Selecting a different visible
        tile first deactivates the target's subscription, and clicking the
        target back triggers a fresh subscribe. This is the recovery path of
        last resort when _focus_table_in_multi('prepare') has already been
        retried but currentGameId is still stale.

        Returns True iff both clicks dispatched (does not imply the game_id
        actually caught up; the caller must re-check the guard).
        """
        tid = str(target_qpid or "").strip()
        if not tid:
            return False
        snap = self._refresh_multi_tile_snapshot(force=True) or {}
        tiles = snap.get("tiles") or []
        decoy_qpid = ""
        for t in tiles:
            if not isinstance(t, dict):
                continue
            q = str(t.get("qpid") or "").strip()
            if not q or q == tid:
                continue
            if not bool(t.get("visible")):
                continue
            if not (bool(t.get("hasP")) or bool(t.get("hasB"))):
                continue
            decoy_qpid = q
            break
        if not decoy_qpid:
            logger.info(f"[DECOY] no eligible decoy tile found target={tid}")
            return False
        try:
            decoy_res = frame.evaluate(self._DECOY_CLICK_HEADER_JS, {"qpid": decoy_qpid})
        except Exception as ex:
            logger.warning(f"[DECOY] decoy click failed decoy={decoy_qpid} err={ex}")
            return False
        logger.info(
            f"[DECOY] clicked decoy={decoy_qpid} for target={tid} "
            f"result={decoy_res}"
        )
        try:
            frame.page.wait_for_timeout(280)
        except Exception:
            time.sleep(0.28)
        try:
            target_res = frame.evaluate(self._DECOY_CLICK_HEADER_JS, {"qpid": tid})
        except Exception as ex:
            logger.warning(f"[DECOY] target reclick failed target={tid} err={ex}")
            return False
        logger.info(f"[DECOY] target reclick target={tid} result={target_res}")
        try:
            frame.page.wait_for_timeout(380)
        except Exception:
            time.sleep(0.38)
        return True

    _CLEAR_ASSIST_OVERLAY_JS = r"""
(args) => {
  const qpid = String((args && args.qpid) || '').trim();
  const clearTile = (tile) => {
    if (!tile) return false;
    try {
      tile.classList.remove('bacopy-assist-tile', 'bacopy-assist-now', 'bacopy-assist-ready');
      tile.style.removeProperty('--bc-ring');
      tile.style.removeProperty('--bc-glow');
      tile.style.removeProperty('--bc-bg');
      tile.removeAttribute('data-bacopy-assist-token');
      for (const old of tile.querySelectorAll(':scope > .bacopy-assist-badge')) old.remove();
      return true;
    } catch(_) {
      return false;
    }
  };
  if (qpid) {
    const tile = document.getElementById('TileHeight-' + qpid);
    if (tile) return {ok: clearTile(tile), qpid, mode: 'target'};
  }
  let count = 0;
  for (const tile of document.querySelectorAll('.bacopy-assist-tile')) {
    if (clearTile(tile)) count += 1;
  }
  return {ok: count > 0, qpid, mode: 'sweep', count};
}
"""

    def clear_manual_assist_overlay(self, qpid: str) -> bool:
        """Remove the manual assist READY/NOW frame without scrolling or betting."""
        tid = str(qpid or "").strip()
        frame = self._find_pragmatic_frame(target_qpid=tid) if tid else self._find_pragmatic_frame()
        if not frame:
            logger.info(f"[MANUAL-ASSIST] overlay clear skipped: frame not found target={tid or '-'}")
            return False
        try:
            res = frame.evaluate(self._CLEAR_ASSIST_OVERLAY_JS, {"qpid": tid})
            ok = bool(isinstance(res, dict) and res.get("ok"))
            logger.info(f"[MANUAL-ASSIST] overlay clear target={tid or '-'} ok={ok} result={res}")
            return ok
        except Exception as ex:
            logger.debug(f"[MANUAL-ASSIST] overlay clear error target={tid or '-'}: {ex}")
            return False

    def _center_multi_tile(
        self,
        qpid: str,
        table_name: str = "",
        *,
        click: bool = False,
        source: str = "",
    ) -> bool:
        """Keep a multi-play tile visible near the screen center.

        ``source`` identifies which hold caller invoked this so logs can be
        disambiguated; previously every caller logged as ``[VISIBLE-HOLD]``
        even when the request came from ASSIST-HOLD or NOW-BET-HOLD.
        """
        tid = str(qpid or "").strip()
        tag_map = {
            "visible_bet_hold": "VISIBLE-HOLD",
            "now_bet_hold": "NOW-BET-HOLD",
            "now_lock": "NOW-BET-HOLD",
            "assist_focus_hold": "ASSIST-HOLD",
        }
        tag = tag_map.get(str(source or "").strip().lower(), "TILE-CENTER")
        if not tid:
            return False
        # 機能①拡張(HOLD): スクロール凍結中は一切センタリング(スクロール)しない。
        # _center_multi_tile を直接呼ぶ全経路(now_bet_hold / visible_bet_hold /
        # mark_bet_resolved 等)をここで一括ガードする。以前は _perform_switch と
        # _maintain_assist_focus_hold だけが freeze を見ており、NOW枠/BET表示保持の
        # 再センタリングが凍結を無視して他卓へスクロールしていた(HOLD効かない不具合)。
        if getattr(self, "_scroll_frozen", False):
            logger.info(f"[{tag}] skip center: scroll-frozen target={tid!r}")
            return False
        frame = self._find_pragmatic_frame(target_qpid=tid)
        if not frame:
            logger.info(f"[{tag}] frame not found for center target={tid!r}")
            return False
        try:
            source_key = str(source or "").strip().lower()
            assist_status = ""
            if source_key in ("now_bet_hold", "now_lock"):
                assist_status = "NOW"
            elif source_key == "assist_focus_hold":
                assist_status = "READY"
            res = frame.evaluate(
                _MULTI_LOBBY_FOCUS_JS,
                {
                    "qpid": tid,
                    "click": bool(click),
                    "maxScroll": int(os.getenv("BACOPY_VISIBLE_HOLD_SCROLL_MAX", "18") or "18"),
                    "candidates": [str(table_name or ""), tid],
                    "hintIndex": -1,
                    "hintTotal": 0,
                    "holdOnly": source_key in ("now_bet_hold", "visible_bet_hold"),
                    "assistStatus": assist_status,
                },
            )
            ok = bool(isinstance(res, dict) and res.get("found"))
            logger.info(f"[{tag}] center target={tid!r} ok={ok} result={res}")
            return ok
        except Exception as ex:
            logger.warning(f"[{tag}] center failed target={tid!r}: {ex}")
            return False

    def _run_lobby_scroll_probe(self, qpid: str) -> None:
        """One-shot probe: log every scroll-able ancestor and translateY element.

        Goal: identify whether the Pragmatic lobby scrolls via document scrollTop,
        an inner overflow:auto container, or a React virtualizer using
        transform: translateY(...). The result is logged as JSON so the user can
        copy/paste a single line of evidence.
        """
        tid = str(qpid or "").strip()
        frame = self._find_pragmatic_frame(target_qpid=tid) or self._find_pragmatic_frame()
        if not frame:
            logger.info(f"[SCROLL-PROBE] frame not found target={tid!r}")
            return
        try:
            res = frame.evaluate(_LOBBY_SCROLL_PROBE_JS, {"qpid": tid})
        except Exception as ex:
            logger.warning(f"[SCROLL-PROBE] eval failed target={tid!r}: {ex}")
            return
        if not isinstance(res, dict):
            logger.info(f"[SCROLL-PROBE] target={tid!r} result_type={type(res).__name__}")
            return
        try:
            anc = res.get("scrollAncestors") or []
            tr = res.get("transformElements") or []
            vh = res.get("virtualizerHints") or []
            tile_rect = (res.get("tile") or {}).get("rect") or {}
            logger.info(
                "[SCROLL-PROBE] target=%s href=%s tile=%s scrollAncestors=%d transforms=%d virtualizers=%d"
                % (
                    tid or "-",
                    str(res.get("href") or "")[:80],
                    json.dumps(tile_rect, separators=(",", ":")),
                    len(anc),
                    len(tr),
                    len(vh),
                )
            )
            for d in anc[:5]:
                logger.info("[SCROLL-PROBE] anc " + json.dumps(d, separators=(",", ":"))[:280])
            for d in tr[:5]:
                logger.info("[SCROLL-PROBE] tr  " + json.dumps(d, separators=(",", ":"))[:280])
            for d in vh[:5]:
                logger.info("[SCROLL-PROBE] vh  " + json.dumps(d, separators=(",", ":"))[:280])
            se = res.get("scrollingElement") or {}
            ws = res.get("windowScroll") or {}
            logger.info(
                "[SCROLL-PROBE] scrollingElement=%s windowScroll=%s"
                % (
                    json.dumps(se, separators=(",", ":")),
                    json.dumps(ws, separators=(",", ":")),
                )
            )
        except Exception as ex:
            logger.warning(f"[SCROLL-PROBE] log format failed: {ex}")

    def _start_visible_bet_hold(self, bet_id: str, table_id: str, table_name: str = "") -> None:
        if not self._multi_lobby_mode:
            return
        if os.getenv("BACOPY_KEEP_BET_VISIBLE_UNTIL_RESULT", "1").strip() == "0":
            return
        tid = str(table_id or "").strip()
        if not tid:
            return
        hold_sec = float(os.getenv("BACOPY_VISIBLE_BET_HOLD_MAX_SEC", "180") or 180)
        self._visible_bet_hold = {
            "bet_id": str(bet_id or "").strip(),
            "table_id": tid,
            "table_name": str(table_name or tid),
            "started_at": time.time(),
            "until": time.time() + max(15.0, hold_sec),
        }
        self._last_visible_bet_center_at = 0.0
        logger.info(
            f"[VISIBLE-HOLD] start table={tid} name={table_name or tid!r} "
            f"bet_id={str(bet_id or '')[:16]} max_sec={hold_sec:.0f}"
        )
        self._center_multi_tile(tid, table_name, click=False, source="visible_bet_hold")

    def freeze_scroll(self, on: bool) -> None:
        """機能①拡張(HOLD): 枠が無くてもスクロール凍結。ON中は卓切替/再センタリングを
        全停止し、現在の表示位置で固定する(特定卓のpinとは独立)。"""
        self._scroll_frozen = bool(on)
        logger.info(f"[ASSIST-HOLD] scroll-freeze {'ON' if on else 'OFF'}")

    def set_pinned_table(self, qpid: str) -> None:
        """機能①(HOLD): この卓を固定。固定中は他卓へfocusを移さず中央保持を続ける。"""
        self._pinned_qpid = str(qpid or "").strip()
        logger.info(f"[ASSIST-HOLD] PIN set qpid={self._pinned_qpid or '-'}")

    def clear_pinned_table(self) -> None:
        """機能①(HOLD): 固定を解除し通常のマルチ卓スキャンに戻す。
        固定卓の中央保持(_assist_focus_hold)が居残らないよう明示クリアする。"""
        qpid = self._pinned_qpid
        if qpid:
            logger.info(f"[ASSIST-HOLD] PIN cleared qpid={qpid}")
        self._pinned_qpid = ""
        self._assist_focus_hold = {}
        try:
            if qpid:
                self.clear_manual_assist_overlay(qpid)
        except Exception:
            pass

    def _start_assist_focus_hold(self, table_id: str, table_name: str = "", *, intent: str = "") -> None:
        if not self._multi_lobby_mode:
            return
        # 機能①拡張(HOLD): スクロール凍結中は新規センタリングしない(画面を動かさない)。
        if getattr(self, "_scroll_frozen", False):
            return
        if os.getenv("BACOPY_ASSIST_FOCUS_HOLD", "1").strip() == "0":
            return
        tid = str(table_id or "").strip()
        if not tid:
            return
        # 機能①(HOLD): 固定中は固定卓以外へ focus を移さない（スクロール抑止）。
        if self._pinned_qpid and tid != self._pinned_qpid:
            return
        # preposition 先を別卓へ切り替える際、前卓の黄色 overlay を明示削除し、
        # 信号化しなかった卓に黄色枠が残留するのを防ぐ (2026-05-30)。
        prev_tid = str((self._assist_focus_hold or {}).get("table_id") or "")
        if prev_tid and prev_tid != tid:
            try:
                self.clear_manual_assist_overlay(prev_tid)
            except Exception:
                pass
        hold_sec = float(os.getenv("BACOPY_ASSIST_FOCUS_HOLD_SEC", "45") or 45)
        self._assist_focus_hold = {
            "table_id": tid,
            "table_name": str(table_name or tid),
            "intent": str(intent or ""),
            "until": time.time() + max(8.0, hold_sec),
        }
        self._last_assist_focus_center_at = 0.0
        logger.info(
            f"[ASSIST-HOLD] start table={tid} name={table_name or tid!r} "
            f"intent={intent or '-'} max_sec={hold_sec:.0f}"
        )
        try:
            now_ts = time.time()
            last_probe = float(getattr(self, "_last_lobby_scroll_probe_at", 0.0) or 0.0)
            if (
                os.getenv("BACOPY_LOBBY_SCROLL_PROBE", "1").strip() != "0"
                and (now_ts - last_probe) > 30.0
            ):
                self._last_lobby_scroll_probe_at = now_ts
                self._run_lobby_scroll_probe(tid)
        except Exception as ex:
            logger.debug(f"[SCROLL-PROBE] dispatch failed: {ex}")

    def _start_active_now_bet_hold(self, bet: dict[str, Any] | None, table_name: str = "") -> None:
        if not self._multi_lobby_mode or not isinstance(bet, dict):
            return
        tid = str(bet.get("table_id") or "").strip()
        if not tid:
            return
        hold_sec = float(os.getenv("BACOPY_NOW_BET_HOLD_MAX_SEC", "65") or 65)
        self._active_now_bet_hold = {
            "table_id": tid,
            "table_name": str(table_name or bet.get("table_name") or tid),
            "bet_id": str(bet.get("bet_id") or ""),
            "decision_id": str(bet.get("decision_id") or ""),
            "side": str(bet.get("side") or ""),
            "started_at": time.time(),
            "until": time.time() + max(30.0, hold_sec),
        }
        self._last_active_now_bet_center_at = 0.0
        logger.info(
            f"[NOW-BET-HOLD] start table={tid} bet_id={str(bet.get('bet_id') or '')[:16] or '-'} "
            f"decision={str(bet.get('decision_id') or '')[:12] or '-'} max_sec={max(30.0, hold_sec):.0f}"
        )
        self._center_multi_tile(
            tid,
            str(table_name or bet.get("table_name") or tid),
            click=False,
            source="now_bet_hold",
        )

    def _clear_active_now_bet_hold(
        self,
        *,
        bet_id: str = "",
        table_id: str = "",
        decision_id: str = "",
        reason: str = "",
    ) -> None:
        hold = self._active_now_bet_hold or {}
        if not hold:
            return
        bid = str(bet_id or "").strip()
        tid = str(table_id or "").strip()
        did = str(decision_id or "").strip()
        hold_bid = str(hold.get("bet_id") or "")
        hold_tid = str(hold.get("table_id") or "")
        hold_did = str(hold.get("decision_id") or "")
        if bid and hold_bid and bid != hold_bid:
            return
        if tid and hold_tid and tid != hold_tid:
            return
        if did and hold_did and did != hold_did:
            return
        logger.info(
            f"[NOW-BET-HOLD] clear table={hold_tid or '-'} bet_id={hold_bid[:16] or '-'} "
            f"decision={hold_did[:12] or '-'} reason={reason or '-'}"
        )
        self._active_now_bet_hold = {}
        # Remove the red/blue NOW overlay from the page immediately. The JS-side
        # removal setTimeout is unreliable because the hold loop kept re-stamping
        # the overlay token every cycle (TTL never fired) — that left the frame
        # "stuck" red after the hand ended. Explicit clear here fixes it.
        if hold_tid:
            try:
                self.clear_manual_assist_overlay(hold_tid)
            except Exception as ex:
                logger.debug(f"[NOW-BET-HOLD] overlay clear failed: {ex}")

    def _maintain_active_now_bet_hold(self, now: float | None = None) -> None:
        hold = self._active_now_bet_hold or {}
        if not hold:
            return
        now = time.time() if now is None else now
        tid = str(hold.get("table_id") or "")
        if not tid:
            self._active_now_bet_hold = {}
            return
        if now >= float(hold.get("until") or 0.0):
            logger.info(f"[NOW-BET-HOLD] expired table={tid}")
            self._active_now_bet_hold = {}
            try:
                self.clear_manual_assist_overlay(tid)
            except Exception:
                pass
            return
        interval = float(os.getenv("BACOPY_NOW_BET_RECENTER_SEC", "0.5") or 0.5)
        if now - self._last_active_now_bet_center_at < max(0.3, interval):
            return
        self._last_active_now_bet_center_at = now
        self._center_multi_tile(
            tid, str(hold.get("table_name") or tid), click=False, source="now_bet_hold"
        )

    def set_now_lock_release_cb(self, fn) -> None:
        """Register the bot callback invoked on betsopen-detected hand-end so the
        red/blue assist overlay clears without waiting for the 80s lock timer."""
        self._now_lock_release_cb = fn if callable(fn) else None

    def set_bot_now_lock(
        self,
        *,
        decision_id: str,
        table_id: str,
        table_name: str = "",
        side: str = "",
        until: float = 0.0,
    ) -> None:
        """Mirror bot._now_lock so executor-side gates can drop competing work."""
        tid = str(table_id or "").strip()
        did = str(decision_id or "").strip()
        if not tid and not did:
            self._bot_now_lock = {}
            return
        # Preserve hand-end tracking across re-stamp calls for the same decision
        # (set_at = when the lock began; bet_gid = the hand the operator bet on).
        prev = self._bot_now_lock or {}
        same = bool(prev) and str(prev.get("decision_id") or "") == did
        set_at = float(prev.get("set_at") or 0.0) if same else time.time()
        bet_gid = str(prev.get("bet_gid") or "") if same else ""
        self._bot_now_lock = {
            "decision_id": did,
            "table_id": tid,
            "table_name": str(table_name or tid),
            "side": str(side or "").upper(),
            "until": float(until or 0.0),
            "set_at": set_at,
            "bet_gid": bet_gid,
        }
        logger.info(
            f"[BOT-NOW-LOCK] mirror set did={did[:12] or '-'} table={tid or '-'} "
            f"side={side or '-'} until={float(until or 0.0):.0f}"
        )

    def clear_bot_now_lock(
        self,
        *,
        decision_id: str = "",
        table_id: str = "",
        reason: str = "",
    ) -> None:
        lock = self._bot_now_lock or {}
        if not lock:
            return
        did = str(decision_id or "").strip()
        tid = str(table_id or "").strip()
        lock_did = str(lock.get("decision_id") or "")
        lock_tid = str(lock.get("table_id") or "")
        if did and lock_did and did != lock_did:
            return
        if tid and lock_tid and tid != lock_tid:
            return
        logger.info(
            f"[BOT-NOW-LOCK] mirror clear did={lock_did[:12] or '-'} "
            f"table={lock_tid or '-'} reason={reason or '-'}"
        )
        self._bot_now_lock = {}

    def release_assist_now(self, qpid: str = "") -> None:
        """Bot calls this when the locked hand has resolved (hand-end detected
        from the result feed) or on the operator's WIN/LOSE. Stop the assist
        focus-hold for the tile and remove the NOW (red/blue) overlay so the
        view returns to normal scanning. This only clears the VISUAL lock —
        the money progression stays operator-driven via WIN/LOSE."""
        tid = str(qpid or "").strip()
        hold = self._assist_focus_hold or {}
        hold_tid = str(hold.get("table_id") or "")
        if hold and (not tid or hold_tid == tid):
            logger.info(f"[ASSIST-HOLD] release (hand-end/result) table={hold_tid or tid or '-'}")
            self._assist_focus_hold = {}
        if tid:
            try:
                self.clear_manual_assist_overlay(tid)
            except Exception as ex:
                logger.debug(f"[MANUAL-ASSIST] release_assist_now clear failed target={tid}: {ex}")

    def read_bet_history(self) -> list:
        """READ-ONLY: extract the Stake 'Bet History' (My Bets) rows from the
        top lobby page DOM for billing. Returns a list of
        {uuid, cells:[game,time,stake,mult,payout]}. Empty on any failure.
        Bet rows carry a per-row UUID (data-test-id); the header row does not.
        No clicks / no navigation — pure DOM read."""
        page = self._lobby_page or self._bet_page
        if page is None:
            return []
        js = r"""
        () => {
          const rows = [];
          for (const tb of Array.from(document.querySelectorAll('table'))) {
            for (const tr of Array.from(tb.querySelectorAll('tr'))) {
              let uuid = '';
              try { for (const a of tr.attributes) { if (/^data-test-id$/i.test(a.name)) { uuid = a.value; break; } } } catch(_) {}
              if (!uuid) continue;
              let cells = [];
              try { for (const c of tr.querySelectorAll('td')) cells.push((c.textContent||'').trim()); } catch(_) {}
              if (cells.length >= 4) rows.push({ uuid: uuid, cells: cells });
            }
          }
          return rows;
        }
        """
        try:
            res = page.evaluate(js)
            return res if isinstance(res, list) else []
        except Exception as ex:
            logger.debug(f"[BILLING] read_bet_history failed: {ex}")
            return []

    def read_stake_balance(self):
        """READ-ONLY: read the player's balance from the Pragmatic game frame DOM
        (bottom: '残高$ 109.40' / 'Balance $109.40'). The Stake account WS does not
        deliver balance frames in chrome_attach (OOPIF), and the stake.com header
        shows dots, but the in-game balance IS plain DOM text. Returns float|None."""
        try:
            fr = self._find_pragmatic_frame()
            if fr is None:
                return None
            js = r"""
            () => {
              try {
                const txt = document.body ? document.body.innerText : '';
                let m = txt.match(/残高[^0-9-]*([0-9,]+\.[0-9]+)/);
                if (!m) m = txt.match(/Balance[^0-9-]*([0-9,]+\.[0-9]+)/i);
                if (!m) return null;
                return parseFloat(m[1].replace(/,/g, ''));
              } catch (e) { return null; }
            }
            """
            v = fr.evaluate(js)
            return float(v) if v is not None else None
        except Exception as ex:
            logger.debug(f"[BILLING] read_stake_balance failed: {ex}")
            return None

    def _bot_now_lock_active(self) -> dict[str, Any]:
        lock = self._bot_now_lock or {}
        if not lock:
            return {}
        until = float(lock.get("until") or 0.0)
        if until and time.time() >= until:
            self._bot_now_lock = {}
            return {}
        return lock

    def _maintain_assist_focus_hold(self, now: float | None = None) -> None:
        # 機能①拡張(HOLD): スクロール凍結中は卓pinの有無に関わらず再センタリングを止め、
        # 現在の表示位置で完全停止する(固定卓の再センタリング=スクロールに見えるのを防ぐ)。
        if getattr(self, "_scroll_frozen", False):
            return
        hold = self._assist_focus_hold or {}
        # 機能①(HOLD): 固定中は固定卓を保持し続ける（期限切れで赤/青枠を消さない）。
        if self._pinned_qpid:
            nowt = time.time() if now is None else now
            if str(hold.get("table_id") or "") != self._pinned_qpid:
                hold = {
                    "table_id": self._pinned_qpid,
                    "table_name": str((hold or {}).get("table_name") or self._pinned_qpid),
                    "intent": "pinned",
                    "until": nowt + 3600.0,
                }
            else:
                hold["until"] = nowt + 3600.0
            self._assist_focus_hold = hold
        if not hold:
            return
        visible_hold = self._visible_bet_hold or {}
        if visible_hold:
            visible_bid = str(visible_hold.get("bet_id") or "")
            if visible_bid and visible_bid in self._confirmed_bets:
                return
            logger.info(
                f"[ASSIST-HOLD] clearing unconfirmed visible hold "
                f"table={visible_hold.get('table_id') or '-'} bet_id={visible_bid[:16] or '-'}"
            )
            self._visible_bet_hold = {}
        now = time.time() if now is None else now
        tid = str(hold.get("table_id") or "")
        if not tid:
            self._assist_focus_hold = {}
            return
        if now >= float(hold.get("until") or 0.0):
            logger.info(f"[ASSIST-HOLD] expired table={tid}")
            self._assist_focus_hold = {}
            # NOW-bet hold と同様、maintain ループの再スタンプで JS 側 TTL が不発に
            # なり preposition 黄色 overlay が残るため、期限切れ時に明示削除する
            # (予告なしの黄色枠 stuck 修正 2026-05-30)。
            try:
                self.clear_manual_assist_overlay(tid)
            except Exception:
                pass
            return
        if self._switch_request or self._switch_in_progress:
            return
        interval = float(os.getenv("BACOPY_ASSIST_FOCUS_RECENTER_SEC", "1.5") or 1.5)
        if now - self._last_assist_focus_center_at < max(0.75, interval):
            return
        self._last_assist_focus_center_at = now
        self._center_multi_tile(
            tid, str(hold.get("table_name") or tid), click=False, source="assist_focus_hold"
        )

    def _maintain_visible_bet_hold(self, now: float | None = None) -> None:
        hold = self._visible_bet_hold or {}
        if not hold:
            return
        now = time.time() if now is None else now
        tid = str(hold.get("table_id") or "")
        if not tid:
            self._visible_bet_hold = {}
            return
        if now >= float(hold.get("until") or 0.0):
            logger.info(f"[VISIBLE-HOLD] expired table={tid}")
            self._visible_bet_hold = {}
            return
        interval = float(os.getenv("BACOPY_VISIBLE_BET_RECENTER_SEC", "4.0") or 4.0)
        if now - self._last_visible_bet_center_at < max(1.0, interval):
            return
        self._last_visible_bet_center_at = now
        self._auto_recover_idle_dialogs("visible-hold")
        self._center_multi_tile(
            tid, str(hold.get("table_name") or tid), click=False, source="visible_bet_hold"
        )

    def mark_bet_resolved(self, bet_id: str = "", table_id: str = "") -> None:
        """Called by the live bot when the watched bet has an outcome."""
        bid = str(bet_id or "").strip()
        tid = str(table_id or "").strip()
        self._clear_active_now_bet_hold(
            bet_id=bid,
            table_id=tid,
            reason="result_resolved",
        )
        hold = self._visible_bet_hold or {}
        if not hold:
            return
        hold_bid = str(hold.get("bet_id") or "")
        hold_tid = str(hold.get("table_id") or "")
        if (bid and hold_bid and bid != hold_bid) or (tid and hold_tid and tid != hold_tid):
            return
        logger.info(f"[VISIBLE-HOLD] resolved; release table={hold_tid} bet_id={hold_bid[:16]}")
        self._center_multi_tile(
            hold_tid,
            str(hold.get("table_name") or hold_tid),
            click=False,
            source="visible_bet_hold",
        )
        self._visible_bet_hold = {}

    def _js_click_bet_in_container(
        self,
        frame,
        qpid: str,
        table_name: str,
        side_class: str,
        side: str = "",
        expected_game_id: str = "",
        stale_tile_game_id_ok: str = "",
    ) -> bool:
        """BET ボタンの座標を JS で取得し、page.mouse.click() で isTrusted=true クリックする"""
        try:
            self._last_click_bet_error = {}
            # ── Double-stake guard ──
            # A Playwright .click(force=True, timeout=...) can physically DISPATCH
            # the click and THEN raise (timeout on the post-click actionability /
            # stability wait). The except branches below fall through to the next
            # click strategy — which clicks the SAME bet zone again, placing a
            # SECOND wager (observed 2026-05-30: $1 plan → $2 stake, two <lpbet>
            # frames for one game 14563248719). Before any fallthrough re-click we
            # verify whether a real <lpbet gId=...> for THIS game already went out
            # (sniffed at _on_ws_sent L3069 → self._last_lpbet_*). If it did,
            # the wager already landed → return success and never re-click.
            _exp_gid = str(expected_game_id or "")
            _bet_entry_lpbet_at = float(getattr(self, "_last_lpbet_at", 0.0) or 0.0)

            def _bet_already_landed(wait_ms: int = 0) -> bool:
                # Only safe with a known game id: in multi-lobby other tables never
                # emit an outgoing <lpbet> unless WE bet them, and one executor bets
                # one game at a time, so a matching gid newer than entry == our
                # wager landed. Without an expected gid we cannot disambiguate, so
                # behave exactly as before (no guard).
                if not _exp_gid:
                    return False
                deadline = time.time() + max(0, int(wait_ms)) / 1000.0
                while True:
                    try:
                        if (float(self._last_lpbet_at or 0.0) > _bet_entry_lpbet_at
                                and str(self._last_lpbet_gid or "") == _exp_gid):
                            return True
                    except Exception:
                        pass
                    if time.time() >= deadline:
                        return False
                    time.sleep(0.05)

            res = frame.evaluate(
                self._JS_BET_COORDS,
                {
                    "qpid": qpid,
                    "tableName": table_name,
                    "sideClass": side_class,
                    "sideCode": side,
                    "expectedGameId": str(expected_game_id or ""),
                    "staleTileGameIdOk": str(stale_tile_game_id_ok or ""),
                    # コンパクトな multi-baccarat タイル (Speed Baccarat 等) には
                    # ym_yP/ym_yQ クラスのベットボタンが存在せず、ベット領域は
                    # 座標(visual ratio)でしか特定できない。preposition probe の
                    # qpid_tile_geometry が安定して P/B 位置を出しており、人間も
                    # その位置を直接クリックして BET できていた実績がある。
                    # よって実BET経路でも geometry を許可する (既定ON)。
                    # game_id ガードが geometryCoords 内で先に走るため別ハンド誤BETは
                    # 防止され、空振りは lpbet 未確認として検知される。
                    # サイド誤り防止のため最初の数回はサイドを目視確認すること。
                    "geometryFallback": os.getenv("BACOPY_CLICK_BET_ALLOW_GEOMETRY", "1").strip(),
                },
            )
            logger.info(f"[CLICK-BET-JS] qpid={qpid!r} table={table_name!r} coords={res}")
            if isinstance(res, dict) and res.get("ok") and "geometry" in str(res.get("source") or ""):
                logger.warning(
                    f"[CLICK-BET-JS] GEOMETRY click qpid={qpid!r} side={side!r} "
                    f"src={res.get('source')!r} x={res.get('x')} y={res.get('y')} "
                    f"— verify side visually on first bets"
                )
            if not (isinstance(res, dict) and res.get("ok")):
                self._last_click_bet_error = res if isinstance(res, dict) else {"reason": "invalid_result", "result": repr(res)}
                return False

            btn_x = float(res.get("x", 0))
            btn_y = float(res.get("y", 0))
            page = frame.page

            # ── Primary path: Playwright TEXT locator scoped to the tile ──
            # The compact tile's bet zone is a <div> whose text is exactly
            # プレイヤー/バンカー/タイ. A text locator re-resolves the live DOM at
            # click time (immune to the React re-render that detaches our marker)
            # and clicks the actual element (immune to frame/OOPIF page-coord
            # offset errors that made mouse.click miss). This is the same zone
            # the human taps — clicking it places a real wager.
            side_label = {"P": "プレイヤー", "B": "バンカー", "T": "タイ"}.get(str(side or "").upper(), "")
            use_text_locator = os.getenv("BACOPY_CLICK_BET_TEXT_LOCATOR", "1").strip() != "0"
            if use_text_locator and side_label and qpid:
                try:
                    loc_timeout = int(os.getenv("BACOPY_CLICK_BET_LOCATOR_TIMEOUT_MS", "1800") or 1800)
                    tile_loc = frame.locator(f'[id="TileHeight-{qpid}"]')
                    zone = tile_loc.get_by_text(side_label, exact=True).first
                    zone.click(force=True, timeout=loc_timeout)
                    self._last_click_bet_page_coords = {
                        "x": btn_x, "y": btn_y, "qpid": str(qpid or ""),
                        "side": str(side or ""), "expected_game_id": str(expected_game_id or ""),
                        "at": time.time(), "method": "text_locator",
                    }
                    logger.info(
                        f"[CLICK-BET-JS] text-locator click OK qpid={qpid!r} side={side!r} "
                        f"label={side_label!r}"
                    )
                    return True
                except Exception as tex:
                    logger.warning(
                        f"[CLICK-BET-JS] text-locator click FAILED ({tex}); "
                        f"checking double-stake guard before fallback"
                    )
                    if _bet_already_landed(int(os.getenv("BACOPY_CLICK_BET_LANDED_WAIT_MS", "2500") or 2500)):
                        self._last_click_bet_page_coords = {
                            "x": btn_x, "y": btn_y, "qpid": str(qpid or ""),
                            "side": str(side or ""), "expected_game_id": _exp_gid,
                            "at": time.time(), "method": "text_locator_landed_after_raise",
                        }
                        logger.warning(
                            f"[CLICK-BET-JS] DOUBLE-STAKE GUARD: text-locator raised but "
                            f"<lpbet gId={_exp_gid}> already sent — wager landed, NOT re-clicking"
                        )
                        return True
                    logger.warning(
                        "[CLICK-BET-JS] no lpbet yet — falling back to marker-locator / mouse.click"
                    )

            # Diagnostic: when geometry is used (no real button element matched),
            # reveal what element actually sits at the computed click point AND
            # where the real bet zones (プレイヤー/バンカー/タイ text) are, so we can
            # target the element that actually places a wager.
            if os.getenv("BACOPY_CLICK_BET_POINT_PROBE", "1").strip() != "0" and \
               isinstance(res, dict) and "geometry" in str(res.get("source") or ""):
                try:
                    probe = frame.evaluate(
                        r"""(a) => {
                          const out = {atPoint: [], zones: []};
                          let el = document.elementFromPoint(a.x, a.y);
                          for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                            const r = el.getBoundingClientRect();
                            out.atPoint.push({
                              tag: el.tagName,
                              cls: String(el.className || '').slice(0, 70),
                              testid: (el.getAttribute && (el.getAttribute('data-testid') || '')) || '',
                              role: (el.getAttribute && (el.getAttribute('role') || '')) || '',
                              text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24),
                              x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
                            });
                          }
                          let tile = document.elementFromPoint(a.x, a.y);
                          for (let i = 0; i < 14 && tile; i++, tile = tile.parentElement) {
                            if (String(tile.id || '').startsWith('TileHeight-')) break;
                          }
                          if (tile && tile.querySelectorAll) {
                            for (const e of tile.querySelectorAll('*')) {
                              const t = String(e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
                              if (/^(プレイヤー|バンカー|タイ|PLAYER|BANKER|TIE|P PAIR|B PAIR|Ｐペア|Ｂペア)$/i.test(t)) {
                                const r = e.getBoundingClientRect();
                                out.zones.push({
                                  tag: e.tagName,
                                  cls: String(e.className || '').slice(0, 70),
                                  testid: (e.getAttribute && (e.getAttribute('data-testid') || '')) || '',
                                  text: t,
                                  x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
                                });
                                if (out.zones.length >= 12) break;
                              }
                            }
                          }
                          return out;
                        }""",
                        {"x": btn_x, "y": btn_y},
                    )
                    logger.warning(f"[CLICK-BET-PROBE] geomPoint=({btn_x:.0f},{btn_y:.0f}) result={probe}")
                except Exception as _pe:
                    logger.warning(f"[CLICK-BET-PROBE] failed: {_pe}")

            # ── Primary path: Playwright locator.click on the JS-tagged button ──
            # _JS_BET_COORDS tags the exact, game-id-validated button with
            # data-bacopy-bet-btn="1". Playwright re-queries the DOM at click
            # time and dispatches a trusted click. This is the approach that was
            # stable in ba/executor.py (locator.click(force=True)) and avoids the
            # absolute-page-coordinate math that is fragile in chrome_attach when
            # the page/iframe scrolls between coord-read and click.
            use_locator = os.getenv("BACOPY_CLICK_BET_USE_LOCATOR", "1").strip() != "0"
            if use_locator and res.get("tagged"):
                try:
                    loc_timeout = int(os.getenv("BACOPY_CLICK_BET_LOCATOR_TIMEOUT_MS", "1800") or 1800)
                    loc = frame.locator('[data-bacopy-bet-btn="1"]').first
                    loc.click(force=True, timeout=loc_timeout)
                    self._last_click_bet_page_coords = {
                        "x": btn_x, "y": btn_y, "qpid": str(qpid or ""),
                        "side": str(side or ""), "expected_game_id": str(expected_game_id or ""),
                        "at": time.time(), "method": "locator",
                    }
                    logger.info(
                        f"[CLICK-BET-JS] locator.click OK qpid={qpid!r} side={side!r} "
                        f"src={res.get('source')!r} wasActive={res.get('wasActive')}"
                    )
                    try:
                        frame.evaluate(
                            "() => { for (const e of document.querySelectorAll('[data-bacopy-bet-btn]')) e.removeAttribute('data-bacopy-bet-btn'); }"
                        )
                    except Exception:
                        pass
                    return True
                except Exception as loc_ex:
                    logger.warning(
                        f"[CLICK-BET-JS] locator.click FAILED ({loc_ex}); "
                        f"checking double-stake guard before mouse fallback"
                    )
                    if _bet_already_landed(int(os.getenv("BACOPY_CLICK_BET_LANDED_WAIT_MS", "2500") or 2500)):
                        self._last_click_bet_page_coords = {
                            "x": btn_x, "y": btn_y, "qpid": str(qpid or ""),
                            "side": str(side or ""), "expected_game_id": _exp_gid,
                            "at": time.time(), "method": "locator_landed_after_raise",
                        }
                        logger.warning(
                            f"[CLICK-BET-JS] DOUBLE-STAKE GUARD: locator raised but "
                            f"<lpbet gId={_exp_gid}> already sent — wager landed, NOT re-clicking"
                        )
                        return True
                    logger.warning(
                        "[CLICK-BET-JS] no lpbet yet — falling back to mouse.click(page coords)"
                    )

            # ── Fallback path: absolute page coords + page.mouse.click ──
            page_x, page_y = btn_x, btn_y
            try:
                frame_el = frame.frame_element()
                bbox = frame_el.bounding_box()
                if bbox:
                    page_x = bbox["x"] + btn_x
                    page_y = bbox["y"] + btn_y
                    logger.info(
                        f"[CLICK-BET-JS] frame offset bbox=({bbox['x']:.1f},{bbox['y']:.1f}) "
                        f"btn=({btn_x:.1f},{btn_y:.1f}) → page=({page_x:.1f},{page_y:.1f})"
                    )
                else:
                    logger.warning(
                        "[CLICK-BET-JS] frame.bounding_box()=None; using frame-relative coords (click may miss)"
                    )
            except Exception as _fe:
                logger.warning(
                    f"[CLICK-BET-JS] frame_element offset FAILED ({_fe}); "
                    f"using frame-relative coords (click may miss)"
                )

            # Final double-stake check: an lpbet may have landed from a prior
            # path's dispatched-but-raised click while we computed page coords.
            if _bet_already_landed(0):
                logger.warning(
                    f"[CLICK-BET-JS] DOUBLE-STAKE GUARD: <lpbet gId={_exp_gid}> already "
                    f"sent before mouse fallback — skipping click to avoid double stake"
                )
                return True
            try:
                page.mouse.move(page_x, page_y, steps=8)
            except TypeError:
                page.mouse.move(page_x, page_y)
            page.wait_for_timeout(180)
            page.mouse.click(page_x, page_y)
            self._last_click_bet_page_coords = {
                "x": page_x,
                "y": page_y,
                "qpid": str(qpid or ""),
                "side": str(side or ""),
                "expected_game_id": str(expected_game_id or ""),
                "at": time.time(),
                "method": "mouse",
            }
            logger.info(f"[CLICK-BET-JS] mouse.click at ({page_x:.1f}, {page_y:.1f}) wasActive={res.get('wasActive')}")
            return True
        except Exception as ex:
            self._last_click_bet_error = {"reason": "exception", "error": str(ex)}
            logger.warning(f"[CLICK-BET-JS] error: {ex}")
            return False

    _JS_SELECTOR_COORDS = r"""
(selector) => {
  const nodes = Array.from(document.querySelectorAll(selector));
  if (!nodes.length) return {ok:false, reason:'not_found', selector};
  function visibleBox(el) {
    try {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      if (!r || r.width <= 4 || r.height <= 4 || cs.visibility === 'hidden' || cs.display === 'none') return null;
      return r;
    } catch(_) {
      return null;
    }
  }
  let el = null;
  let r = null;
  let picked = -1;
  for (let i = 0; i < nodes.length; i++) {
    const nr = visibleBox(nodes[i]);
    if (nr) {
      el = nodes[i];
      r = nr;
      picked = i;
      break;
    }
  }
  if (!el) {
    const first = nodes[0];
    const fr = first && first.getBoundingClientRect ? first.getBoundingClientRect() : null;
    return {ok:false, reason:'not_visible', selector, matches:nodes.length,
            x: fr ? fr.left : 0, y: fr ? fr.top : 0,
            w: fr ? fr.width : 0, h: fr ? fr.height : 0};
  }
  try { el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
  r = visibleBox(el) || r;
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const ev = { bubbles:true, cancelable:true, composed:true, clientX:cx, clientY:cy, view:window };
  try {
    if (window.PointerEvent) {
      el.dispatchEvent(new PointerEvent('pointerdown', ev));
      el.dispatchEvent(new PointerEvent('pointerup', ev));
    }
    el.dispatchEvent(new MouseEvent('mousedown', ev));
    el.dispatchEvent(new MouseEvent('mouseup', ev));
    el.dispatchEvent(new MouseEvent('click', ev));
    if (typeof el.click === 'function') el.click();
  } catch(_) {}
  return {ok:true, selector, picked, matches:nodes.length, x:cx, y:cy,
          w:r.width, h:r.height, text:String(el.innerText || el.textContent || '').trim().slice(0,80)};
}
"""

    _JS_CHIP_SCAN = r"""
() => Array.from(document.querySelectorAll('[data-testid^="chip-stack-value-"]'))
  .map((el, index) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const testid = String(el.getAttribute('data-testid') || '');
    return {
      index,
      testid,
      value: testid.replace('chip-stack-value-', ''),
      text: String(el.innerText || el.textContent || '').trim().slice(0, 80),
      visible: !!(r && r.width > 4 && r.height > 4 && cs.visibility !== 'hidden' && cs.display !== 'none'),
      x: r ? Math.round(r.left + r.width / 2) : 0,
      y: r ? Math.round(r.top + r.height / 2) : 0,
      w: r ? Math.round(r.width) : 0,
      h: r ? Math.round(r.height) : 0,
      cls: String(el.className || '').slice(0, 120)
    };
  })
"""

    def _js_click_selector(self, frame, selector: str, label: str) -> bool:
        """Click an arbitrary frame element via page.mouse for elements Playwright considers non-actionable."""
        try:
            res = frame.evaluate(self._JS_SELECTOR_COORDS, selector)
            logger.info(f"[CLICK-{label}-JS] selector={selector!r} coords={res}")
            if not (isinstance(res, dict) and res.get("ok")):
                return False
            x = float(res.get("x") or 0)
            y = float(res.get("y") or 0)
            page_x, page_y = x, y
            try:
                frame_el = frame.frame_element()
                bbox = frame_el.bounding_box()
                if bbox:
                    page_x = bbox["x"] + x
                    page_y = bbox["y"] + y
            except Exception as _fe:
                logger.debug(f"[CLICK-{label}-JS] frame_element offset failed: {_fe}")
            page = frame.page
            try:
                page.mouse.move(page_x, page_y, steps=8)
            except TypeError:
                page.mouse.move(page_x, page_y)
            page.wait_for_timeout(250 if label == "CHIP" else 120)
            page.mouse.click(page_x, page_y)
            logger.info(f"[CLICK-{label}-JS] mouse.click at ({page_x:.1f}, {page_y:.1f})")
            return True
        except Exception as ex:
            logger.warning(f"[CLICK-{label}-JS] error: {ex}")
            return False

    _CHIP_DENOMS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000]

    @staticmethod
    def _fmt_chip_denom(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    def _available_chip_denoms(self, frame) -> list[float]:
        """Read currently rendered chip denominations from the Pragmatic frame."""
        try:
            chips = frame.evaluate(self._JS_CHIP_SCAN)
            if isinstance(chips, list):
                logger.info(f"[AUTO-PROBE] chip scan count={len(chips)} chips={chips[:12]}")
                vals = []
                for item in chips:
                    if not isinstance(item, dict):
                        continue
                    try:
                        vals.append(float(item.get("value") or 0))
                    except Exception:
                        pass
                denoms = sorted({float(v) for v in vals if float(v) > 0})
                if denoms:
                    return denoms
        except Exception as ex:
            logger.debug(f"[CLICK-BET] chip denom scan failed: {ex}")
        return list(self._CHIP_DENOMS)

    def _chip_plan(
        self, amount: float, denoms: list[float] | None = None, *, log_failure: bool = True
    ) -> list[float]:
        """Decompose a bet amount into rendered chip denominations."""
        choices = sorted({float(d) for d in (denoms or self._CHIP_DENOMS) if float(d) > 0}, reverse=True)
        if not choices:
            return []
        denoms_key = tuple(sorted(choices))
        amount_key = int((Decimal(str(amount)) * Decimal("10")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        cache_key = (denoms_key, amount_key)
        cached = self._chip_plan_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        scale = Decimal("10")
        remaining = amount_key
        plan: list[float] = []
        for d in choices:
            units = int((Decimal(str(d)) * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if units <= 0:
                continue
            count, remaining = divmod(remaining, units)
            if count:
                plan.extend([d] * int(count))
        if remaining:
            if log_failure:
                logger.error(
                    f"[CLICK-BET-PLAN] amount=${amount:.2f} cannot be exactly decomposed "
                    f"with rendered chips {choices}; aborting to avoid wrong stake"
                )
            self._chip_plan_cache[cache_key] = []
            return []
        self._chip_plan_cache[cache_key] = list(plan)
        return plan

    def _prewarm_small_seq_chip_plans(self, denoms: list[float]) -> None:
        """Precompute fixed SMALL SEQ chip click plans for the current chip set."""
        denoms_key = tuple(sorted({float(d) for d in denoms if float(d) > 0}))
        if not denoms_key or denoms_key in self._chip_plan_prewarmed_keys:
            return
        amounts: list[float] = []
        try:
            from dual_line_money import SEQ_SMALL02, SEQ_SMALL1, SEQ_SMALL3, SEQ_SMALL6

            for seq in (SEQ_SMALL02, SEQ_SMALL1, SEQ_SMALL3, SEQ_SMALL6):
                amounts.extend(float(v) for v in seq)
        except Exception as ex:
            logger.debug(f"[CLICK-BET-PLAN] SMALL SEQ import failed for prewarm: {ex}")
            amounts.extend([
                0.2, 0.4, 0.6, 0.8, 1, 2, 3, 4, 5, 6, 7, 10, 12, 14,
                25, 50, 100, 200, 333, 500, 1000, 2000,
            ])
        for amt in sorted(set(amounts)):
            self._chip_plan(amt, list(denoms_key), log_failure=False)
        self._chip_plan_prewarmed_keys.add(denoms_key)
        logger.info(
            f"[CLICK-BET-PLAN] prewarmed SMALL SEQ plans: amounts={len(set(amounts))} "
            f"denoms={list(denoms_key)} cache_size={len(self._chip_plan_cache)}"
        )

    def _select_chip_in_frame(self, frame, chip_value: float, log_prefix: str = "CLICK-BET") -> bool:
        chip_str = self._fmt_chip_denom(chip_value)
        chip_sel = f'[data-testid="chip-stack-value-{chip_str}"]'
        try:
            chips = frame.evaluate(self._JS_CHIP_SCAN)
            logger.info(
                f"[AUTO-PROBE] chip select target={chip_str} "
                f"visible={[c for c in chips if isinstance(c, dict) and c.get('visible')][:12] if isinstance(chips, list) else chips}"
            )
        except Exception as ex:
            logger.info(f"[AUTO-PROBE] chip select scan failed target={chip_str}: {ex}")
        # Longer default: the chip locator click was timing out at 800ms in
        # chrome_attach (OOPIF actionability is slower; a hidden duplicate chip
        # could also stall scrollIntoViewIfNeeded). 2.5s + visible-first fixes it.
        chip_timeout_ms = int(float(os.getenv("BACOPY_CHIP_SELECT_TIMEOUT_SEC", "2.5") or 2.5) * 1000)
        # ── Primary: trusted Playwright click on the stable data-testid. ──
        # The chip MUST be "held" (isTrusted click) before the bet zone is
        # clicked, otherwise the wager is never placed (same rule the human
        # follows: tap a chip first). The legacy _js_click_selector path uses
        # synthetic events + computed coords, which (a) are not trusted and
        # (b) miss in the nested OOPIF multibaccarat frame — so the chip was
        # never actually picked up. Element-direct locator click fixes both.
        # visible-first avoids grabbing a hidden duplicate chip element.
        use_locator = os.getenv("BACOPY_CHIP_USE_LOCATOR", "1").strip() != "0"
        if use_locator:
            for sel in (f"{chip_sel} >> visible=true", chip_sel):
                try:
                    frame.locator(sel).first.click(force=True, timeout=max(800, chip_timeout_ms))
                    logger.info(f"[{log_prefix}] chip selected (locator trusted): {chip_str} (selector={sel})")
                    try:
                        frame.page.wait_for_timeout(80)
                    except Exception:
                        pass
                    return True
                except Exception as ce:
                    logger.warning(f"[{log_prefix}] chip locator click failed ({sel}): {ce}")
        try:
            frame.click(chip_sel, timeout=max(250, chip_timeout_ms))
            logger.info(f"[{log_prefix}] chip selected: {chip_str} (selector={chip_sel})")
            return True
        except Exception as ce:
            logger.warning(f"[{log_prefix}] chip select failed ({chip_sel}): {ce}; trying JS mouse fallback")
            if self._js_click_selector(frame, chip_sel, "CHIP"):
                logger.info(f"[{log_prefix}] chip selected via JS mouse: {chip_str} (selector={chip_sel})")
                try:
                    frame.page.wait_for_timeout(120)
                except Exception:
                    pass
                return True
        return False

    def _amount_key(self, amount: float) -> int:
        return int((Decimal(str(amount)) * Decimal("10")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _preselect_first_chip(self, target_qpid: str, amount: float) -> bool:
        """Select the first required chip while waiting for the target betsopen."""
        frame = self._find_pragmatic_frame(target_qpid=target_qpid)
        if not frame:
            logger.info(f"[CHIP-PRESELECT] skip: frame not found target={target_qpid!r}")
            return False
        rendered_denoms = self._available_chip_denoms(frame)
        self._prewarm_small_seq_chip_plans(rendered_denoms)
        # 機能②: 手動アシストではチップ事前選択を固定基準額($1既定)にする。
        # SEQ進行で次BETが$5等になると chip_plan の先頭=大チップが選ばれ、慌てて連打
        # すると過大BET事故になるため。人間が手動でチップ選択+回数を決める運用に合わせ、
        # アクティブチップを基準額に固定する(表示NEXT BET額は SEQ のまま=変更しない)。
        manual_base = os.getenv("BACOPY_MANUAL_CHIP_BASE", "").strip()
        if manual_base:
            try:
                want = float(manual_base)
            except Exception:
                want = 1.0
            if want > 0:
                pick = want if want in rendered_denoms else (
                    min(rendered_denoms) if rendered_denoms else want
                )
                if self._select_chip_in_frame(frame, pick, "CHIP-PRESELECT"):
                    self._preselected_chip = {
                        "target": str(target_qpid or ""),
                        "amount_key": self._amount_key(amount),
                        "chip": pick,
                        "at": time.time(),
                    }
                    logger.info(
                        f"[CHIP-PRESELECT] manual base chip=${pick:.2f} "
                        f"(BACOPY_MANUAL_CHIP_BASE={manual_base}) target={target_qpid!r} "
                        f"display_amount=${amount:.2f}"
                    )
                    return True
                logger.info(
                    f"[CHIP-PRESELECT] manual base chip=${pick:.2f} select failed; "
                    f"falling back to plan target={target_qpid!r}"
                )
        chip_plan = self._chip_plan(amount, rendered_denoms)
        if not chip_plan:
            logger.info(f"[CHIP-PRESELECT] skip: empty chip plan amount=${amount:.2f} target={target_qpid!r}")
            logger.info(
                f"[AUTO-PROBE] preselect result=empty_plan target={target_qpid!r} "
                f"amount=${amount:.2f} denoms={rendered_denoms}"
            )
            return False
        first = float(chip_plan[0])
        if not self._select_chip_in_frame(frame, first, "CHIP-PRESELECT"):
            logger.warning(
                f"[AUTO-PROBE] preselect result=failed target={target_qpid!r} "
                f"amount=${amount:.2f} first_chip={self._fmt_chip_denom(first)}"
            )
            return False
        self._preselected_chip = {
            "target": str(target_qpid or ""),
            "amount_key": self._amount_key(amount),
            "chip": first,
            "at": time.time(),
        }
        logger.info(
            f"[CHIP-PRESELECT] ready target={target_qpid!r} amount=${amount:.2f} "
            f"first_chip={self._fmt_chip_denom(first)} plan_clicks={len(chip_plan)}"
        )
        logger.info(
            f"[AUTO-PROBE] preselect result=ready target={target_qpid!r} "
            f"amount=${amount:.2f} first_chip={self._fmt_chip_denom(first)} "
            f"plan_clicks={len(chip_plan)}"
        )
        return True

    def _pick_chip_denom(self, amount: float) -> str:
        """BET額に最も近い（超えない）チップ額の文字列を返す。"""
        best = self._CHIP_DENOMS[0]
        for d in self._CHIP_DENOMS:
            if d <= amount + 0.001:
                best = d
        return str(int(best)) if best == int(best) else str(best)

    def _click_place_bet(
        self,
        side: str,
        amount: float,
        target_qpid: str = "",
        target_table_name: str = "",
        expected_game_id: str = "",
        stale_tile_game_id_ok: str = "",
    ) -> bool:
        """Pragmatic multi-play フレームでクリックBETを実行する。
        side: 'P'=Player, 'B'=Banker, 'T'=Tie, 'PP'=P-Pair, 'BP'=B-Pair
        amount: BET額 (USDT)
        target_qpid: 対象テーブルのqpid。指定するとそのテーブルのiframeを優先検索する。
        Returns True if click succeeded.
        """
        side_class = {'P': 'ym_yP', 'B': 'ym_yQ', 'T': 'ym_yB',
                      'PP': 'ym_yT', 'BP': 'ym_yU'}.get(side, 'ym_yP')

        frame = self._find_pragmatic_frame(target_qpid=target_qpid)
        if not frame:
            logger.warning("[CLICK-BET] Pragmatic frame not found")
            return False
        frame_url = str(getattr(frame, 'url', ''))
        qpid_matched = bool(target_qpid and target_qpid in frame_url)
        logger.info(f"[CLICK-BET] frame selected: qpid_matched={qpid_matched} target={target_qpid!r} frame_url={frame_url[:80]}")

        rendered_denoms = self._available_chip_denoms(frame)
        self._prewarm_small_seq_chip_plans(rendered_denoms)
        chip_plan = self._chip_plan(amount, rendered_denoms)
        if not chip_plan:
            logger.warning(f"[CLICK-BET-PLAN] empty plan amount=${amount:.2f} denoms={rendered_denoms}")
            return False
        plan_summary: dict[str, int] = {}
        for d in chip_plan:
            key = self._fmt_chip_denom(d)
            plan_summary[key] = plan_summary.get(key, 0) + 1
        logger.info(
            f"[CLICK-BET-PLAN] amount=${amount:.2f} denoms={rendered_denoms} "
            f"plan={plan_summary} clicks={len(chip_plan)}"
        )

        # multi-baccarat モードはフレームURLにqpidが入らない（1フレーム共有）
        # → JSでフレーム内DOM検索して対象テーブルのボタンを特定してクリック
        # (qpid_matched=False でも JS は実行し、フォールバック含めて試みる)
        def is_tile_game_id_mismatch() -> bool:
            err = self._last_click_bet_error or {}
            return str(err.get("reason") or "") == "game_id_mismatch"

        def wait_for_matching_tile(click_index: int) -> bool:
            wait_sec = float(os.getenv("BACOPY_TILE_GAME_ID_WAIT_SEC", "8.0") or 8.0)
            refocus_after = float(os.getenv("BACOPY_TILE_GAME_ID_REFOCUS_AFTER_SEC", "3.0") or 3.0)
            refocus_max = int(os.getenv("BACOPY_TILE_GAME_ID_REFOCUS_MAX", "0") or 0)
            decoy_after = float(os.getenv("BACOPY_TILE_GAME_ID_DECOY_AFTER_SEC", "2.0") or 2.0)
            decoy_enabled = os.getenv("BACOPY_TILE_GAME_ID_DECOY", "1") != "0"
            started_at = time.time()
            deadline = started_at + max(0.0, wait_sec)
            last_recovery_at = 0.0
            refocus_count = 0
            decoy_done = False
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                try:
                    frame.page.wait_for_timeout(180)
                except Exception:
                    time.sleep(0.18)
                self._hover_multi_tile(frame, target_qpid)
                if self._js_click_bet_in_container(
                    frame,
                    target_qpid,
                    target_table_name,
                    side_class,
                    side,
                    expected_game_id,
                    stale_tile_game_id_ok,
                ):
                    logger.info(
                        f"[CLICK-BET] JS click OK after tile game-id wait: "
                        f"side={side} qpid={target_qpid!r} attempt={attempt} "
                        f"click={click_index}/{len(chip_plan)} refocus={refocus_count} "
                        f"decoy={'1' if decoy_done else '0'}"
                    )
                    return True
                if not is_tile_game_id_mismatch():
                    return False
                # Stuck on game_id_mismatch. Hover alone does not refresh the
                # tile's WS-driven data subscription. Two-tier recovery:
                # 1) Refocus via _focus_table_in_multi(intent='prepare') —
                #    re-scrolls and re-clicks the same tile (cheap).
                # 2) Decoy resubscribe — Pragmatic ignores reselecting an
                #    already-active tile, so click a different visible tile
                #    first to force deactivation, then click target back to
                #    trigger a fresh subscribe (heavy, used as last resort).
                # The hand/game-id guard is unchanged: every retry below goes
                # through _js_click_bet_in_container which still rejects any
                # click while currentGameId != expectedGameId.
                elapsed = time.time() - started_at
                since_recovery = time.time() - last_recovery_at if last_recovery_at else 9999.0
                if decoy_enabled and not decoy_done and elapsed >= decoy_after and since_recovery >= 1.0:
                    try:
                        logger.info(
                            f"[CLICK-BET] decoy resubscribe: "
                            f"qpid={target_qpid!r} attempt={attempt} elapsed={elapsed:.1f}s "
                            f"expected={expected_game_id!r} "
                            f"current={(self._last_click_bet_error or {}).get('currentGameId') or '-'}"
                        )
                        self._decoy_click_to_resubscribe(frame, target_qpid, target_table_name)
                    except Exception as ex:
                        logger.debug(f"[CLICK-BET] decoy resubscribe failed: {ex}")
                    decoy_done = True
                    last_recovery_at = time.time()
                    continue
                if refocus_count < refocus_max and elapsed >= refocus_after and since_recovery >= refocus_after:
                    try:
                        logger.info(
                            f"[CLICK-BET] active-panel refresh: "
                            f"qpid={target_qpid!r} attempt={attempt} elapsed={elapsed:.1f}s "
                            f"refocus={refocus_count + 1}/{refocus_max} "
                            f"expected={expected_game_id!r}"
                        )
                        self._focus_table_in_multi({
                            "table_id": str(target_qpid or ""),
                            "table_name": str(target_table_name or ""),
                            "qpid": str(target_qpid or ""),
                            "intent": "prepare",
                            "side": str(side or ""),
                        })
                    except Exception as ex:
                        logger.debug(f"[CLICK-BET] active-panel refresh failed: {ex}")
                    last_recovery_at = time.time()
                    refocus_count += 1
                    continue
            logger.warning(
                f"[CLICK-BET] tile game-id did not catch up: qpid={target_qpid!r} "
                f"expected={expected_game_id!r} refocus={refocus_count} "
                f"decoy={'1' if decoy_done else '0'} last={self._last_click_bet_error}"
            )
            return False

        def click_side_once(click_index: int) -> bool:
            self._hover_multi_tile(frame, target_qpid)
            if self._js_click_bet_in_container(
                frame,
                target_qpid,
                target_table_name,
                side_class,
                side,
                expected_game_id,
                stale_tile_game_id_ok,
            ):
                logger.info(
                    f"[CLICK-BET] JS click OK: side={side} qpid={target_qpid!r} "
                    f"qpid_matched={qpid_matched} click={click_index}/{len(chip_plan)}"
                )
                return True

            if is_tile_game_id_mismatch():
                return wait_for_matching_tile(click_index)

            if target_qpid and not qpid_matched:
                try:
                    # Manual-assist 時代、NOW 点灯後は窓が ~12s 開いており人間は余裕で
                    # 手動 BET できた。自動でも同じ時間予算を使い切るべき。compact tile に
                    # P/B ボタンがまだ描画されていない (not_found) 場合、窓が開いている間
                    # ずっとタイルを再アクティブ化しながらボタン出現を待ってクリックする。
                    # game_id ガードにより窓が閉じて次ハンドへ変わると全クリックが拒否される
                    # ため、別ハンドへの誤 BET は起きない (最悪でも返却=損失なし)。
                    retry_sec = float(os.getenv("BACOPY_MULTI_BUTTON_WAIT_SEC", "10.0") or 10.0)
                    started_reseek = time.time()
                    deadline = started_reseek + max(0.5, retry_sec)
                    attempt = 0

                    def _window_closed_now() -> bool:
                        # 「確実に閉じた」時だけ True。状態不明 (open_gid 空) では
                        # 早すぎる中断を避けるため False を返し、リトライを継続する。
                        st_w = self._table_states.get(str(target_qpid or "")) or {}
                        og = str(st_w.get("bets_open_game_id") or "")
                        cg = str(st_w.get("bets_closed_game_id") or "")
                        return bool(og and cg and og == cg)

                    while time.time() < deadline:
                        attempt += 1
                        if attempt > 1 and _window_closed_now():
                            logger.info(
                                f"[CLICK-BET-RESEEK] stop: bet window closed "
                                f"qpid={target_qpid!r} attempt={attempt} "
                                f"elapsed={time.time() - started_reseek:.1f}s"
                            )
                            break
                        reseek = frame.evaluate(
                            _MULTI_LOBBY_FOCUS_JS,
                            {
                                            "qpid": target_qpid,
                                            "click": True,
                                            "maxScroll": int(os.getenv("BACOPY_MULTI_CLICK_RESEEK_SCROLL_MAX", "12") or "12"),
                                            "candidates": [target_table_name, target_qpid],
                                            "hintIndex": -1,
                                            "hintTotal": 0,
                                            "hintScrollTop": int((self._table_focus_cache.get(str(target_qpid or ""), {}) or {}).get("scroll_top", -1)),
                                            "hintScrollRatio": float((self._table_focus_cache.get(str(target_qpid or ""), {}) or {}).get("scroll_ratio", -1)),
                                        },
                                    )
                        logger.info(
                            f"[CLICK-BET-RESEEK] qpid={target_qpid!r} attempt={attempt} "
                            f"elapsed={time.time() - started_reseek:.1f}s/{retry_sec:.0f}s result={reseek}"
                        )
                        if isinstance(reseek, dict) and reseek.get("found"):
                            try:
                                frame.page.wait_for_timeout(350)
                            except Exception:
                                pass
                            self._hover_multi_tile(frame, target_qpid)
                            if self._js_click_bet_in_container(
                                frame,
                                target_qpid,
                                target_table_name,
                                side_class,
                                side,
                                expected_game_id,
                                stale_tile_game_id_ok,
                            ):
                                logger.info(
                                    f"[CLICK-BET] JS click OK after reseek: side={side} "
                                    f"qpid={target_qpid!r} attempt={attempt} "
                                    f"elapsed={time.time() - started_reseek:.1f}s click={click_index}/{len(chip_plan)}"
                                )
                                return True
                        frame.page.wait_for_timeout(250)
                    logger.warning(
                        f"[CLICK-BET-RESEEK] exhausted qpid={target_qpid!r} attempts={attempt} "
                        f"elapsed={time.time() - started_reseek:.1f}s budget={retry_sec:.0f}s "
                        f"last_err={self._last_click_bet_error}"
                    )
                except Exception as ex:
                    logger.warning(f"[CLICK-BET-RESEEK] failed qpid={target_qpid!r}: {ex}")

            if not qpid_matched:
                # マルチロビー共有フレームで JS 完全失敗 → no fallback (別テーブル誤クリックのリスク)
                logger.warning(f"[CLICK-BET] JS failed for qpid={target_qpid!r} in multi-lobby — no selector fallback")
                return False

            # 単一テーブルフレーム (qpid_matched=True) のセレクターフォールバック
            for sel in [f'.{side_class}.ym_yn', f'.{side_class}']:
                try:
                    frame.click(sel, timeout=2000)
                    logger.info(
                        f"[CLICK-BET] bet placed (selector fallback): side={side} "
                        f"selector={sel} click={click_index}/{len(chip_plan)}"
                    )
                    return True
                except Exception as e:
                    logger.debug(f"[CLICK-BET] frame.click({sel}) failed: {e}")

            logger.warning(f"[CLICK-BET] all selectors failed for side={side}")
            return False

        current_selected_chip: float | None = None
        cached_click_coords: dict[str, Any] | None = None
        # 多チップ満額検証用: この game の送出 <lpbet> 累計のベースライン（着弾チップ数=lpbet増分）
        _verify_gid = str(expected_game_id or "")
        _lpbet_base = self._lpbet_count_by_gid.get(_verify_gid, 0) if _verify_gid else 0

        def click_cached_side_once(click_index: int) -> bool:
            nonlocal cached_click_coords
            coords = cached_click_coords or {}
            if not coords:
                return False
            try:
                if str(coords.get("qpid") or "") != str(target_qpid or ""):
                    return False
                if str(coords.get("side") or "") != str(side or ""):
                    return False
                if str(coords.get("expected_game_id") or "") != str(expected_game_id or ""):
                    return False
                if time.time() - float(coords.get("at") or 0.0) > 8.0:
                    return False
                x = float(coords.get("x") or 0.0)
                y = float(coords.get("y") or 0.0)
                if x <= 0 or y <= 0:
                    return False
                # ── 座標系の補正（多チップ過少BETの根本修正）──
                # locator/text_locator 経路は btn の FRAME相対座標を保存する。だが
                # frame.page.mouse.click() は PAGE(viewport)座標を要求する。マルチロビーの
                # OOPIF では frame が画面内にオフセット(例 +147,+100)しているため、frame相対を
                # そのまま渡すと毎回ベットゾーンを外し 2枚目以降が一切着弾しなかった($4→$1)。
                # iframe オフセットを加算して PAGE 座標に直す。mouse 経路の座標は既に PAGE。
                meth = str(coords.get("method") or "")
                if meth in ("text_locator", "locator", "text_locator_landed_after_raise", "locator_landed_after_raise"):
                    try:
                        bbox = frame.frame_element().bounding_box()
                        if bbox:
                            x = float(bbox["x"]) + x
                            y = float(bbox["y"]) + y
                    except Exception as _bx:
                        logger.debug(f"[CLICK-BET] cached coord frame-offset failed: {_bx}")
                if x <= 0 or y <= 0:
                    return False
                frame.page.mouse.click(x, y)
                logger.info(
                    f"[CLICK-BET] cached direct click OK: side={side} qpid={target_qpid!r} "
                    f"click={click_index}/{len(chip_plan)} at=({x:.1f},{y:.1f}) method={meth or '-'}"
                )
                return True
            except Exception as ex:
                logger.debug(f"[CLICK-BET] cached direct click failed: {ex}")
                return False

        for idx, chip_value in enumerate(chip_plan, start=1):
            pre = self._preselected_chip or {}
            chip_changed_this_click = False
            reuse_preselected_chip = os.getenv("BACOPY_REUSE_PRESELECTED_CHIP", "0").strip() == "1"
            can_skip_preselect = (
                reuse_preselected_chip
                and
                idx == 1
                and str(pre.get("target") or "") == str(target_qpid or "")
                and int(pre.get("amount_key") or -1) == self._amount_key(amount)
                and abs(float(pre.get("chip") or 0.0) - float(chip_value)) < 0.0001
                and 0.0 <= time.time() - float(pre.get("at") or 0.0) <= float(os.getenv("BACOPY_CHIP_PRESELECT_MAX_AGE_SEC", "90") or 90)
            )
            if can_skip_preselect:
                logger.info(
                    f"[CLICK-BET] chip preselect reused: {self._fmt_chip_denom(chip_value)} "
                    f"click={idx}/{len(chip_plan)}"
                )
                current_selected_chip = float(chip_value)
            elif current_selected_chip is not None and abs(float(current_selected_chip) - float(chip_value)) < 0.0001:
                logger.info(
                    f"[CLICK-BET] chip selection kept: {self._fmt_chip_denom(chip_value)} "
                    f"click={idx}/{len(chip_plan)}"
                )
            elif not self._select_chip_in_frame(frame, chip_value, "CLICK-BET"):
                logger.warning(
                    f"[CLICK-BET] abort: chip select failed value={self._fmt_chip_denom(chip_value)} "
                    f"click={idx}/{len(chip_plan)}"
                )
                return False
            else:
                current_selected_chip = float(chip_value)
                chip_changed_this_click = True
            used_cached_click = False
            if idx > 1 and cached_click_coords is not None and not chip_changed_this_click:
                used_cached_click = click_cached_side_once(idx)
            if not used_cached_click:
                if not click_side_once(idx):
                    return False
                coords = getattr(self, "_last_click_bet_page_coords", None)
                cached_click_coords = dict(coords) if isinstance(coords, dict) else None
            try:
                delay_ms = int(os.getenv("BACOPY_CLICK_BET_INTER_CLICK_MS", "45") or 45)
                frame.page.wait_for_timeout(max(0, delay_ms))
            except Exception:
                pass

        # ── 多チップ満額検証 ＋ 不足リトライ ──
        # 送出 <lpbet> 回数(=実際に乗ったチップ数)を計画枚数と照合し、不足分を再クリックする。
        # lpbet 反映には ~1.5s の遅延があるため十分待ってから判定（早すぎる誤判定→過大BET防止）。
        # リトライは「均一プラン($1×N 等)」のみ実施=どのチップが欠けても同一額なので再クリック安全。
        # 混在プラン($6=$5+$1 等)は欠けたチップ額を特定できないためログのみ（過大BETを作らない）。
        planned_clicks = len(chip_plan)
        if _verify_gid and planned_clicks > 1:
            uniform_plan = len(set(float(d) for d in chip_plan)) == 1
            rounds = int(os.getenv("BACOPY_CHIP_VERIFY_RETRY_ROUNDS", "2") or 2)
            settle_ms = int(os.getenv("BACOPY_CHIP_VERIFY_SETTLE_MS", "2500") or 2500)
            uchip = float(chip_plan[0])

            def _landed_now() -> int:
                return self._lpbet_count_by_gid.get(_verify_gid, 0) - _lpbet_base

            def _wait_lpbets(target: int) -> int:
                dl = time.time() + max(0, settle_ms) / 1000.0
                while time.time() < dl:
                    if _landed_now() >= target:
                        break
                    try:
                        frame.page.wait_for_timeout(120)
                    except Exception:
                        time.sleep(0.12)
                return _landed_now()

            landed = _wait_lpbets(planned_clicks)
            for vr in range(rounds):
                shortfall = planned_clicks - landed
                if shortfall <= 0:
                    break
                if not uniform_plan:
                    logger.warning(
                        f"[CLICK-BET-VERIFY] mixed-plan shortfall (no auto-retry): gId={_verify_gid} "
                        f"landed={landed}/{planned_clicks} plan={plan_summary} amount=${amount:.2f}"
                    )
                    break
                logger.warning(
                    f"[CLICK-BET-VERIFY] shortfall: gId={_verify_gid} landed={landed}/{planned_clicks} "
                    f"chip={self._fmt_chip_denom(uchip)} round={vr + 1}/{rounds} — retrying {shortfall} click(s)"
                )
                for _ in range(shortfall):
                    did_click = False
                    if cached_click_coords is not None:
                        did_click = click_cached_side_once(planned_clicks)
                    if not did_click:
                        if click_side_once(planned_clicks):
                            c2 = getattr(self, "_last_click_bet_page_coords", None)
                            if isinstance(c2, dict):
                                cached_click_coords = dict(c2)
                    try:
                        frame.page.wait_for_timeout(max(0, int(os.getenv("BACOPY_CLICK_BET_INTER_CLICK_MS", "45") or 45)))
                    except Exception:
                        pass
                landed = _wait_lpbets(planned_clicks)
            if landed >= planned_clicks:
                logger.info(
                    f"[CLICK-BET-VERIFY] full amount landed: gId={_verify_gid} "
                    f"chips={landed}/{planned_clicks} amount=${amount:.2f}"
                )
            else:
                logger.warning(
                    f"[CLICK-BET-VERIFY] STILL short: gId={_verify_gid} landed={landed}/{planned_clicks} "
                    f"amount=${amount:.2f} — partial bet will be flagged downstream"
                )

        logger.info(f"[CLICK-BET] all planned chip clicks completed: side={side} amount=${amount:.2f} clicks={len(chip_plan)}")
        return True

    def _ws_send(self, table_id: str, payload: str) -> dict:
        """game WS にメッセージを送信。"""
        match = f"tableId={table_id}"
        st = self._table_states.get(table_id) or {}
        ws_url = str(st.get("ws_url") or self._game_ws_url or "").strip()

        def _note_direct_lpbet_sent(mode: str, channel: str) -> None:
            if "<lpbet" not in payload:
                return
            m_gid = re.search(r'gId="([^"]+)"', payload)
            m_ck = re.search(r'\bck="([^"]+)"', payload)
            gid = str(m_gid.group(1) if m_gid else "").strip()
            ck = str(m_ck.group(1) if m_ck else "").strip()
            self._last_lpbet_gid = gid
            self._last_lpbet_at = time.time()
            logger.info(
                f"[WS-BET-SEND] direct lpbet sent marker mode={mode} "
                f"channel={channel or '-'} gId={gid or '-'} ck={ck or '-'}"
            )

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
        proxy_server = (
            self._ws_proxy_servers.get(table_id) or self._ws_proxy_server
            if self._multi_lobby_mode else None
        )
        logger.info(
            f"[WS-SEND-PATH] table={table_id or '-'} ws_url={ws_url[:60] if ws_url else 'NONE'} "
            f"proxy={'SET' if proxy_server else 'NONE'} "
            f"proxy_channels={list(self._ws_proxy_servers.keys())} "
            f"proxy_global={'SET' if self._ws_proxy_server else 'NONE'} "
            f"worker_bridge={bool(self._multi_lobby_mode)}"
        )
        if proxy_server is not None:
            try:
                proxy_server.send(payload)
                _note_direct_lpbet_sent("ws_proxy", table_id)
                logger.info(f"[WS-SEND] ws_proxy_server.send OK channel={table_id}")
                return {"ok": True, "mode": "ws_proxy", "channel": table_id}
            except Exception as _e:
                logger.warning(f"[WS-SEND] ws_proxy_server.send failed: {_e}")
                with self._lock:
                    if self._ws_proxy_servers.get(table_id) is proxy_server:
                        self._ws_proxy_servers.pop(table_id, None)
                    if self._ws_proxy_server is proxy_server:
                        self._ws_proxy_server = None
                        self._ws_proxy_route = None

        # 1. Worker ブリッジ経由（multi-lobby モードの次点ルート）
        if self._multi_lobby_mode:
            res_w = _try_worker_send()
            if isinstance(res_w, dict) and res_w.get("ok"):
                _note_direct_lpbet_sent("worker_bridge", table_id)
                return res_w
            logger.info(f"[WS-SEND] worker bridge result: {res_w} — falling back")

        # 2. 並列 WS open_send
        # In multi-lobby the Pragmatic socket often lives outside page JS
        # (Playwright sees frames, but window/Worker bridges may have 0 sendable
        # sockets). Use the captured game ws_url as a targeted fallback.
        if ws_url:
            logger.info(
                f"[WS-SEND] trying open_send url fallback multi={self._multi_lobby_mode} "
                f"channel={table_id}"
            )
            res = _try_open_send(ws_url)
            if isinstance(res, dict) and res.get("ok"):
                _note_direct_lpbet_sent("open_send", table_id)
                return res

        # 3. 既存 __bacopy_ws_send（ページ文脈の WS）
        first = _send_once()
        if isinstance(first, dict) and first.get("ok"):
            _note_direct_lpbet_sent("page_bridge", table_id)
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
                _note_direct_lpbet_sent("worker_bridge_retry", table_id)
                return res_w2
        if ws_url and not self._multi_lobby_mode:
            res2 = _try_open_send(ws_url)
            if isinstance(res2, dict) and res2.get("ok"):
                _note_direct_lpbet_sent("open_send_retry", table_id)
                return res2
        second = _send_once()
        if isinstance(second, dict) and second.get("ok"):
            _note_direct_lpbet_sent("page_bridge_retry", table_id)
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
        # WS transport already observes every multi-table window; it does not navigate a tile.
        already_in = (
            (self._phase == "ready" and target and self._table_id == target)
            or (self._multi_lobby_mode and target and self._prepared_table_id == target)
            or (self._multi_lobby_mode and target and bool(md.get("antenna_ok")))
            or (self._multi_lobby_mode and self._multi_bet_transport == "ws")
        )
        # switch が進行中 or 要求済みで同一テーブルなら二重ナビゲーション不要
        pending_req = (self._switch_request or {}).get("table_id", "") or (self._switch_request or {}).get("qpid", "")
        active_req = self._active_switch_request or {}
        active_req_target = str(active_req.get("table_id", "") or active_req.get("qpid", ""))
        already_switching_to_target = bool(
            (pending_req and pending_req == target)
            or (self._switch_in_progress and active_req_target and active_req_target == target)
        )
        if not already_in and not already_switching_to_target:
            # multi-lobby: scroll のみ（クリックで個別テーブルへ遷移させない）
            _intent = "decision" if self._multi_lobby_mode else "prepare"
            self._request_switch(str(table_id or ""), table_name, qpid, intent=_intent)
        bet_id = f"dl_{uuid.uuid4().hex[:12]}"
        self.send_bet(side=side, amount=amount, table_id=target, bet_id=bet_id, metadata=md)
        return bet_id

    def consume_sent_bet(self, bet_id: str) -> bool:
        bid = str(bet_id or "").strip()
        if not bid:
            return False
        if bid in self._confirmed_bets:
            self._confirmed_bets.pop(bid, None)
            self._sent_bet_ids.discard(bid)
            return True
        return False

    def has_sent_bet(self, bet_id: str) -> bool:
        bid = str(bet_id or "").strip()
        if not bid:
            return False
        return bid in self._confirmed_bets

    def get_confirmed_bet(self, bet_id: str) -> dict[str, Any] | None:
        bid = str(bet_id or "").strip()
        if not bid:
            return None
        item = self._confirmed_bets.get(bid)
        return dict(item) if isinstance(item, dict) else None

    def consume_confirmed_bet(self, bet_id: str) -> dict[str, Any] | None:
        bid = str(bet_id or "").strip()
        if not bid:
            return None
        self._sent_bet_ids.discard(bid)
        item = self._confirmed_bets.pop(bid, None)
        return dict(item) if isinstance(item, dict) else None

    def consume_failed_bet(self, bet_id: str) -> dict[str, Any] | None:
        bid = str(bet_id or "").strip()
        if not bid:
            return None
        item = self._failed_bet_ids.pop(bid, None)
        return item if isinstance(item, dict) else None

    def has_failed_bet(self, bet_id: str) -> bool:
        bid = str(bet_id or "").strip()
        return bool(bid and bid in self._failed_bet_ids)

    def return_to_lobby(self) -> None:
        """テーブルを離れてロビーへ戻る。内部stateをリセットし実際にナビゲートする。

        Multi-lobby mode never leaves the lobby page (the engine lives in the
        multi-baccarat grid the entire session), so the bot's table-timeout
        watchdog calling this path was structurally a no-op for navigation
        but still wiped _prepared_table_id, throwing away a perfectly valid
        focused tile and forcing a costly re-focus on the next decision.
        Preserve the prepared tile in multi-lobby; only abandon the pending
        bet and any queued switch_request so a stale signal cannot survive.
        """
        logger.info(f"[RETURN-LOBBY] called: phase={self._phase} table={self._table_id!r} pending_bet={bool(self._pending_bet)} multi={self._multi_lobby_mode}")
        if self._multi_lobby_mode:
            with self._lock:
                self._pending_bet = None
            self._switch_request = None
            self._switch_target_table_id = ""
            logger.info(
                f"[RETURN-LOBBY] multi-lobby: prepared preserved "
                f"target={self._prepared_table_id or '-'!r} "
                f"age={time.time() - (self._prepared_at or 0.0):.1f}s"
            )
            return
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
        if self._prepared_table_id:
            logger.warning(
                f"[PREPARED-CLEAR] reason=return_to_lobby "
                f"prev_target={self._prepared_table_id!r} "
                f"age={time.time() - (self._prepared_at or 0.0):.1f}s"
            )
        self._prepared_table_id = ""
        self._prepared_at = 0.0
        self._pending_bet = None
        page = self._bet_page or self._lobby_page
        if page is not None:
            try:
                page.goto(
                    PRAGMATIC_BACCARAT_LOBBY_URL,
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                logger.info("[LIVE] returned to lobby")
            except Exception as e:
                logger.warning(f"[LIVE] return_to_lobby navigation failed: {e}")

    def _request_switch(
        self,
        table_id: str = "",
        table_name: str = "",
        qpid: str = "",
        intent: str = "preposition",
        side: str = "",
        preselect_amount: float = 0.0,
        preposition_score: int = 0,
        steps_before: int = 0,
    ) -> None:
        """対象卓への switch 要求をキューに積む。"""
        tid = str(table_id or "").strip()
        if not tid and not qpid:
            return
        # 機能①拡張(HOLD): スクロール凍結中はスキャン系の卓切替を捨てる(画面を動かさない)。
        # 実BET(decision/bet)系は止めない。
        if getattr(self, "_scroll_frozen", False) and str(intent or "").strip().lower() in (
            "preposition", "prepare", "manual_assist"
        ):
            logger.info(
                f"[ASSIST-HOLD] scroll-frozen: drop switch intent={intent} target={tid or qpid}"
            )
            return
        new_req = {
            "table_id": tid or str(qpid or "").strip(),
            "table_name": str(table_name or "").strip(),
            "qpid": str(qpid or "").strip(),
            "intent": str(intent or "preposition").strip().lower(),
            "side": str(side or "").strip().upper(),
            "preselect_amount": float(preselect_amount or 0.0),
            "preposition_score": int(preposition_score or 0),
            "steps_before": int(steps_before or 0),
            "requested_at": time.time(),
        }
        priorities = {"preposition": 10, "prepare": 20, "manual_assist": 25, "decision": 30, "bet": 30, "fallback_join": 40}
        new_intent = str(new_req["intent"])
        new_target = str(new_req["qpid"] or new_req["table_id"])
        now = time.time()
        bot_lock = self._bot_now_lock_active()
        if (
            self._multi_lobby_mode
            and bot_lock
            and new_target
            and new_target != str(bot_lock.get("table_id") or "")
            and new_intent in ("preposition", "prepare", "manual_assist")
        ):
            logger.info(
                f"[BOT-NOW-LOCK] drop switch intent={new_intent} target={new_target} "
                f"keep={bot_lock.get('table_id') or '-'} "
                f"did={str(bot_lock.get('decision_id') or '-')[:12]}"
            )
            return
        if self._multi_lobby_mode and new_intent == "preposition" and new_target:
            blocked_until = float(self._preposition_focus_block_until.get(new_target) or 0.0)
            pre_score = int(new_req.get("preposition_score") or 0)
            steps = int(new_req.get("steps_before") or 0)
            urgent = pre_score >= 2 or steps == 1
            if blocked_until > now and not urgent:
                logger.info(
                    f"[SWITCH] drop preposition focus-cooldown: "
                    f"target={new_target} wait={blocked_until - now:.1f}s"
                )
                return
            if blocked_until > now and urgent:
                logger.info(
                    f"[SWITCH] bypass preposition focus-cooldown for urgent hint: "
                    f"target={new_target} score={pre_score} steps={steps} "
                    f"wait={blocked_until - now:.1f}s"
                )
                self._preposition_focus_block_until.pop(new_target, None)
        hold = self._visible_bet_hold or {}
        hold_target = str(hold.get("table_id") or "")
        hold_until = float(hold.get("until") or 0.0)
        now_bet_hold = self._active_now_bet_hold or {}
        now_bet_target = str(now_bet_hold.get("table_id") or "")
        now_bet_until = float(now_bet_hold.get("until") or 0.0)
        if (
            self._multi_lobby_mode
            and now_bet_target
            and new_target
            and new_target != now_bet_target
        ):
            lock_for_hold = self._bot_now_lock_active()
            lock_table = str(lock_for_hold.get("table_id") or "")
            if not lock_table:
                logger.info(
                    f"[NOW-BET-HOLD] clear stale hold before new focus: "
                    f"old={now_bet_target} new={new_target} intent={new_intent}"
                )
                self._active_now_bet_hold = {}
                now_bet_target = ""
                now_bet_until = 0.0
        if (
            self._multi_lobby_mode
            and now_bet_target
            and now < now_bet_until
            and new_target != now_bet_target
            and new_intent in ("preposition", "prepare", "manual_assist", "decision", "bet")
        ):
            logger.info(
                f"[NOW-BET-HOLD] drop focus while active NOW bet is locked: "
                f"keep={now_bet_target} drop={new_target or '-'} intent={new_intent} "
                f"remaining={now_bet_until - now:.1f}s"
            )
            return
        if (
            self._multi_lobby_mode
            and hold_target
            and now < hold_until
            and new_target != hold_target
            and new_intent in ("preposition", "prepare", "manual_assist", "decision", "bet")
        ):
            logger.info(
                f"[VISIBLE-HOLD] drop focus while showing bet/result: "
                f"keep={hold_target} drop={new_target or '-'} intent={new_intent} "
                f"remaining={hold_until - now:.1f}s"
            )
            return
        current = self._switch_request or {}
        cur_intent = str(current.get("intent") or "")
        cur_target = str(current.get("qpid") or current.get("table_id") or "")
        cur_age = max(0.0, now - float(current.get("requested_at") or 0.0)) if current else 0.0
        if current and priorities.get(cur_intent, 15) > priorities.get(new_intent, 15) and cur_age < 8.0:
            logger.debug(f"[SWITCH] keep higher priority request {cur_intent}:{cur_target}")
            return
        if current and cur_target == new_target and priorities.get(cur_intent, 15) >= priorities.get(new_intent, 15) and cur_age < 8.0:
            return
        if current and cur_intent == "preposition" and new_intent == "preposition" and cur_target != new_target and cur_age < 4.0:
            logger.info(f"[SWITCH] drop overlapping preposition: keep={cur_target} drop={new_target}")
            return
        self._switch_request = new_req
        logger.info(f"[SWITCH] queued intent={new_intent} target={new_target or '-'}")

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
                route_url = str(getattr(route, "url", "") or "")
                logger.info(f"[WS-PROXY] intercepted WS: {route_url[:100]}")
                server = route.connect_to_server()
                if (
                    ("pragmaticplaylive.net" in route_url or "qpidreoxcc.net" in route_url)
                    and "/game" in route_url
                ):
                    m_tid = re.search(r"[?&]tableId=([^&]+)", route_url)
                    channel_id = str(m_tid.group(1) if m_tid else "").strip()
                    is_multi = "multiTable=true" in route_url
                    with self._lock:
                        if channel_id:
                            self._ws_proxy_servers[channel_id] = server
                        if is_multi:
                            self._ws_proxy_server = server
                            self._ws_proxy_route = route
                    logger.info(
                        f"[WS-PROXY] game proxy registered channel={channel_id or '-'} "
                        f"multi={is_multi}"
                    )
                route.on_message(lambda msg, url=route_url: (_on_client_msg(url, msg), _safe_send(server, msg)))
                server.on_message(lambda msg, url=route_url: (_on_server_msg(url, msg), _safe_send(route, msg)))
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

        def _on_client_msg(url: str, msg: Any) -> None:
            try:
                self._on_proxy_ws_message(url, msg, is_recv=False)
            except Exception:
                pass

        def _on_server_msg(url: str, msg: Any) -> None:
            try:
                self._on_proxy_ws_message(url, msg, is_recv=True)
            except Exception:
                pass

        # Some embedded multi-baccarat sockets are not matched by host-based
        # patterns even though Playwright emits a websocket event for them.
        # Proxy all sockets transparently, but only register Pragmatic /game
        # channels above as BET send targets.
        for _pattern in ["**"]:
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
                        "scroll_top": int(v.get("scroll_top") or -1),
                        "scroll_height": int(v.get("scroll_height") or 0),
                        "client_height": int(v.get("client_height") or 0),
                        "scroll_ratio": float(v.get("scroll_ratio") or -1),
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
        return (
            bool(self._pending_bet)
            or bool(self._active_now_bet_hold)
            or self._switch_in_progress
            or bool(self._switch_request)
        )

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
        if self._switch_in_progress or self._pending_bet or self._active_now_bet_hold:
            return False
        if self._multi_lobby_mode:
            return True
        return self._phase == "waiting"

    def is_on_table(self, table_id: str) -> bool:
        return bool(table_id) and str(table_id).strip() == str(self._table_id).strip()
