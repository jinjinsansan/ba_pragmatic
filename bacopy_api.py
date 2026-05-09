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
  GET  /api/decisions/wait         (auth)  (long-poll)
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
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as _UrlRequest, urlopen as _urlopen
from urllib.error import URLError as _UrlError, HTTPError as _HttpError

from bacopy_db import (
    get_by_status,
    get_pending,
    get_stats,
    init_db,
    insert_decision,
    list_executors,
    get_executor_email,
    mark_ack,
    mark_result,
    upsert_executor,
    cancel_pending_bets_for_executor,
    cancel_all_pending_decisions,
    get_decision_target_executor,
)

from decision_logger import (
    append_decision_event,
    append_ack_event,
    append_result_event,
    reconstruct_decisions,
)
from snapshot_store import get_snapshot, load_snapshots, update_snapshot


_DECISION_WAIT_COND = threading.Condition()
_DECISION_WAIT_TICK = 0

# bafather approved-users cache (5 min TTL).
_APPROVED_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "error": ""}
_APPROVED_CACHE_LOCK = threading.Lock()
_APPROVED_CACHE_TTL_SEC = 300

def _fetch_approved_users() -> dict[str, Any]:
    """Fetch approved users from bafather, cached 5 minutes.

    Returns: {"ok": bool, "users": [...], "error": str, "fetched_at": str, "cached": bool}
    """
    now = time.time()
    with _APPROVED_CACHE_LOCK:
        cached = _APPROVED_CACHE.get("data")
        cached_at = float(_APPROVED_CACHE.get("at") or 0)
        if cached is not None and (now - cached_at) < _APPROVED_CACHE_TTL_SEC:
            return {"ok": True, **cached, "cached": True}
    base = (os.getenv("BACOPY_BAFATHER_URL", "") or "https://www.bafather.uk").rstrip("/")
    api_key = (os.getenv("LAPLACE_API_KEY", "") or "").strip()
    if not api_key:
        return {"ok": False, "error": "LAPLACE_API_KEY not set", "users": []}
    url = f"{base}/api/admin/approved-users"
    body = json.dumps({"api_key": api_key}).encode("utf-8")
    req = _UrlRequest(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"ok": False, "error": "unexpected response shape", "users": []}
        if not data.get("ok"):
            return {"ok": False, "error": str(data.get("reason") or "bafather_returned_false"), "users": []}
        users = data.get("users") or []
        fetched_at = data.get("fetched_at") or datetime.now(timezone.utc).isoformat()
        payload = {"users": users, "fetched_at": fetched_at, "error": ""}
        with _APPROVED_CACHE_LOCK:
            _APPROVED_CACHE["at"] = now
            _APPROVED_CACHE["data"] = payload
            _APPROVED_CACHE["error"] = ""
        return {"ok": True, **payload, "cached": False}
    except _HttpError as e:
        return {"ok": False, "error": f"http_error {e.code}", "users": []}
    except _UrlError as e:
        return {"ok": False, "error": f"url_error {e.reason}", "users": []}
    except Exception as e:
        return {"ok": False, "error": f"exception {e!r}", "users": []}


def _notify_decision_waiters() -> None:
    global _DECISION_WAIT_TICK
    with _DECISION_WAIT_COND:
        _DECISION_WAIT_TICK += 1
        _DECISION_WAIT_COND.notify_all()


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


def _compute_derived_roads(statistics: Any) -> dict[str, Any]:
    """大路(statistics)から中国罫線(大眼仔・小路・曱甴路)を計算する。

    統計グリッド: 列の配列。各列は ["PN0","BN0","---",...] 形式。
    先頭文字 P/B が勝者。"---" は空セル。

    各路の計算:
      r==0 (新列開始): 基準列の長さと1つ前の基準列の長さを比較 → 同一=Red, 異なる=Blue
      r>0  (列の継続):  基準列に同じ深さのエントリがあるか    → あり=Red, なし=Blue
    """
    try:
        grid = json.loads(statistics) if isinstance(statistics, str) else statistics
        if not isinstance(grid, list):
            return {}
    except Exception:
        return {}

    # 列を抽出 (空でないもののみ)
    columns: list[list[str]] = []
    for col in grid:
        entries = [c[0] for c in col if isinstance(c, str) and c != "---" and c and c[0] in "PBT"]
        if entries:
            columns.append(entries)

    def _road(offset: int) -> list[str]:
        # offset=1: 大眼仔, offset=2: 小路, offset=3: 曱甴路
        road: list[str] = []
        start_col = offset + 1  # 最低限必要な列数
        for c in range(start_col, len(columns) + 1):
            col_c   = columns[c - 1]
            col_ref = columns[c - 1 - offset]
            for r in range(len(col_c)):
                if r == 0:
                    prev_ref = columns[c - 2 - offset] if (c - 2 - offset) >= 0 else []
                    entry = "R" if len(col_ref) == len(prev_ref) else "B"
                else:
                    entry = "R" if r < len(col_ref) else "B"
                road.append(entry)
        return road

    return {
        "big_eye_boy":   _road(1)[-12:],
        "small_road":    _road(2)[-12:],
        "cockroach_road": _road(3)[-12:],
    }


def _fill_snapshot(provider: str, table_id: str, payload: dict[str, Any]) -> None:
    snap = payload.get("snapshot")
    if isinstance(snap, dict) and snap:
        # 既存スナップショットにも中国罫線を補完する
        if "derived_roads" not in snap:
            stats = snap.get("statistics")
            if stats:
                snap["derived_roads"] = _compute_derived_roads(stats)
        return
    if not provider or not table_id:
        return
    s = get_snapshot(provider, table_id)
    if isinstance(s, dict) and s:
        # 中国罫線を計算して付与
        stats = s.get("statistics")
        if stats and "derived_roads" not in s:
            s["derived_roads"] = _compute_derived_roads(stats)
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
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_text(handler: BaseHTTPRequestHandler, status: int, text: str, *, content_type: str = "text/plain; charset=utf-8") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
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
<title>BACOPYMASTER — sign in</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;700;900&family=Share+Tech+Mono&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#05080f;--bg-card:rgba(15,20,35,0.85);--bg-glass:rgba(20,28,50,0.60);--accent:#00e5ff;--win:#00ff88;--lose:#ff3366;--text:#e0e8f0;--text-muted:#7888a0;--border:rgba(0,229,255,0.12);--border-h:rgba(0,229,255,0.38);--font-hud:'Orbitron',sans-serif;--font-mono:'Share Tech Mono',monospace;--font-body:'Inter',sans-serif}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:var(--font-body);background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;overflow:hidden}}
body::before{{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,229,255,0.035) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,0.035) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0;opacity:0.5}}
body::after{{content:'';position:fixed;top:-100px;left:50%;transform:translateX(-50%);width:600px;height:360px;background:radial-gradient(ellipse at center,rgba(0,229,255,0.08) 0%,transparent 70%);pointer-events:none;z-index:0}}
.card{{position:relative;z-index:1;width:100%;max-width:440px;background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:32px;backdrop-filter:blur(10px);box-shadow:0 0 40px rgba(0,229,255,0.15)}}
.brand{{font-family:var(--font-hud);font-weight:900;font-size:22px;letter-spacing:8px;color:var(--accent);text-align:center;margin-bottom:4px;text-shadow:0 0 18px rgba(0,229,255,0.5)}}
.sub{{font-family:var(--font-mono);text-align:center;font-size:11px;color:var(--text-muted);letter-spacing:2px;margin-bottom:28px}}
label{{display:block;margin:16px 0 8px;color:var(--text-muted);font-family:var(--font-hud);font-size:10px;letter-spacing:3px;text-transform:uppercase}}
input{{width:100%;padding:14px 16px;border-radius:10px;border:1px solid var(--border);background:var(--bg-glass);color:var(--text);font-family:var(--font-mono);font-size:14px;letter-spacing:2px}}
input:focus{{outline:none;border-color:var(--accent);box-shadow:0 0 12px rgba(0,229,255,0.3)}}
button{{width:100%;margin-top:20px;padding:14px;border-radius:10px;border:1px solid var(--border-h);background:linear-gradient(180deg,rgba(0,229,255,0.18),rgba(0,229,255,0.04));color:var(--accent);font-family:var(--font-hud);font-weight:700;letter-spacing:4px;font-size:14px;cursor:pointer;transition:all .2s}}
button:hover{{background:linear-gradient(180deg,rgba(0,229,255,0.3),rgba(0,229,255,0.1));box-shadow:0 0 20px rgba(0,229,255,0.35)}}
.err{{margin:12px 0;color:var(--lose);font-family:var(--font-mono);font-size:12px;padding:10px;background:rgba(255,51,102,0.08);border:1px solid rgba(255,51,102,0.28);border-radius:8px}}
.hint{{color:var(--text-muted);font-size:11px;line-height:1.6;margin-top:18px;padding-top:14px;border-top:1px dashed var(--border);font-family:var(--font-mono)}}
</style></head>
<body><div class="card">
<div class="brand">BACOPYMASTER</div>
<div class="sub">operator console</div>
{err}
<form method="POST" action="/master/login">
  <label>Password</label>
  <input name="password" type="password" autofocus/>
  <button type="submit">SIGN IN</button>
</form>
<p class="hint">※ VPS 公開時は HTTPS (リバプロ) 必須（平文 HTTP はパスワード漏洩リスクあり）。</p>
</div></body></html>"""


def _master_app_page(csrf: str) -> str:
    from bacopy_master_ui import render_master_app
    return render_master_app(csrf)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            return _redirect(self, "/master")

        if u.path == "/master/login":
            return _send_html(self, 200, _master_login_page())
        if u.path == "/master/theme.css":
            # Share the exact same theme as the Receiver GUI (copytrade_gui).
            # Not sensitive; allow without auth so CSS loads reliably.
            css_path = Path(__file__).parent / "copytrade_gui" / "src" / "renderer" / "styles.css"
            try:
                css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
            except Exception:
                css = ""
            return _send_text(self, 200, css, content_type="text/css; charset=utf-8")
        if u.path == "/master":
            s = _get_session(self.headers)
            if not s:
                return _redirect(self, "/master/login")
            return _send_html(self, 200, _master_app_page(str(s.get("csrf") or "")))

        if u.path == "/master/ledger":
            return _redirect(self, "https://www.bafather.uk/admin/ledger")

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
        if u.path == "/api/approved-users":
            res = _fetch_approved_users()
            if res.get("ok"):
                return _send_json(self, 200, res)
            return _send_json(self, 502, res)
        if u.path == "/api/training/export":
            # ML 学習データ: event-sourced JSONL を decision_id 毎に集約.
            # 学習クライアントはこれ 1 本で完全レコードを得られる.
            qs = parse_qs(u.query or "")
            complete_only = (qs.get("complete_only") or ["0"])[0] in ("1","true","yes")
            path = os.getenv("BACOPY_DECISIONS_JSONL", "data/decisions.jsonl")
            records = reconstruct_decisions(path)
            if complete_only:
                records = [r for r in records if r.get("status") == "done" and r.get("result")]
            return _send_json(self, 200, {
                "ok": True,
                "count": len(records),
                "complete_only": complete_only,
                "records": records,
            })
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
            executor_id_qs = str((qs.get("executor_id") or [""])[0] or "")
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except Exception:
                limit = 50
            # approved-users check: executor が pending を取得する時のみ検証
            if status == "pending" and executor_id_qs:
                email = get_executor_email(executor_id_qs)
                if email:
                    approved = _fetch_approved_users()
                    if approved.get("ok"):
                        approved_emails = {
                            str(u.get("email") or "").lower()
                            for u in (approved.get("users") or [])
                        }
                        if email.lower() not in approved_emails:
                            return _send_json(self, 200, {
                                "decisions": [],
                                "approved": False,
                                "reason": "not_approved",
                            })
            return _send_json(self, 200, {"decisions": get_by_status(status, limit=limit)})
        if u.path == "/api/decisions/wait":
            qs = parse_qs(u.query or "")
            status = (qs.get("status") or ["pending"])[0]
            provider = str((qs.get("provider") or [""])[0] or "")
            executor_id = str((qs.get("executor_id") or [""])[0] or "")
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except Exception:
                limit = 50
            try:
                wait_sec = float((qs.get("wait_sec") or ["20"])[0])
            except Exception:
                wait_sec = 20.0
            wait_sec = max(0.2, min(25.0, wait_sec))

            def _match(d: dict[str, Any]) -> bool:
                if provider and str(d.get("provider") or "") != provider:
                    return False
                if not executor_id:
                    return True
                tgt = str(d.get("target_executor_id") or "")
                # If executor_id was specified, return broadcast (no target) + explicit matches.
                return (not tgt) or tgt == executor_id

            deadline = time.time() + wait_sec
            while True:
                with _DECISION_WAIT_COND:
                    tick = _DECISION_WAIT_TICK
                items = [d for d in (get_by_status(status, limit=limit) or []) if isinstance(d, dict) and _match(d)]
                if items:
                    return _send_json(self, 200, {"ok": True, "decisions": items})
                remaining = deadline - time.time()
                if remaining <= 0:
                    return _send_json(self, 200, {"ok": True, "decisions": []})
                with _DECISION_WAIT_COND:
                    # If a decision arrived between our DB read and waiting, loop and re-check.
                    if _DECISION_WAIT_TICK != tick:
                        continue
                    _DECISION_WAIT_COND.wait(timeout=remaining)
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
            _notify_decision_waiters()
            return _send_json(self, 200, {"accepted": True, "decision_id": decision_id})

        if u.path == "/api/snapshots/update":
            body = _read_json(self)
            provider = str(body.get("provider") or "")
            table_id = str(body.get("table_id") or "")
            snapshot = body.get("snapshot")
            if provider and table_id and isinstance(snapshot, dict):
                update_snapshot(provider, table_id, snapshot)
                return _send_json(self, 200, {"ok": True})
            # bulk form: {snapshots:{provider:{table_id:snapshot}}}
            snaps = body.get("snapshots")
            if isinstance(snaps, dict):
                n = 0
                for prov, mp in snaps.items():
                    if not isinstance(prov, str) or not isinstance(mp, dict):
                        continue
                    for tid, snap in mp.items():
                        if isinstance(tid, str) and isinstance(snap, dict):
                            update_snapshot(prov, tid, snap)
                            n += 1
                return _send_json(self, 200, {"ok": True, "count": n})
            return _send_json(self, 400, {"ok": False, "error": "provider/table_id/snapshot required"})

        if u.path == "/api/executors/heartbeat":
            body = _read_json(self)
            executor_id = str(body.get("executor_id") or "")
            if len(executor_id) < 4:
                return _send_json(self, 400, {"ok": False, "error": "executor_id required"})
            upsert_executor(executor_id, body if isinstance(body, dict) else {})
            return _send_json(self, 200, {"ok": True})

        if u.path == "/api/decisions/cancel-pending":
            # 全 pending decision を強制 error 遷移 (#11 学習セッション pending 詰まり対応)
            body = _read_json(self)
            reason = str((body.get("reason") if isinstance(body, dict) else None) or "manual_cancel")
            cancelled = cancel_all_pending_decisions(reason=reason)
            return _send_json(self, 200, {"ok": True, "cancelled": cancelled})

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
                # ML 契約: JSONL にも ack event を追記
                append_ack_event(decision_id, ack, status=status)
                # SWITCH_TABLE が ack された場合、pending BET をキャンセル
                # (テーブル移動中に BET が stale になるのを防ぐ)
                if isinstance(ack, dict):
                    action = str((ack.get("friend_action") or {}).get("action") or "").upper()
                    if action == "SWITCH_TABLE":
                        # DB から target_executor_id を取得 (ack body に含まれていないため)
                        tgt_exec = get_decision_target_executor(decision_id)
                        n = cancel_pending_bets_for_executor(tgt_exec, decision_id)
                        if n:
                            print(f"[ack] cancelled {n} stale BET(s) for executor={tgt_exec or 'broadcast'} after SWITCH_TABLE", flush=True)
                _notify_decision_waiters()
                return _send_json(self, 200, {"ok": True})
            if parts[3] == "result":
                result = body.get("result") if isinstance(body, dict) else {}
                if not isinstance(result, dict):
                    result = {}
                status = str(body.get("status") or "done") if isinstance(body, dict) else "done"
                mark_result(decision_id, result, status=status)
                # ML 契約: JSONL にも result event を追記
                append_result_event(decision_id, result, status=status)
                _notify_decision_waiters()
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
