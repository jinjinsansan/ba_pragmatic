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
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
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
  const hintScrollTop = Number.isFinite(Number(args && args.hintScrollTop)) ? Number(args && args.hintScrollTop) : -1;
  const hintScrollRatio = Number.isFinite(Number(args && args.hintScrollRatio)) ? Number(args && args.hintScrollRatio) : -1;
  const candidates = Array.isArray(args && args.candidates) ? args.candidates : [];
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
    if (String((tile && tile.id) || '').startsWith('TileHeight-') && !hasBetCells(tile)) return null;
    return target;
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
        if (tile && hasBetCells(tile)) {
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
          idx += 1;
          continue;
        }
        if (isCandidateText(textOf(el))) return { el: clickableAncestor(el), idx, total: nodes.length };
      } catch(_) {}
      idx += 1;
    }
    return { el: null, idx: -1, total: nodes.length };
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
    const delta = Math.max(240, (sc.clientHeight || 500) * 0.8) * (down ? 1 : -1);
    try { sc.scrollTop = Math.max(0, Math.min((sc.scrollHeight || 99999), (sc.scrollTop || 0) + delta)); } catch(_) {}
    try {
      const r = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
      const ev = new WheelEvent('wheel', {deltaY: delta, bubbles:true, cancelable:true, composed:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2});
      sc.dispatchEvent(ev);
    } catch(_) {}
  }
  function setScrollTop(sc, top) {
    if (!sc) return;
    try {
      const span = Math.max(0, (sc.scrollHeight || 0) - (sc.clientHeight || 0));
      sc.scrollTop = Math.max(0, Math.min(span, Math.floor(top)));
    } catch(_) {}
    try {
      const r = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
      const ev = new WheelEvent('wheel', {deltaY: 1, bubbles:true, cancelable:true, composed:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2});
      sc.dispatchEvent(ev);
    } catch(_) {}
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
  if (qpid) {
    const positions = scanPositions(cachedScroller, maxScroll);
    for (let i = 0; i < positions.length && Date.now() < deadline; i++) {
      setScrollTop(cachedScroller, positions[i]);
      await sleep(90);
      const t = findTarget();
      if (t && t.el) {
        try { t.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
        if (click) clickEl(t.el);
        return {
          ok:true, found:true, clicked: !!click,
          matchIndex: Number(t.idx), totalNodes: Number(t.total),
          scroll: scrollMeta(cachedScroller), diag: diagOf(t.el),
          scanMode: 'absolute_qpid', scanStep: i, scanTotal: positions.length
        };
      }
    }
  } else {
    for (let i = 0; i < maxScroll && Date.now() < deadline; i++) {
      const t = findTarget();
      if (t && t.el) {
        try { t.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
        if (click) clickEl(t.el);
        return { ok:true, found:true, clicked: !!click, matchIndex: Number(t.idx), totalNodes: Number(t.total), scroll: scrollMeta(cachedScroller), diag: diagOf(t.el) };
      }
      scrollStep(cachedScroller, true);
      await sleep(70);
    }
    for (let i = 0; i < maxScroll && Date.now() < deadline; i++) {
      const t = findTarget();
      if (t && t.el) {
        try { t.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
        if (click) clickEl(t.el);
        return { ok:true, found:true, clicked: !!click, matchIndex: Number(t.idx), totalNodes: Number(t.total), scroll: scrollMeta(cachedScroller), diag: diagOf(t.el) };
      }
      scrollStep(cachedScroller, false);
      await sleep(70);
    }
  }
  for (let i = 0; i < Math.max(3, Math.floor(maxScroll / 4)) && Date.now() < deadline; i++) {
    const t = findTarget();
    if (t && t.el) {
      try { t.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
      if (click) clickEl(t.el);
      return { ok:true, found:true, clicked: !!click, matchIndex: Number(t.idx), totalNodes: Number(t.total), scroll: scrollMeta(cachedScroller), diag: diagOf(t.el) };
    }
    scrollStep(cachedScroller, false);
    await sleep(70);
  }
  const timedOut = Date.now() >= deadline;
  const finalTarget = findTarget();
  if (finalTarget && finalTarget.el) {
    try { finalTarget.el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
    if (click) clickEl(finalTarget.el);
    return { ok:true, found:true, clicked: !!click, matchIndex: Number(finalTarget.idx), totalNodes: Number(finalTarget.total), scroll: scrollMeta(cachedScroller), diag: diagOf(finalTarget.el), reason: timedOut ? 'deadline_late_match' : undefined };
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

_AUTO_RECOVER_IDLE_JS = r"""
() => {
  const dialogNeedles = [
    '非アクティブ', '操作がありません', '接続が失われ', '再接続',
    '他の場所でセッション', 'inactivity', 'are you still there',
    'session elsewhere', 'reconnecting', 'connection lost',
    'カスタマーサポート', 'カスタマーサポートに連絡してください',
    'お問い合わせください', 'customer support',
    '連絡してください', 'サポートに連絡', 'please contact', 'contact support', 'technical issue',
    'something went wrong', 'try again later', 'エラーが発生',
    'セッションが終了', 'session has ended', 'session expired'
  ];
  const buttonNeedles = [
    'continue', 'stay', 'close', 'remain', 'dismiss', 'ok', 'okay',
    'got it', 'understand', 'retry', 'reload', 'refresh', 'confirm',
    '続行', '閉じる', 'ここに残る', '再開', '戻る', '再試行', '更新',
    'はい', '確認', '了解', 'オーケー'
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
        const isOkLike = t === 'ok' || t === 'okay' || t === '確認' || t === '了解';
        if (t && (isOkLike || buttonNeedles.some((k) => t.includes(k))) && click(el)) clicked += 1;
        if (clicked >= 3) break;
      } catch(_) {}
    }
    if (!clicked) {
      for (const el of candidates) {
        try {
          if (!visible(el)) continue;
          const t = norm((el.innerText || el.textContent || el.getAttribute('aria-label') || '').slice(0, 160));
          if (t.includes('support') || t.includes('サポート') || t.includes('customer')) continue;
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
        self._last_session_ended_check_at: float = 0.0
        self._last_session_elsewhere_check_at: float = 0.0
        self._last_reconnecting_check_at: float = 0.0
        self._last_idle_recover_check_at: float = 0.0
        self._last_betslip_check_at: float = 0.0
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
        self._last_bet_modal_recover_at: float = 0.0
        self._table_states: dict[str, dict[str, Any]] = {}
        self._game_to_table_id: dict[str, str] = {}
        self._saw_per_table_betsopen: bool = False
        self._last_multi_area_ensure_at: float = 0.0
        self._multi_area_ready: bool = False
        self._last_multi_dom_recover_at: float = 0.0
        self._last_multi_fallback_join_at: float = 0.0
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

    def _max_bet_signal_age_sec(self) -> float:
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
            lobby_url = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"
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
                    "BACOPY_MULTI_PREPOSITION_SCROLL_MAX" if intent == "preposition" else "BACOPY_MULTI_SCROLL_MAX",
                    "48" if intent == "preposition" else "18",
                )
                or ("48" if intent == "preposition" else "18")
            ),
            "candidates": candidates,
            "hintIndex": -1,
            "hintTotal": 0,
            "hintScrollTop": -1,
            "hintScrollRatio": -1,
            "maxMs": int(
                os.getenv(
                    "BACOPY_MULTI_PREPOSITION_FOCUS_MS" if intent == "preposition" else "BACOPY_MULTI_FOCUS_MS",
                    "9000" if intent == "preposition" else "12000",
                )
                or ("9000" if intent == "preposition" else "12000")
            ),
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
                if click and res.get("clicked") and tid:
                    try:
                        page.wait_for_timeout(750)
                    except Exception:
                        pass
                    self._prepared_table_id = tid
                    self._prepared_at = time.time()
                    self._refresh_multi_tile_snapshot(force=True)
                    logger.info(f"[ML-PREPARED] active target ready for BET table={tid}")
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
        _raw = repr(data[:120]) if data else "(empty)"
        logger.info(f"[WS-SENT-RAW] tid={ws_table_id} len={len(data) if data else 0} raw={_raw}")
        if not data:
            return
        # <lpbet> 検出: Pragmatic game client がクリック受理後に自動送信する BET確定メッセージ
        if "<lpbet" in data:
            m_gid = re.search(r'gId="([^"]+)"', data)
            gid = str(m_gid.group(1) if m_gid else "").strip()
            self._last_lpbet_gid = gid
            self._last_lpbet_at = time.time()
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
                self._perform_switch(req)
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
                probe_frame = self._find_pragmatic_frame(target_qpid=prepared_tid)
                if probe_frame is not None:
                    side_class = {"P": "ym_yP", "B": "ym_yQ"}[self._diag_prepared_side]
                    self._hover_multi_tile(probe_frame, prepared_tid)
                    try:
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

        max_signal_age = self._max_bet_signal_age_sec()
        if pending_snapshot and pending_age > max_signal_age:
            logger.warning(
                f"[LIVE] drop stale signal pending bet "
                f"(age={pending_age:.1f}s > {max_signal_age:.1f}s, "
                f"table={pending_snapshot.get('table_id')})"
            )
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
                self._ensure_multi_area()
            except Exception:
                pass
            try:
                self._maintain_visible_bet_hold(now)
            except Exception as ex:
                logger.debug(f"[VISIBLE-HOLD] maintain failed: {ex}")

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
            target = str(qpid or table_id)
            if not ok and intent == "preposition" and target:
                cooldown = float(os.getenv("BACOPY_PREPOSITION_FOCUS_FAIL_COOLDOWN_SEC", "10") or 10)
                self._preposition_focus_block_until[target] = time.time() + max(5.0, cooldown)
                logger.info(
                    f"[SWITCH] preposition focus cooldown target={target} "
                    f"sec={max(5.0, cooldown):.1f}"
                )
            elif ok and target:
                self._preposition_focus_block_until.pop(target, None)
            if ok and self._switch_target_table_id:
                self._switch_target_table_id = ""
            if ok and target and self._prepared_table_id == target:
                with self._lock:
                    pending_target = str((self._pending_bet or {}).get("table_id") or "")
                    pending_amount = float((self._pending_bet or {}).get("amount") or 0.0)
                preselect_amount = float(req.get("preselect_amount") or req.get("amount") or 0.0)
                if pending_target == target and self._is_table_bet_window_open(target):
                    st = self._table_states.get(target) or {}
                    gid = str(st.get("bets_open_game_id") or "")
                    if gid:
                        logger.info(f"[SWITCH] prepared target has open BET window; resume pending BET table={target}")
                        self._try_execute_bet(gid, table_id=target)
                elif pending_target == target and pending_amount > 0:
                    self._preselect_first_chip(target, pending_amount)
                elif intent in ("preposition", "manual_assist") and preselect_amount > 0:
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
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"[LIVE] idle recover frame list error ({reason}): {e}")
        return total

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

        age = self._bet_signal_age(bet)
        max_age = self._max_bet_signal_age_sec()
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
        if self._multi_lobby_mode and self._multi_bet_transport == "click":
            prepared = str(self._prepared_table_id or "")
            active_window = self._is_table_bet_window_open(bet_table_id)
            antenna_ok = self.is_table_in_antenna_zone(bet_table_id, str(side or ""))
            if prepared != bet_table_id and not antenna_ok:
                age_before_focus = self._bet_signal_age(bet)
                max_age_before_focus = self._max_bet_signal_age_sec()
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
                    now_wait = time.time()
                    if now_wait - self._last_bet_modal_recover_at >= 0.4:
                        self._last_bet_modal_recover_at = now_wait
                        self._auto_recover_idle_dialogs("lpbet-wait")
                    time.sleep(0.1)
                require_trusted = os.getenv("BACOPY_DUAL_REQUIRE_TRUSTED_CONFIRM", "1").strip() != "0"
                if (not trusted_confirm) and lpbet_confirmed and not require_trusted:
                    trusted_confirm = {
                        "confirm_type": "lpbet_only_untrusted",
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
    return {ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
            wasActive: wasActive, source: src, btnW: r.width, btnH: r.height};
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
    // prefer the large active wager-panel button. Compact tile buttons can
    // select/focus a card without placing a real wager.
    const candidates = Array.from(document.querySelectorAll('.' + sideClass + '.' + activeClass))
      .filter((btn) => {
        const r = btn.getBoundingClientRect();
        if (!(rectOk(r) && r.x >= 0 && r.y >= 0)) return false;
        const tileOfBtn = enclosingTile(btn);
        return !tileOfBtn || tileOfBtn === tile;
      })
      .map((btn) => {
        const r = btn.getBoundingClientRect();
        return {btn, area: r.width * r.height, x: r.left, y: r.top};
      })
      .sort((a, b) => b.area - a.area);
    if (candidates.length < 1) return null;
    return btnCoords(candidates[0].btn, true, 'active_panel_after_qpid_tile');
  }
  function enclosingTile(el) {
    let node = el;
    for (let depth = 0; depth < 12 && node; depth++, node = node.parentElement) {
      if (String(node.id || '').startsWith('TileHeight-')) return node;
    }
    return null;
  }

  let targetTile = null;
  // 1) qpid と一致するタイル内部だけを対象にする。親グリッドへは広げない。
  if (qpid) {
    targetTile = document.getElementById('TileHeight-' + qpid);
    if (targetTile) {
      try { targetTile.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
      const activePanelResult = tryActivePanelAfterTargetTile(targetTile);
      if (activePanelResult) return activePanelResult;
      const orderedResult = tryOrderedTileButtons(targetTile, 'qpid_tile_ordered');
      if (orderedResult) return orderedResult;
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
        const activePanelResult = tryActivePanelAfterTargetTile(tile);
        if (activePanelResult) return activePanelResult;
        const orderedResult = tryOrderedTileButtons(tile, 'table_name_tile_ordered');
        if (orderedResult) return orderedResult;
        const result = tryWithinTile(tile, 'table_name_tile');
        if (result) return result;
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
  const targetDiag = targetTile ? Array.from(targetTile.querySelectorAll('[class]'))
    .filter((el) => String(el.className || '').includes('ym_'))
    .slice(0, 18)
    .map((el) => {
      const r = el.getBoundingClientRect();
      return {
        cls: String(el.className || '').slice(0, 80),
        text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height),
      };
    }) : [];

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
            frame.locator(f'[id="TileHeight-{tid}"]').hover(timeout=2000, force=True)
            frame.page.wait_for_timeout(250)
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

    def _center_multi_tile(self, qpid: str, table_name: str = "", *, click: bool = False) -> bool:
        """Keep a multi-play tile visible near the screen center."""
        tid = str(qpid or "").strip()
        if not tid:
            return False
        frame = self._find_pragmatic_frame(target_qpid=tid)
        if not frame:
            logger.info(f"[VISIBLE-HOLD] frame not found for center target={tid!r}")
            return False
        try:
            res = frame.evaluate(
                _MULTI_LOBBY_FOCUS_JS,
                {
                    "qpid": tid,
                    "click": bool(click),
                    "maxScroll": int(os.getenv("BACOPY_VISIBLE_HOLD_SCROLL_MAX", "18") or "18"),
                    "candidates": [str(table_name or ""), tid],
                    "hintIndex": -1,
                    "hintTotal": 0,
                },
            )
            ok = bool(isinstance(res, dict) and res.get("found"))
            logger.info(f"[VISIBLE-HOLD] center target={tid!r} ok={ok} result={res}")
            return ok
        except Exception as ex:
            logger.warning(f"[VISIBLE-HOLD] center failed target={tid!r}: {ex}")
            return False

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
        self._center_multi_tile(tid, table_name, click=False)

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
        self._center_multi_tile(tid, str(hold.get("table_name") or tid), click=False)

    def mark_bet_resolved(self, bet_id: str = "", table_id: str = "") -> None:
        """Called by the live bot when the watched bet has an outcome."""
        hold = self._visible_bet_hold or {}
        if not hold:
            return
        bid = str(bet_id or "").strip()
        tid = str(table_id or "").strip()
        hold_bid = str(hold.get("bet_id") or "")
        hold_tid = str(hold.get("table_id") or "")
        if (bid and hold_bid and bid != hold_bid) or (tid and hold_tid and tid != hold_tid):
            return
        logger.info(f"[VISIBLE-HOLD] resolved; release table={hold_tid} bet_id={hold_bid[:16]}")
        self._center_multi_tile(hold_tid, str(hold.get("table_name") or hold_tid), click=False)
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
            res = frame.evaluate(
                self._JS_BET_COORDS,
                {
                    "qpid": qpid,
                    "tableName": table_name,
                    "sideClass": side_class,
                    "sideCode": side,
                    "expectedGameId": str(expected_game_id or ""),
                    "staleTileGameIdOk": str(stale_tile_game_id_ok or ""),
                },
            )
            logger.info(f"[CLICK-BET-JS] qpid={qpid!r} table={table_name!r} coords={res}")
            if not (isinstance(res, dict) and res.get("ok")):
                self._last_click_bet_error = res if isinstance(res, dict) else {"reason": "invalid_result", "result": repr(res)}
                return False

            btn_x = float(res.get("x", 0))
            btn_y = float(res.get("y", 0))

            page_x, page_y = btn_x, btn_y
            try:
                frame_el = frame.frame_element()
                bbox = frame_el.bounding_box()
                if bbox:
                    page_x = bbox["x"] + btn_x
                    page_y = bbox["y"] + btn_y
            except Exception as _fe:
                logger.debug(f"[CLICK-BET-JS] frame_element offset failed: {_fe}")

            page = frame.page
            page.mouse.move(page_x, page_y)
            page.wait_for_timeout(80)
            page.mouse.click(page_x, page_y)
            self._last_click_bet_page_coords = {
                "x": page_x,
                "y": page_y,
                "qpid": str(qpid or ""),
                "side": str(side or ""),
                "expected_game_id": str(expected_game_id or ""),
                "at": time.time(),
            }
            logger.info(f"[CLICK-BET-JS] mouse.click at ({page_x:.1f}, {page_y:.1f}) wasActive={res.get('wasActive')}")
            return True
        except Exception as ex:
            self._last_click_bet_error = {"reason": "exception", "error": str(ex)}
            logger.warning(f"[CLICK-BET-JS] error: {ex}")
            return False

    _JS_SELECTOR_COORDS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return {ok:false, reason:'not_found', selector};
  try { el.scrollIntoView({block:'center', inline:'center'}); } catch(_) {}
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  if (!r || r.width <= 4 || r.height <= 4 || cs.visibility === 'hidden' || cs.display === 'none') {
    return {ok:false, reason:'not_visible', selector,
            x: r ? r.left : 0, y: r ? r.top : 0,
            w: r ? r.width : 0, h: r ? r.height : 0};
  }
  return {ok:true, selector, x:r.left + r.width / 2, y:r.top + r.height / 2,
          w:r.width, h:r.height, text:String(el.innerText || el.textContent || '').trim().slice(0,80)};
}
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
            page.mouse.move(page_x, page_y)
            page.wait_for_timeout(60)
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
            vals = frame.evaluate(
                r"""
() => Array.from(document.querySelectorAll('[data-testid^="chip-stack-value-"]'))
  .map(el => String(el.getAttribute('data-testid') || '').replace('chip-stack-value-', ''))
  .map(v => Number(v))
  .filter(v => Number.isFinite(v) && v > 0)
"""
            )
            if isinstance(vals, list):
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
        if self._js_click_selector(frame, chip_sel, "CHIP"):
            logger.info(f"[{log_prefix}] chip selected via JS mouse: {chip_str} (selector={chip_sel})")
            try:
                frame.page.wait_for_timeout(80)
            except Exception:
                pass
            return True
        try:
            chip_timeout_ms = int(float(os.getenv("BACOPY_CHIP_SELECT_TIMEOUT_SEC", "0.8") or 0.8) * 1000)
            frame.click(chip_sel, timeout=max(250, chip_timeout_ms))
            logger.info(f"[{log_prefix}] chip selected: {chip_str} (selector={chip_sel})")
            return True
        except Exception as ce:
            logger.warning(f"[{log_prefix}] chip select failed ({chip_sel}): {ce}")
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
        chip_plan = self._chip_plan(amount, rendered_denoms)
        if not chip_plan:
            logger.info(f"[CHIP-PRESELECT] skip: empty chip plan amount=${amount:.2f} target={target_qpid!r}")
            return False
        first = float(chip_plan[0])
        if not self._select_chip_in_frame(frame, first, "CHIP-PRESELECT"):
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
                    retry_sec = float(os.getenv("BACOPY_MULTI_BUTTON_WAIT_SEC", "3.5") or 3.5)
                    deadline = time.time() + max(0.5, retry_sec)
                    attempt = 0
                    while time.time() < deadline:
                        attempt += 1
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
                        logger.info(f"[CLICK-BET-RESEEK] qpid={target_qpid!r} attempt={attempt} result={reseek}")
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
                                    f"qpid={target_qpid!r} attempt={attempt} click={click_index}/{len(chip_plan)}"
                                )
                                return True
                        frame.page.wait_for_timeout(250)
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
                frame.page.mouse.click(x, y)
                logger.info(
                    f"[CLICK-BET] cached direct click OK: side={side} qpid={target_qpid!r} "
                    f"click={click_index}/{len(chip_plan)} at=({x:.1f},{y:.1f})"
                )
                return True
            except Exception as ex:
                logger.debug(f"[CLICK-BET] cached direct click failed: {ex}")
                return False

        for idx, chip_value in enumerate(chip_plan, start=1):
            pre = self._preselected_chip or {}
            chip_changed_this_click = False
            can_skip_preselect = (
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

        logger.info(f"[CLICK-BET] all planned chip clicks completed: side={side} amount=${amount:.2f} clicks={len(chip_plan)}")
        return True

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
        proxy_server = (
            self._ws_proxy_servers.get(table_id) or self._ws_proxy_server
            if self._multi_lobby_mode else None
        )
        if proxy_server is not None:
            try:
                proxy_server.send(payload)
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
                    "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat",
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
        if (
            self._multi_lobby_mode
            and new_intent == "preposition"
            and hold_target
            and now < hold_until
            and new_target != hold_target
        ):
            logger.info(
                f"[VISIBLE-HOLD] drop preposition while showing bet/result: "
                f"keep={hold_target} drop={new_target or '-'}"
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
