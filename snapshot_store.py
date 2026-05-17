from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional


_LOCK = threading.RLock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _combined_path() -> str:
    """Legacy single-file snapshot store path (used only when env override is set)."""
    return os.getenv("BACOPY_SNAPSHOT_PATH", "").strip()


def _provider_path(provider: str) -> str:
    """Default per-provider snapshot path (prevents Windows cross-process replace collision)."""
    safe = (provider or "unknown").lower().strip()
    if not safe:
        safe = "unknown"
    return os.path.join("data", f"latest_snapshots_{safe}.json")


def _load_json(path: str) -> dict[str, Any]:
    # On Windows, readers can observe transient errors while another process is
    # doing os.replace(). Retry briefly to avoid "missing provider" flicker in
    # /api/snapshots responses.
    path_exists = os.path.exists(path)
    if not path_exists:
        return {}
    for attempt in range(30):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = json.load(f)
                return v if isinstance(v, dict) else {}
        except FileNotFoundError:
            # File truly absent: return immediately.
            if not os.path.exists(path):
                return {}
            # If file existed a moment ago, treat as transient and retry.
            if attempt >= 29:
                return {}
            time.sleep(0.03)
        except (PermissionError, json.JSONDecodeError):
            if attempt >= 29:
                return {}
            time.sleep(0.03)
        except Exception:
            return {}
    return {}


def load_snapshots() -> dict[str, Any]:
    """Load merged snapshots for all providers.

    By default, snapshots are stored per-provider:
      - data/latest_snapshots_evolution.json
      - data/latest_snapshots_pragmatic.json

    This avoids WinError 5 on concurrent os.replace across processes.

    If BACOPY_SNAPSHOT_PATH is explicitly set, we treat it as a *combined*
    legacy file path and read it too (compat).
    """
    merged: dict[str, Any] = {"updated_at": None, "snapshots": {}}
    snaps: dict[str, Any] = {}

    # Prefer per-provider files
    for prov in ("evolution", "pragmatic"):
        p = _provider_path(prov)
        d = _load_json(p)
        prov_snaps = d.get("snapshots") if isinstance(d, dict) else None
        if isinstance(prov_snaps, dict) and prov_snaps:
            snaps[prov] = prov_snaps
        u = d.get("updated_at") if isinstance(d, dict) else None
        if isinstance(u, str) and (merged["updated_at"] is None or u > merged["updated_at"]):
            merged["updated_at"] = u

    # Read legacy combined file if configured (or if it exists in the old default location)
    legacy_path = _combined_path() or os.path.join("data", "latest_snapshots.json")
    legacy = _load_json(legacy_path)
    legacy_snaps = legacy.get("snapshots") if isinstance(legacy, dict) else None
    if isinstance(legacy_snaps, dict):
        for prov, prov_map in legacy_snaps.items():
            if isinstance(prov, str) and isinstance(prov_map, dict) and prov_map:
                # Do not overwrite per-provider data if already present
                snaps.setdefault(prov, prov_map)
    legacy_u = legacy.get("updated_at") if isinstance(legacy, dict) else None
    if isinstance(legacy_u, str) and (merged["updated_at"] is None or legacy_u > merged["updated_at"]):
        merged["updated_at"] = legacy_u

    merged["snapshots"] = snaps
    return merged


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".snap_", suffix=".json", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        # On Windows, os.replace can throw WinError 5 if another process briefly holds the file.
        # Retry a few times to smooth transient sharing violations.
        for attempt in range(30):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt >= 29:
                    raise
                time.sleep(0.03)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def update_snapshot(provider: str, table_id: str, snapshot: dict[str, Any]) -> None:
    if not provider or not table_id:
        return
    with _LOCK:
        # If user explicitly configured BACOPY_SNAPSHOT_PATH, keep legacy combined format.
        legacy_path = _combined_path()
        if legacy_path:
            state = _load_json(legacy_path) or {"updated_at": None, "snapshots": {}}
            snaps = state.get("snapshots")
            if not isinstance(snaps, dict):
                snaps = {}
            prov = snaps.get(provider)
            if not isinstance(prov, dict):
                prov = {}
            prov[table_id] = snapshot
            snaps[provider] = prov
            state["snapshots"] = snaps
            state["updated_at"] = _utc_now_iso()
            _atomic_write_json(legacy_path, state)
            return

        # Default: per-provider file (prevents cross-process collision)
        p = _provider_path(provider)
        state = _load_json(p) or {"updated_at": None, "snapshots": {}}
        prov_snaps = state.get("snapshots")
        if not isinstance(prov_snaps, dict):
            prov_snaps = {}
        prov_snaps[table_id] = snapshot
        state["snapshots"] = prov_snaps
        state["updated_at"] = _utc_now_iso()
        _atomic_write_json(p, state)


def get_snapshot(provider: str, table_id: str) -> Optional[dict[str, Any]]:
    state = load_snapshots()
    snaps = state.get("snapshots") or {}
    prov = snaps.get(provider) if isinstance(snaps, dict) else None
    if not isinstance(prov, dict):
        return None
    s = prov.get(table_id)
    return s if isinstance(s, dict) else None
