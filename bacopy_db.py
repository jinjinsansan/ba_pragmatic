from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _db_path() -> str:
    return os.getenv("BACOPY_DB_PATH", "data/bacopy.sqlite3")


def init_db() -> None:
    os.makedirs(os.path.dirname(_db_path()) or ".", exist_ok=True)
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
              decision_id TEXT PRIMARY KEY,
              received_at TEXT NOT NULL,
              status TEXT NOT NULL,
              provider TEXT,
              table_id TEXT,
              table_name TEXT,
              game_id TEXT,
              payload_json TEXT NOT NULL,
              ack_json TEXT,
              result_json TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_received_at ON decisions(received_at)")
        conn.commit()
    finally:
        conn.close()


def insert_decision(decision_id: str, payload: dict[str, Any]) -> None:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO decisions
              (decision_id, received_at, status, provider, table_id, table_name, game_id, payload_json)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                _utc_now_iso(),
                "pending",
                str(payload.get("provider") or ""),
                str(payload.get("table_id") or ""),
                str(payload.get("table_name") or ""),
                str(payload.get("game_id") or ""),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending(limit: int = 50) -> list[dict[str, Any]]:
    return get_by_status("pending", limit=limit)


def get_by_status(status: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT decision_id, received_at, payload_json
            FROM decisions
            WHERE status = ?
            ORDER BY received_at ASC
            LIMIT ?
            """,
            (str(status or ""), int(limit)),
        )
        rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for did, at, pjson in rows:
            try:
                payload = json.loads(pjson) if pjson else {}
            except Exception:
                payload = {}
            payload.setdefault("decision_id", did)
            payload.setdefault("received_at", at)
            out.append(payload)
        return out
    finally:
        conn.close()


def mark_ack(decision_id: str, ack: dict[str, Any], status: str = "acked") -> None:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE decisions
            SET status = ?, ack_json = ?
            WHERE decision_id = ?
            """,
            (status, json.dumps(ack, ensure_ascii=False), decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_result(decision_id: str, result: dict[str, Any], status: str = "done") -> None:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE decisions
            SET status = ?, result_json = ?
            WHERE decision_id = ?
            """,
            (status, json.dumps(result, ensure_ascii=False), decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_stats() -> dict[str, Any]:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM decisions GROUP BY status")
        counts = {s: int(c) for s, c in cur.fetchall()}
        cur.execute("SELECT MAX(received_at) FROM decisions")
        last_at = cur.fetchone()[0]
        return {"counts": counts, "last_received_at": last_at}
    finally:
        conn.close()
