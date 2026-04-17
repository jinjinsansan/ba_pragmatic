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

        # Prevent duplicate BET per (provider, table_id, game_id) across retries/restarts.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bet_guard (
              provider TEXT NOT NULL,
              table_id TEXT NOT NULL,
              game_id TEXT NOT NULL,
              decision_id TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (provider, table_id, game_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bet_guard_created_at ON bet_guard(created_at)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS executors (
              executor_id TEXT PRIMARY KEY,
              label TEXT,
              username TEXT,
              provider TEXT,
              table_id TEXT,
              table_name TEXT,
              balance REAL,
              seq_json TEXT,
              status TEXT,
              error TEXT,
              updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_executors_updated_at ON executors(updated_at)")
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
            SELECT decision_id, received_at, status, payload_json, ack_json, result_json
            FROM decisions
            WHERE status = ?
            ORDER BY received_at ASC
            LIMIT ?
            """,
            (str(status or ""), int(limit)),
        )
        rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for did, at, st, pjson, ajson, rjson in rows:
            try:
                payload = json.loads(pjson) if pjson else {}
            except Exception:
                payload = {}
            payload.setdefault("decision_id", did)
            payload.setdefault("received_at", at)
            payload.setdefault("status", st)
            try:
                payload.setdefault("ack", json.loads(ajson) if ajson else {})
            except Exception:
                payload.setdefault("ack", {})
            try:
                payload.setdefault("result", json.loads(rjson) if rjson else {})
            except Exception:
                payload.setdefault("result", {})
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


def try_lock_bet(*, provider: str, table_id: str, game_id: str, decision_id: str = "") -> bool:
    """Acquire a durable lock for a BET for this (provider, table_id, game_id).

    Returns True if lock acquired, False if already locked.
    """
    if not provider or not table_id or not game_id:
        return False
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO bet_guard(provider, table_id, game_id, decision_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (provider, table_id, game_id, decision_id, _utc_now_iso()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()


def upsert_executor(executor_id: str, payload: dict[str, Any]) -> None:
    if not executor_id:
        return
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO executors
              (executor_id, label, username, provider, table_id, table_name, balance, seq_json, status, error, updated_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(executor_id) DO UPDATE SET
              label=excluded.label,
              username=excluded.username,
              provider=excluded.provider,
              table_id=excluded.table_id,
              table_name=excluded.table_name,
              balance=excluded.balance,
              seq_json=excluded.seq_json,
              status=excluded.status,
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (
                executor_id,
                str(payload.get("label") or ""),
                str(payload.get("username") or ""),
                str(payload.get("provider") or ""),
                str(payload.get("table_id") or ""),
                str(payload.get("table_name") or ""),
                float(payload.get("balance")) if payload.get("balance") is not None else None,
                json.dumps(payload.get("seq") or {}, ensure_ascii=False) if payload.get("seq") is not None else None,
                str(payload.get("status") or ""),
                str(payload.get("error") or ""),
                _utc_now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_executors(limit: int = 200) -> list[dict[str, Any]]:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT executor_id, label, username, provider, table_id, table_name, balance, seq_json, status, error, updated_at
            FROM executors
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            (
                executor_id,
                label,
                username,
                provider,
                table_id,
                table_name,
                balance,
                seq_json,
                status,
                error,
                updated_at,
            ) = r
            try:
                seq = json.loads(seq_json) if seq_json else {}
            except Exception:
                seq = {}
            out.append(
                {
                    "executor_id": executor_id,
                    "label": label or "",
                    "username": username or "",
                    "provider": provider or "",
                    "table_id": table_id or "",
                    "table_name": table_name or "",
                    "balance": balance,
                    "seq": seq,
                    "status": status or "",
                    "error": error or "",
                    "updated_at": updated_at,
                }
            )
        return out
    finally:
        conn.close()
