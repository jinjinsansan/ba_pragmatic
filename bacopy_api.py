from __future__ import annotations

"""bacopy Master API (dependency-free).

This repo previously used FastAPI, but the current execution environment may not
have external packages installed. For the copytrade MVP we use only the Python
standard library so it runs anywhere.

Endpoints:
  GET  /api/health
  GET  /api/status                 (auth)
  POST /api/decisions              (auth)
  GET  /api/decisions/pending      (auth)
  POST /api/decisions/{id}/ack     (auth)
  POST /api/decisions/{id}/result  (auth)
"""

import argparse
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from bacopy_db import (
    get_by_status,
    get_pending,
    get_stats,
    init_db,
    insert_decision,
    list_executors,
    mark_ack,
    mark_result,
    upsert_executor,
)

from decision_logger import append_decision_event
from snapshot_store import get_snapshot, load_snapshots


def _resolve_table_id_from_snapshots(provider: str, table_name: str) -> str:
    if not provider or not table_name:
        return ""
    data = load_snapshots()
    snaps = (data.get("snapshots") or {}).get(provider) or {}
    if not isinstance(snaps, dict):
        return ""
    tn = str(table_name).strip().lower()
    # exact match
    for tid, s in snaps.items():
        name = (s or {}).get("table_name") or ""
        if str(name).strip().lower() == tn:
            return str(tid)
    # contains match
    for tid, s in snaps.items():
        name = (s or {}).get("table_name") or ""
        if tn and tn in str(name).strip().lower():
            return str(tid)
    return ""


def _fill_snapshot(provider: str, table_id: str, payload: dict[str, Any]) -> None:
    snap = payload.get("snapshot")
    if isinstance(snap, dict) and snap:
        return
    if not provider or not table_id:
        return
    s = get_snapshot(provider, table_id)
    if isinstance(s, dict) and s:
        payload["snapshot"] = s


def _expected_api_key() -> str:
    expected = os.getenv("BACOPY_API_KEY", "").strip()
    if expected:
        return expected
    expected = secrets.token_hex(16)
    os.environ["BACOPY_API_KEY"] = expected
    return expected


_SESS_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}  # token -> {csrf, exp}


def _master_password() -> str:
    pw = os.getenv("BACOPY_MASTER_PASSWORD", "").strip()
    if pw:
        return pw
    pw = secrets.token_urlsafe(18)
    os.environ["BACOPY_MASTER_PASSWORD"] = pw
    return pw


def _cookie_secure_flag() -> bool:
    return os.getenv("BACOPY_COOKIE_SECURE", "").strip() in ("1", "true", "yes", "on")


def _parse_cookies(headers) -> dict[str, str]:
    raw = headers.get("Cookie") or headers.get("cookie") or ""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        p = part.strip()
        if not p or "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _get_session(headers) -> Optional[dict[str, Any]]:
    tok = _parse_cookies(headers).get("bacopy_session") or ""
    if not tok:
        return None
    now = time.time()
    with _SESS_LOCK:
        s = _SESSIONS.get(tok)
        if not isinstance(s, dict):
            return None
        if float(s.get("exp") or 0) < now:
            _SESSIONS.pop(tok, None)
            return None
        return s


def _bearer_ok(headers) -> bool:
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    token = auth.split(" ", 1)[1].strip()
    return token == _expected_api_key()


def _auth_ok(headers, *, require_csrf: bool = False) -> bool:
    if _bearer_ok(headers):
        return True
    s = _get_session(headers)
    if not s:
        return False
    if not require_csrf:
        return True
    csrf = headers.get("X-CSRF-Token") or headers.get("x-csrf-token") or ""
    return bool(csrf) and csrf == str(s.get("csrf") or "")


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        ln = int(handler.headers.get("Content-Length") or "0")
    except Exception:
        ln = 0
    raw = handler.rfile.read(ln) if ln > 0 else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_html(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _redirect(handler: BaseHTTPRequestHandler, location: str, *, set_cookie: str = "") -> None:
    handler.send_response(302)
    if set_cookie:
        handler.send_header("Set-Cookie", set_cookie)
    handler.send_header("Location", location)
    handler.end_headers()


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    try:
        ln = int(handler.headers.get("Content-Length") or "0")
    except Exception:
        ln = 0
    raw = handler.rfile.read(ln) if ln > 0 else b""
    if not raw:
        return {}
    try:
        # parse_qs expects str
        qs = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in qs.items()}
    except Exception:
        return {}


def _master_login_page(*, error: str = "") -> str:
    err = f"<div class='err'>{error}</div>" if error else ""
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>bacopy master login</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Yu Gothic UI',sans-serif;background:#0f1419;color:#e0e6ed;margin:0;padding:24px}}
.card{{max-width:420px;margin:64px auto;background:#141c26;border:1px solid #243040;border-radius:10px;padding:18px}}
label{{display:block;margin:8px 0 6px;color:#9fb0c5;font-size:13px}}
input{{width:100%;padding:12px;border-radius:8px;border:1px solid #2a3441;background:#0f1419;color:#e0e6ed}}
button{{width:100%;margin-top:14px;padding:12px;border-radius:8px;border:0;background:#3b82f6;color:#fff;font-weight:700}}
.err{{margin:10px 0;color:#fca5a5}}
</style></head>
<body><div class="card">
<h2 style="margin:0 0 10px 0">bacopy Master</h2>
{err}
<form method="POST" action="/master/login">
  <label>パスワード</label>
  <input name="password" type="password" autofocus/>
  <button type="submit">ログイン</button>
</form>
<p style="color:#9fb0c5;font-size:12px;line-height:1.5;margin:12px 0 0 0;">
※ VPS公開の場合は HTTPS(リバプロ) を推奨します（平文HTTPだとパスワードが漏れます）。</p>
</div></body></html>"""


def _master_app_page(csrf: str) -> str:
    # Single-file UI: HTML + CSS + JS
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="csrf" content="{csrf}"/>
<title>bacopy master</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Yu Gothic UI',sans-serif;background:#0f1419;color:#e0e6ed;margin:0}}
header{{position:sticky;top:0;background:#0f1419;border-bottom:1px solid #223044;padding:12px 14px;z-index:5}}
main{{padding:14px}}
.row{{display:flex;gap:10px;flex-wrap:wrap}}
.card{{background:#141c26;border:1px solid #243040;border-radius:10px;padding:12px}}
.k{{color:#9fb0c5;font-size:12px}}
.v{{font-size:16px;font-weight:700}}
select,input,button{{font-size:14px}}
input,select{{padding:10px;border-radius:8px;border:1px solid #2a3441;background:#0f1419;color:#e0e6ed}}
button{{padding:10px 12px;border-radius:10px;border:0;background:#1f2937;color:#e0e6ed;font-weight:700}}
button.primary{{background:#3b82f6}}
button.warn{{background:#f59e0b;color:#111827}}
button.danger{{background:#ef4444}}
button:disabled{{opacity:.45}}
.pill{{display:inline-block;padding:3px 8px;border-radius:999px;background:#1f2937;color:#9fb0c5;font-size:12px}}
.list{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}}
.tablebtn{{text-align:left;width:100%;background:#0f1419;border:1px solid #243040}}
.tablebtn.active{{border-color:#3b82f6}}
.small{{font-size:12px;color:#9fb0c5}}
.err{{color:#fca5a5;white-space:pre-wrap}}
.ok{{color:#86efac}}
</style></head>
<body>
<header>
  <div class="row" style="align-items:center;justify-content:space-between;">
    <div style="display:flex;gap:10px;align-items:center;">
      <strong>bacopy Master</strong>
      <span id="statusPill" class="pill">loading...</span>
    </div>
    <form method="POST" action="/master/logout"><button type="submit">ログアウト</button></form>
  </div>
</header>
<main>
  <div class="row">
    <div class="card" style="flex:1;min-width:260px">
      <div class="k">Snapshots updated_at</div><div id="snapUpdatedAt" class="v">-</div>
      <div class="k" style="margin-top:10px">Decisions (db counts)</div><div id="dbCounts" class="small">-</div>
      <div class="k" style="margin-top:10px">Winrate (BET only)</div><div id="winrate" class="v">-</div>
    </div>
    <div class="card" style="flex:2;min-width:320px">
      <div class="row" style="align-items:end">
        <div>
          <div class="k">Provider</div>
          <select id="providerSel"><option value="pragmatic">pragmatic</option><option value="evolution">evolution</option></select>
        </div>
        <div style="flex:1;min-width:160px">
          <div class="k">Search</div>
          <input id="searchBox" placeholder="table name contains..." />
        </div>
        <div style="min-width:220px">
          <div class="k">Target executor</div>
          <select id="execSel"><option value="">(broadcast)</option></select>
        </div>
      </div>
      <div class="row" style="margin-top:10px;align-items:end">
        <div style="flex:1;min-width:220px">
          <div class="k">Selected table</div>
          <div id="selectedTable" class="v">-</div>
          <div id="selectedMeta" class="small"></div>
        </div>
        <div>
          <div class="k">Amount</div>
          <input id="amountBox" type="number" step="1" min="0" value="1" style="width:110px"/>
        </div>
        <div style="flex:1;min-width:180px">
          <div class="k">Note</div>
          <input id="noteBox" placeholder="optional note"/>
        </div>
      </div>
      <div class="row" style="margin-top:10px">
        <button id="btnSwitch" class="warn">テーブル切替</button>
        <button id="btnLook">LOOK</button>
        <button id="btnP" class="primary">PLAYER</button>
        <button id="btnB" class="primary">BANKER</button>
        <button id="btnT" class="primary">TIE</button>
      </div>
      <div class="small" style="margin-top:8px">
        Pragmaticの実BETは現状PLAYERのみ対応（BANKER/TIEは追加スニフ後に解放）。
      </div>
      <div id="sendMsg" class="small" style="margin-top:6px"></div>
    </div>
  </div>

  <h3>Executors (GUI接続)</h3>
  <div id="execList" class="list"></div>

  <h3>Tables</h3>
  <div id="tableList" class="list"></div>

  <h3>History</h3>
  <div class="row">
    <div class="card" style="flex:1;min-width:320px">
      <div class="k">pending / processing</div>
      <div id="histPending" class="small">-</div>
    </div>
    <div class="card" style="flex:1;min-width:320px">
      <div class="k">done / error (latest)</div>
      <div id="histDone" class="small">-</div>
    </div>
  </div>
</main>
<script>
const csrf = document.querySelector('meta[name=\"csrf\"]').content;
let selected = {{provider:'pragmatic', table_id:'', table_name:''}};

function fmt(o){{ try{{return JSON.stringify(o)}}catch(e){{return String(o)}} }}
function escapeHtml(s){{ return String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); }}

async function apiGet(path) {{
  const r = await fetch(path, {{credentials:'same-origin'}});
  const t = await r.text();
  try {{ return JSON.parse(t); }} catch(e) {{ return {{ok:false, error:'non_json', raw:t, status:r.status}}; }}
}}
async function apiPost(path, body) {{
  const r = await fetch(path, {{
    method:'POST',
    credentials:'same-origin',
    headers: {{'Content-Type':'application/json', 'X-CSRF-Token': csrf}},
    body: JSON.stringify(body || {{}})
  }});
  const t = await r.text();
  try {{ return JSON.parse(t); }} catch(e) {{ return {{ok:false, error:'non_json', raw:t, status:r.status}}; }}
}}

function decisionId() {{
  const a = crypto.getRandomValues(new Uint8Array(8));
  return 'dec_' + Array.from(a).map(x=>x.toString(16).padStart(2,'0')).join('');
}}

function setMsg(msg, isErr=false) {{
  const el = document.getElementById('sendMsg');
  el.className = isErr ? 'err' : 'ok';
  el.textContent = msg;
}}

async function sendDecision(action, side) {{
  if(!selected.table_id) {{ setMsg('テーブルを選択してください', true); return; }}
  const provider = document.getElementById('providerSel').value;
  const target_executor_id = document.getElementById('execSel').value || '';
  const amount = Number(document.getElementById('amountBox').value || '0');
  const note = document.getElementById('noteBox').value || '';
  const payload = {{
    decision_id: decisionId(),
    provider,
    table_id: selected.table_id,
    table_name: selected.table_name,
    target_executor_id,
    friend_action: {{action, side: side || '', amount: amount || 0, note}},
  }};
  const res = await apiPost('/api/decisions', payload);
  if(res && res.accepted) setMsg('sent: ' + res.decision_id);
  else setMsg('send failed: ' + fmt(res), true);
}}

function renderExecList(executors) {{
  const now = Date.now();
  const wrap = document.getElementById('execList');
  wrap.innerHTML = '';
  for(const e of (executors||[])) {{
    const ageSec = e.updated_at ? Math.max(0, (now - Date.parse(e.updated_at))/1000) : 99999;
    const online = ageSec < 30;
    const c = document.createElement('div');
    c.className = 'card';
    const bal = (e.balance===null || e.balance===undefined) ? '-' : String(e.balance);
    const err = e.error ? '<div class=\"err\">'+escapeHtml(e.error)+'</div>' : '';
    c.innerHTML = `
      <div style=\"display:flex;justify-content:space-between;gap:8px;align-items:center\">
        <div><strong>${{escapeHtml(e.label||e.executor_id)}}</strong> <span class=\"pill\">${{online?'ONLINE':'OFFLINE'}}</span></div>
        <div class=\"small\">${{escapeHtml(e.updated_at||'')}}</div>
      </div>
      <div class=\"small\">user=${{escapeHtml(e.username||'')}} provider=${{escapeHtml(e.provider||'')}} table=${{escapeHtml(e.table_name||e.table_id||'')}}</div>
      <div class=\"small\">balance=${{escapeHtml(bal)}} seq=${{escapeHtml(fmt(e.seq||{{}}))}} status=${{escapeHtml(e.status||'')}}</div>
      ${{err}}
    `;
    wrap.appendChild(c);
  }}
}}

function renderExecutorSelect(executors) {{
  const sel = document.getElementById('execSel');
  const cur = sel.value;
  sel.innerHTML = '<option value=\"\">(broadcast)</option>';
  for(const e of (executors||[])) {{
    const opt = document.createElement('option');
    opt.value = e.executor_id;
    opt.textContent = (e.label||e.executor_id) + (e.username?(' ['+e.username+']'):'');
    sel.appendChild(opt);
  }}
  sel.value = cur;
}}

function renderTables(provider, snapshots) {{
  const search = (document.getElementById('searchBox').value||'').toLowerCase().trim();
  const list = (snapshots && snapshots.snapshots && snapshots.snapshots[provider]) ? snapshots.snapshots[provider] : {{}};
  const items = Object.entries(list).map(([tid, s]) => ({{tid, s}}));
  items.sort((a,b)=> String(a.s.table_name||'').localeCompare(String(b.s.table_name||'')));
  const wrap = document.getElementById('tableList');
  wrap.innerHTML = '';
  for(const it of items) {{
    const name = String((it.s||{{}}).table_name||'');
    if(search && !name.toLowerCase().includes(search)) continue;
    const btn = document.createElement('button');
    btn.className = 'card tablebtn' + (selected.table_id===String(it.tid) ? ' active':'' );
    const last10 = (it.s && it.s.last_10) ? it.s.last_10.join('') : '';
    const players = (it.s && it.s.players!==undefined) ? (' players='+it.s.players) : '';
    const hands = (it.s && it.s.hands!==undefined) ? (' hands='+it.s.hands) : '';
    btn.innerHTML = `<div style=\"display:flex;justify-content:space-between;gap:8px\"><div><strong>${{escapeHtml(name||it.tid)}}</strong></div><div class=\"small\">id=${{escapeHtml(it.tid)}}</div></div>
      <div class=\"small\">${{escapeHtml(last10)}}${{escapeHtml(players)}}${{escapeHtml(hands)}}</div>`;
    btn.onclick = () => {{
      selected = {{provider, table_id:String(it.tid), table_name:name}};
      document.getElementById('selectedTable').textContent = name || it.tid;
      document.getElementById('selectedMeta').textContent = 'provider='+provider+' table_id='+it.tid;
      refreshOnce();
    }};
    wrap.appendChild(btn);
  }}
}}

function renderHistory(pending, processing, done, error) {{
  const p = [...(pending||[]), ...(processing||[])].slice(-20);
  const d = [...(error||[]), ...(done||[])].slice(-20).reverse();
  const fmtRow = (x) => {{
    const fa = (x.friend_action||{{}});
    const r = (x.result||{{}});
    const out = r.outcome || r.result || r.error || '';
    const side = fa.side||'';
    return `${{escapeHtml(x.status||'')}} ${{escapeHtml(x.provider||'')}} ${{escapeHtml(x.table_name||x.table_id||'')}}  ${{escapeHtml(fa.action||'')}} ${{escapeHtml(side)}} -> ${{escapeHtml(out)}}  (${{escapeHtml(x.decision_id||'')}})`;
  }};
  document.getElementById('histPending').innerHTML = '<pre style=\"margin:0\">'+ p.map(fmtRow).join('\\n') +'</pre>';
  document.getElementById('histDone').innerHTML = '<pre style=\"margin:0\">'+ d.map(fmtRow).join('\\n') +'</pre>';
}}

function computeWinrate(done) {{
  let bet=0, win=0, lose=0, tie=0;
  for(const x of (done||[])) {{
    const fa = x.friend_action||{{}};
    if(String(fa.action||'').toUpperCase()!=='BET') continue;
    const side = String(fa.side||'').toLowerCase();
    const out = String(((x.result||{{}}).outcome)||'').toLowerCase();
    bet += 1;
    if(out==='tie') tie += 1;
    if(out===side) win += 1;
    else lose += 1;
  }}
  const wr = bet ? (win/bet*100).toFixed(2) : '0.00';
  return {{bet, win, lose, tie, wr}};
}}

async function refreshOnce() {{
  const provider = document.getElementById('providerSel').value;
  selected.provider = provider;
  const st = await apiGet('/api/status');
  const snaps = await apiGet('/api/snapshots');
  const executors = await apiGet('/api/executors');
  const pending = await apiGet('/api/decisions?status=pending&limit=50');
  const processing = await apiGet('/api/decisions?status=processing&limit=50');
  const done = await apiGet('/api/decisions?status=done&limit=1000');
  const error = await apiGet('/api/decisions?status=error&limit=200');

  document.getElementById('statusPill').textContent = (st && st.ok) ? 'OK' : 'ERR';
  document.getElementById('snapUpdatedAt').textContent = (st && st.snapshots_updated_at) ? st.snapshots_updated_at : '-';
  document.getElementById('dbCounts').textContent = st && st.db && st.db.counts ? fmt(st.db.counts) : '-';

  const wr = computeWinrate((done && done.decisions) ? done.decisions : []);
  document.getElementById('winrate').textContent = `${{wr.wr}}% (win=${{wr.win}} / bet=${{wr.bet}} / tie=${{wr.tie}})`;

  const execArr = executors && executors.executors ? executors.executors : [];
  renderExecList(execArr);
  renderExecutorSelect(execArr);

  renderTables(provider, snaps);
  renderHistory(
    (pending && pending.decisions) ? pending.decisions : [],
    (processing && processing.decisions) ? processing.decisions : [],
    (done && done.decisions) ? done.decisions : [],
    (error && error.decisions) ? error.decisions : [],
  );

  // Disable banker/tie buttons for pragmatic until enabled.
  const isPrag = provider === 'pragmatic';
  document.getElementById('btnB').disabled = isPrag;
  document.getElementById('btnT').disabled = isPrag;
}}

document.getElementById('providerSel').onchange = refreshOnce;
document.getElementById('searchBox').oninput = () => {{ window.clearTimeout(window.__t); window.__t=setTimeout(refreshOnce, 200); }};
document.getElementById('btnSwitch').onclick = () => sendDecision('SWITCH_TABLE', '');
document.getElementById('btnLook').onclick = () => sendDecision('LOOK', '');
document.getElementById('btnP').onclick = () => sendDecision('BET', 'PLAYER');
document.getElementById('btnB').onclick = () => sendDecision('BET', 'BANKER');
document.getElementById('btnT').onclick = () => sendDecision('BET', 'TIE');

refreshOnce();
setInterval(refreshOnce, 2500);
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            return _redirect(self, "/master")

        if u.path == "/master/login":
            return _send_html(self, 200, _master_login_page())
        if u.path == "/master":
            s = _get_session(self.headers)
            if not s:
                return _redirect(self, "/master/login")
            return _send_html(self, 200, _master_app_page(str(s.get("csrf") or "")))

        if u.path == "/api/health":
            return _send_json(self, 200, {"ok": True})
        if not _auth_ok(self.headers):
            return _send_json(self, 401, {"ok": False, "error": "unauthorized"})
        if u.path == "/api/status":
            snaps = load_snapshots()
            providers = list((snaps.get("snapshots") or {}).keys()) if isinstance(snaps, dict) else []
            return _send_json(
                self,
                200,
                {
                    "ok": True,
                    "db": get_stats(),
                    "snapshots_updated_at": (snaps.get("updated_at") if isinstance(snaps, dict) else None),
                    "snapshot_providers": providers,
                },
            )
        if u.path == "/api/executors":
            qs = parse_qs(u.query or "")
            try:
                limit = int((qs.get("limit") or ["200"])[0])
            except Exception:
                limit = 200
            return _send_json(self, 200, {"executors": list_executors(limit=limit)})
        if u.path == "/api/snapshots":
            qs = parse_qs(u.query or "")
            provider = (qs.get("provider") or [""])[0]
            table_id = (qs.get("table_id") or [""])[0]
            if provider and table_id:
                return _send_json(self, 200, {"snapshot": get_snapshot(provider, table_id)})
            return _send_json(self, 200, load_snapshots())
        if u.path == "/api/decisions/pending":
            qs = parse_qs(u.query or "")
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except Exception:
                limit = 50
            return _send_json(self, 200, {"pending": get_pending(limit=limit)})
        if u.path == "/api/decisions":
            qs = parse_qs(u.query or "")
            status = (qs.get("status") or ["pending"])[0]
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except Exception:
                limit = 50
            return _send_json(self, 200, {"decisions": get_by_status(status, limit=limit)})
        return _send_json(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/master/login":
            form = _read_form(self)
            pw = str(form.get("password") or "")
            if pw != _master_password():
                return _send_html(self, 401, _master_login_page(error="パスワードが違います"))
            tok = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(18)
            exp = time.time() + 60 * 60 * 12
            with _SESS_LOCK:
                _SESSIONS[tok] = {"csrf": csrf, "exp": exp}
            secure = "; Secure" if _cookie_secure_flag() else ""
            cookie = f"bacopy_session={tok}; Path=/; HttpOnly; SameSite=Strict{secure}"
            return _redirect(self, "/master", set_cookie=cookie)

        if u.path == "/master/logout":
            secure = "; Secure" if _cookie_secure_flag() else ""
            cookie = f"bacopy_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"
            return _redirect(self, "/master/login", set_cookie=cookie)

        if not _auth_ok(self.headers, require_csrf=True):
            return _send_json(self, 401, {"ok": False, "error": "unauthorized"})

        if u.path == "/api/decisions":
            payload = _read_json(self)
            decision_id = str(payload.get("decision_id") or "")
            provider = str(payload.get("provider") or "")
            if len(decision_id) < 8:
                return _send_json(self, 400, {"ok": False, "error": "decision_id required"})
            if provider not in ("evolution", "pragmatic"):
                return _send_json(self, 400, {"ok": False, "error": "provider must be evolution|pragmatic"})
            fa = payload.get("friend_action") or {}
            if not isinstance(fa, dict) or not fa.get("action"):
                return _send_json(self, 400, {"ok": False, "error": "friend_action.action required"})

            # Ensure decision-time metadata exists (no look-ahead; snapshot is taken "now" at API receive time).
            payload.setdefault("schema_version", 1)
            if not str(payload.get("captured_at") or ""):
                payload["captured_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            table_id = str(payload.get("table_id") or "")
            table_name = str(payload.get("table_name") or "")
            if not table_id and table_name:
                table_id = _resolve_table_id_from_snapshots(provider, table_name)
                if table_id:
                    payload["table_id"] = table_id
            _fill_snapshot(provider, table_id, payload)

            append_decision_event(payload)
            insert_decision(decision_id, payload)
            return _send_json(self, 200, {"accepted": True, "decision_id": decision_id})

        if u.path == "/api/executors/heartbeat":
            body = _read_json(self)
            executor_id = str(body.get("executor_id") or "")
            if len(executor_id) < 4:
                return _send_json(self, 400, {"ok": False, "error": "executor_id required"})
            upsert_executor(executor_id, body if isinstance(body, dict) else {})
            return _send_json(self, 200, {"ok": True})

        parts = [p for p in u.path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] in ("ack", "result"):
            decision_id = parts[2]
            body = _read_json(self)
            if parts[3] == "ack":
                ack = body.get("ack") if isinstance(body, dict) else {}
                if not isinstance(ack, dict):
                    ack = {}
                status = str(body.get("status") or "processing") if isinstance(body, dict) else "processing"
                mark_ack(decision_id, ack, status=status)
                return _send_json(self, 200, {"ok": True})
            if parts[3] == "result":
                result = body.get("result") if isinstance(body, dict) else {}
                if not isinstance(result, dict):
                    result = {}
                status = str(body.get("status") or "done") if isinstance(body, dict) else "done"
                mark_result(decision_id, result, status=status)
                return _send_json(self, 200, {"ok": True})

        return _send_json(self, 404, {"ok": False, "error": "not_found"})


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.getenv("BACOPY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("BACOPY_PORT", "8010")))
    args = ap.parse_args(argv)

    init_db()
    env_key = os.getenv("BACOPY_API_KEY", "").strip()
    key = _expected_api_key()
    env_pw = os.getenv("BACOPY_MASTER_PASSWORD", "").strip()
    pw = _master_password()
    if env_key:
        print(f"[bacopy-api] listening on http://{args.host}:{args.port}  (BACOPY_API_KEY is set)")
    else:
        print(f"[bacopy-api] listening on http://{args.host}:{args.port}  (generated BACOPY_API_KEY={key})")
    if env_pw:
        print("[bacopy-api] master UI: /master  (BACOPY_MASTER_PASSWORD is set)")
    else:
        print(f"[bacopy-api] master UI: /master  (generated BACOPY_MASTER_PASSWORD={pw})")
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
