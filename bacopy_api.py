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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from bacopy_db import get_by_status, get_pending, get_stats, init_db, insert_decision, mark_ack, mark_result
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


def _auth_ok(headers) -> bool:
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    token = auth.split(" ", 1)[1].strip()
    return token == _expected_api_key()


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


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
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
        if not _auth_ok(self.headers):
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
    if env_key:
        print(f"[bacopy-api] listening on http://{args.host}:{args.port}  (BACOPY_API_KEY is set)")
    else:
        print(f"[bacopy-api] listening on http://{args.host}:{args.port}  (generated BACOPY_API_KEY={key})")
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
