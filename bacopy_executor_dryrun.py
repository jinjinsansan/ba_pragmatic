from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from decision_logger import append_decision_event


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _api_url() -> str:
    return os.getenv("BACOPY_API_URL", "http://127.0.0.1:8010").rstrip("/")


def _api_key() -> str:
    key = os.getenv("BACOPY_API_KEY", "").strip()
    if not key:
        raise SystemExit("BACOPY_API_KEY is required")
    return key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


def _get_snapshot(provider: str, table_id: str) -> Optional[dict[str, Any]]:
    r = requests.get(
        f"{_api_url()}/api/snapshots",
        params={"provider": provider, "table_id": table_id},
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    s = r.json().get("snapshot")
    return s if isinstance(s, dict) else None


def _resolve_table_id(provider: str, table_name: str) -> Optional[str]:
    if not table_name:
        return None
    r = requests.get(f"{_api_url()}/api/snapshots", headers=_headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    snaps = (data.get("snapshots") or {}).get(provider) or {}
    if not isinstance(snaps, dict):
        return None
    tn = table_name.strip().lower()
    # exact match
    for tid, s in snaps.items():
        name = (s or {}).get("table_name") or ""
        if str(name).strip().lower() == tn:
            return str(tid)
    # contains match (fallback)
    for tid, s in snaps.items():
        name = (s or {}).get("table_name") or ""
        if tn and tn in str(name).strip().lower():
            return str(tid)
    return None


def _infer_outcome(provider: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    if provider == "pragmatic":
        lh = (after or {}).get("last_hand") or {}
        winner = (lh or {}).get("winner")
        if winner == "PLAYER_WIN":
            return "player"
        if winner == "BANKER_WIN":
            return "banker"
        if winner == "TIE":
            return "tie"
        return "unknown"

    # evolution snapshot has counts
    try:
        bp = int((before or {}).get("p_count") or 0)
        bb = int((before or {}).get("b_count") or 0)
        bt = int((before or {}).get("tie_count") or 0)
        ap = int((after or {}).get("p_count") or 0)
        ab = int((after or {}).get("b_count") or 0)
        at = int((after or {}).get("tie_count") or 0)
    except Exception:
        bp = bb = bt = ap = ab = at = 0

    if ap > bp:
        return "player"
    if ab > bb:
        return "banker"
    if at > bt:
        return "tie"
    return "unknown"


def _has_advanced(provider: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
    if provider == "pragmatic":
        b = ((before or {}).get("last_hand") or {}).get("gameId")
        a = ((after or {}).get("last_hand") or {}).get("gameId")
        return bool(a and a != b)

    # evolution: check hands count
    try:
        bh = int((before or {}).get("hands") or 0)
        ah = int((after or {}).get("hands") or 0)
        return ah > bh
    except Exception:
        return False


def _post_ack(decision_id: str, ack: dict[str, Any], status: str = "processing") -> None:
    requests.post(
        f"{_api_url()}/api/decisions/{decision_id}/ack",
        headers=_headers(),
        json={"ack": ack, "status": status},
        timeout=10,
    ).raise_for_status()


def _post_result(decision_id: str, result: dict[str, Any], status: str = "done") -> None:
    requests.post(
        f"{_api_url()}/api/decisions/{decision_id}/result",
        headers=_headers(),
        json={"result": result, "status": status},
        timeout=10,
    ).raise_for_status()


def _fetch_decisions(status: str, limit: int) -> list[dict[str, Any]]:
    r = requests.get(
        f"{_api_url()}/api/decisions",
        params={"status": status, "limit": int(limit)},
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("decisions") or []
    return items if isinstance(items, list) else []


def process_one(d: dict[str, Any], *, result_timeout_sec: int = 180) -> None:
    did = str(d.get("decision_id") or "")
    provider = str(d.get("provider") or "")
    table_id = str(d.get("table_id") or "")
    table_name = str(d.get("table_name") or "")
    fa = d.get("friend_action") or {}
    action = str(fa.get("action") or "")

    if not table_id:
        table_id = _resolve_table_id(provider, table_name) or ""

    if not did or provider not in ("evolution", "pragmatic"):
        _post_result(did or "unknown", {"error": "invalid decision payload"}, status="error")
        return

    if not table_id:
        _post_result(did, {"error": "table_id required (or resolvable from table_name)"}, status="error")
        return

    before = _get_snapshot(provider, table_id) or {}
    ack = {
        "mode": "dry_run",
        "acked_at": _utc_now_iso(),
        "provider": provider,
        "table_id": table_id,
        "table_name": table_name or before.get("table_name") or "",
        "friend_action": fa,
        "snapshot_before": before,
    }
    _post_ack(did, ack, status="processing")

    # Wait for next hand after ack
    t0 = time.time()
    after = None
    while time.time() - t0 < result_timeout_sec:
        s = _get_snapshot(provider, table_id)
        if isinstance(s, dict) and _has_advanced(provider, before, s):
            after = s
            break
        time.sleep(0.5)

    if after is None:
        _post_result(
            did,
            {"error": "result timeout", "timeout_sec": result_timeout_sec, "snapshot_before": before},
            status="error",
        )
        return

    outcome = _infer_outcome(provider, before, after)
    result_payload = {
        "mode": "dry_run",
        "observed_at": _utc_now_iso(),
        "provider": provider,
        "table_id": table_id,
        "table_name": table_name or after.get("table_name") or "",
        "friend_action": fa,
        "outcome": outcome,
        "snapshot_before": before,
        "snapshot_after": after,
    }
    _post_result(did, result_payload, status="done")

    # Append a second log entry that includes outcome + snapshots for training/audit
    try:
        append_decision_event(
            {
                "schema_version": 1,
                "event_type": "decision_resolved",
                "decision_id": did,
                "captured_at": ack.get("acked_at"),
                "provider": provider,
                "table_id": table_id,
                "table_name": result_payload.get("table_name", ""),
                "snapshot": before,
                "friend_action": fa,
                "ack": ack,
                "result": outcome,
                "execution": {"mode": "dry_run"},
                "resolved_at": result_payload.get("observed_at"),
            }
        )
    except Exception:
        pass

    print(f"[done] {did} {provider} {result_payload.get('table_name')} -> {outcome} ({action})", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        # Windows default buffering can make the executor look "stuck".
        # Prefer line-buffered stdout for operator confidence.
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-sec", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--result-timeout-sec", type=int, default=180)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    print(f"[executor] api={_api_url()} poll={args.poll_sec}s limit={args.limit} timeout={args.result_timeout_sec}s", flush=True)

    while True:
        # Resume processing items first, then pending
        items = _fetch_decisions("processing", limit=args.limit)
        if not items:
            items = _fetch_decisions("pending", limit=args.limit)
        if items:
            for d in items:
                try:
                    process_one(d, result_timeout_sec=args.result_timeout_sec)
                except Exception as e:
                    did = str(d.get("decision_id") or "")
                    try:
                        _post_result(did, {"error": f"executor exception: {e}"}, status="error")
                    except Exception:
                        pass
        if args.once:
            break
        time.sleep(max(args.poll_sec, 0.2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
