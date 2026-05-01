from __future__ import annotations

import json
import os
import sqlite3
import time
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
              target_executor_id TEXT,
              action TEXT,
              side TEXT,
              amount REAL,
              game_id TEXT,
              payload_json TEXT NOT NULL,
              ack_json TEXT,
              result_json TEXT
            )
            """
        )
        # Backward-compatible schema migration for decisions table.
        for ddl in [
            "ALTER TABLE decisions ADD COLUMN target_executor_id TEXT",
            "ALTER TABLE decisions ADD COLUMN action TEXT",
            "ALTER TABLE decisions ADD COLUMN side TEXT",
            "ALTER TABLE decisions ADD COLUMN amount REAL",
        ]:
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column name" in msg:
                    continue
                raise
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_received_at ON decisions(received_at)")

        # Idempotency guard for BET. PK includes executor_id so that multiple
        # executors (different Stake accounts) on the same table+game_id can each
        # lock independently. Old schema (no executor_id) is dropped & recreated.
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='bet_guard'")
        row = cur.fetchone()
        needed_bg_pk = "PRIMARY KEY (executor_id, provider, table_id, game_id)"
        if row and needed_bg_pk not in (row[0] or ""):
            cur.execute("DROP TABLE bet_guard")
            row = None
        if not row:
            cur.execute(
                """
                CREATE TABLE bet_guard (
                  executor_id TEXT NOT NULL DEFAULT '',
                  provider TEXT NOT NULL,
                  table_id TEXT NOT NULL,
                  game_id TEXT NOT NULL,
                  decision_id TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (executor_id, provider, table_id, game_id)
                )
                """
            )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bet_guard_created_at ON bet_guard(created_at)")

        # Idempotency guard for decision execution (across executor restarts).
        # PK is (decision_id, executor_id) so broadcasts let every matching executor
        # execute exactly once independently.
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_exec_guard'")
        row = cur.fetchone()
        needed_deg_pk = "PRIMARY KEY (decision_id, executor_id)"
        if row and needed_deg_pk not in (row[0] or ""):
            cur.execute("DROP TABLE decision_exec_guard")
            row = None
        if not row:
            cur.execute(
                """
                CREATE TABLE decision_exec_guard (
                  decision_id TEXT NOT NULL,
                  executor_id TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (decision_id, executor_id)
                )
                """
            )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_exec_guard_created_at ON decision_exec_guard(created_at)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS executors (
              executor_id TEXT PRIMARY KEY,
              label TEXT,
              username TEXT,
              user_email TEXT,
              user_id TEXT,
              provider TEXT,
              table_id TEXT,
              table_name TEXT,
              balance REAL,
              seq_json TEXT,
              gui_json TEXT,
              phase_json TEXT,
              caps_json TEXT,
              ws_json TEXT,
              bettable INTEGER,
              bet_window_open INTEGER,
              bet_window_open_age_sec REAL,
              session_elsewhere_unresolved INTEGER,
              daily_pnl REAL,
              daily_pnl_date TEXT,
              os TEXT,
              recovering INTEGER,
              recovering_reason TEXT,
              inactivity_dismissed_count INTEGER,
              inactivity_modal_unresolved INTEGER,
              status TEXT,
              error TEXT,
              updated_at TEXT NOT NULL
            )
            """
        )
        # Backward-compatible schema migration (old DB may miss new columns).
        for ddl in [
            "ALTER TABLE executors ADD COLUMN user_email TEXT",
            "ALTER TABLE executors ADD COLUMN user_id TEXT",
            "ALTER TABLE executors ADD COLUMN caps_json TEXT",
            "ALTER TABLE executors ADD COLUMN ws_json TEXT",
            "ALTER TABLE executors ADD COLUMN bettable INTEGER",
            "ALTER TABLE executors ADD COLUMN bet_window_open INTEGER",
            "ALTER TABLE executors ADD COLUMN bet_window_open_age_sec REAL",
            "ALTER TABLE executors ADD COLUMN session_elsewhere_unresolved INTEGER",
            "ALTER TABLE executors ADD COLUMN gui_json TEXT",
            "ALTER TABLE executors ADD COLUMN phase_json TEXT",
            "ALTER TABLE executors ADD COLUMN daily_pnl REAL",
            "ALTER TABLE executors ADD COLUMN daily_pnl_date TEXT",
            "ALTER TABLE executors ADD COLUMN os TEXT",
            "ALTER TABLE executors ADD COLUMN recovering INTEGER",
            "ALTER TABLE executors ADD COLUMN recovering_reason TEXT",
            "ALTER TABLE executors ADD COLUMN inactivity_dismissed_count INTEGER",
            "ALTER TABLE executors ADD COLUMN inactivity_modal_unresolved INTEGER",
        ]:
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column name" in msg:
                    continue
                raise
        cur.execute("CREATE INDEX IF NOT EXISTS idx_executors_updated_at ON executors(updated_at)")
        conn.commit()
    finally:
        conn.close()


def insert_decision(decision_id: str, payload: dict[str, Any]) -> None:
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        fa = payload.get("friend_action") if isinstance(payload, dict) else None
        if not isinstance(fa, dict):
            fa = {}
        action = str(fa.get("action") or "")
        side = str(fa.get("side") or "")
        amt = fa.get("amount")
        try:
            amount = float(amt) if amt is not None else None
        except Exception:
            amount = None
        cur.execute(
            """
            INSERT OR REPLACE INTO decisions
              (decision_id, received_at, status, provider, table_id, table_name, target_executor_id, action, side, amount, game_id, payload_json)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                _utc_now_iso(),
                "pending",
                str(payload.get("provider") or ""),
                str(payload.get("table_id") or ""),
                str(payload.get("table_name") or ""),
                str(payload.get("target_executor_id") or ""),
                action,
                side,
                amount,
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
            ORDER BY received_at DESC
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


def get_decision_payload(decision_id: str) -> dict:
    """指定 decision の payload_json を返す。"""
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute("SELECT payload_json FROM decisions WHERE decision_id=?", (decision_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            return {}
        try:
            return json.loads(row[0])
        except Exception:
            return {}
    finally:
        conn.close()


def get_decision_target_executor(decision_id: str) -> str:
    """指定 decision の target_executor_id を返す（空文字=ブロードキャスト）。"""
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute("SELECT target_executor_id FROM decisions WHERE decision_id=?", (decision_id,))
        row = cur.fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()


def cancel_pending_bets_for_executor(executor_id: str, superseded_by: str) -> int:
    """SWITCH_TABLE ack 後に pending BET を自動キャンセル。
    executor_id が空文字の場合はブロードキャスト扱いで全 BET をキャンセル。
    stale BET が別テーブルで実行されるのを防ぐ。戻り値はキャンセルした件数。"""
    if not superseded_by:
        return 0
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT decision_id, payload_json FROM decisions
            WHERE status = 'pending'
            """,
        )
        rows = cur.fetchall()
        cancelled = 0
        for did, pjson in rows:
            if did == superseded_by:
                continue
            try:
                p = json.loads(pjson) if pjson else {}
            except Exception:
                continue
            tgt = str(p.get("target_executor_id") or "")
            if executor_id:
                # 特定 executor 向け: 対象外 executor の BET はスキップ
                if tgt and tgt != executor_id:
                    continue
            # executor_id が空(ブロードキャスト): 全 pending BET をキャンセル
            fa = p.get("friend_action") or {}
            if not isinstance(fa, dict):
                continue
            if str(fa.get("action") or "").upper() != "BET":
                continue
            result = {
                "error": "cancelled: SWITCH_TABLE in progress",
                "superseded_by": superseded_by,
                "executor_id": executor_id,
            }
            cur.execute(
                "UPDATE decisions SET status='error', result_json=? WHERE decision_id=?",
                (json.dumps(result, ensure_ascii=False), did),
            )
            cancelled += 1
        conn.commit()
        return cancelled
    finally:
        conn.close()


def cancel_all_pending_decisions(reason: str = "manual_cancel") -> int:
    """全ての pending decision を error に強制遷移する。
    学習セッション OFF 時などに pending 詰まりを解消するために使う (#11)。
    戻り値はキャンセルした件数。
    """
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute("SELECT decision_id FROM decisions WHERE status = 'pending'")
        rows = cur.fetchall()
        cancelled = 0
        for (did,) in rows:
            result_payload = json.dumps({"error": f"cancelled: {reason}"}, ensure_ascii=False)
            cur.execute(
                "UPDATE decisions SET status='error', result_json=? WHERE decision_id=?",
                (result_payload, did),
            )
            cancelled += 1
        conn.commit()
        return cancelled
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
        try:
            cur.execute("SELECT COUNT(*) FROM decisions WHERE action = 'BET'")
            bet_total = int(cur.fetchone()[0] or 0)
        except Exception:
            bet_total = 0
        try:
            # in_learning_session フラグがある場合は true のみカウント
            # フラグなし (旧データ) は後方互換でカウント対象のまま
            cur.execute("""SELECT COUNT(*) FROM decisions
                WHERE action = 'BET' AND status = 'done'
                AND (
                    json_extract(payload_json, '$.friend_action.in_learning_session') IS NULL
                    OR json_extract(payload_json, '$.friend_action.in_learning_session') = 1
                )""")
            bet_done = int(cur.fetchone()[0] or 0)
        except Exception:
            bet_done = 0
        try:
            cur.execute("SELECT COUNT(*) FROM decisions WHERE action = 'BET' AND status = 'error'")
            bet_error = int(cur.fetchone()[0] or 0)
        except Exception:
            bet_error = 0
        return {
            "counts": counts,
            "last_received_at": last_at,
            "training": {"bet_goal": 5000, "bets_total": bet_total, "bets_done": bet_done, "bets_error": bet_error},
        }
    finally:
        conn.close()


def try_lock_bet(*, executor_id: str, provider: str, table_id: str, game_id: str, decision_id: str = "") -> bool:
    """Acquire a durable lock for a BET for this (executor_id, provider, table_id, game_id).

    Each executor can lock independently — multiple executors on the same table
    (different Stake accounts) must each lock their own row so that a broadcast
    BET can be placed in parallel.

    Returns True if lock acquired, False if already locked by this same executor.
    """
    if not executor_id or not provider or not table_id or not game_id:
        return False
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        # Short busy-timeout: betting window is time-sensitive; fail fast and retry.
        conn = sqlite3.connect(_db_path(), timeout=0.2)
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO bet_guard(executor_id, provider, table_id, game_id, decision_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (executor_id, provider, table_id, game_id, decision_id, _utc_now_iso()),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            except sqlite3.OperationalError as e:
                last_exc = e
        finally:
            conn.close()
        time.sleep(0.2 * (attempt + 1))
    raise sqlite3.OperationalError(str(last_exc) if last_exc else "bet_guard: retry exhausted")


def try_mark_decision_executed(decision_id: str, executor_id: str = "") -> bool:
    """Idempotency guard at (decision_id, executor_id) level.

    Each executor reserves its own row for a decision, so broadcasts let every
    matching executor execute exactly once independently.

    Returns True if newly reserved for this executor, False if this executor
    already reserved this decision.
    """
    did = str(decision_id or "").strip()
    eid = str(executor_id or "").strip()
    if not did:
        return False
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO decision_exec_guard(decision_id, executor_id, created_at)
                VALUES (?, ?, ?)
                """,
                (did, eid, _utc_now_iso()),
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
              (executor_id, label, username, user_email, user_id, provider, table_id, table_name, balance, seq_json, gui_json, phase_json, caps_json, ws_json, bettable, bet_window_open, bet_window_open_age_sec, session_elsewhere_unresolved, daily_pnl, daily_pnl_date, os, recovering, recovering_reason, inactivity_dismissed_count, inactivity_modal_unresolved, status, error, updated_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(executor_id) DO UPDATE SET
              label=excluded.label,
              username=excluded.username,
              user_email=excluded.user_email,
              user_id=excluded.user_id,
              provider=excluded.provider,
              table_id=excluded.table_id,
              table_name=excluded.table_name,
              balance=excluded.balance,
              seq_json=excluded.seq_json,
              gui_json=excluded.gui_json,
              phase_json=excluded.phase_json,
              caps_json=excluded.caps_json,
              ws_json=excluded.ws_json,
              bettable=excluded.bettable,
              bet_window_open=excluded.bet_window_open,
              bet_window_open_age_sec=excluded.bet_window_open_age_sec,
              session_elsewhere_unresolved=excluded.session_elsewhere_unresolved,
              daily_pnl=excluded.daily_pnl,
              daily_pnl_date=excluded.daily_pnl_date,
              os=excluded.os,
              recovering=excluded.recovering,
              recovering_reason=excluded.recovering_reason,
              inactivity_dismissed_count=excluded.inactivity_dismissed_count,
              inactivity_modal_unresolved=excluded.inactivity_modal_unresolved,
              status=excluded.status,
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (
                executor_id,
                str(payload.get("label") or ""),
                str(payload.get("username") or ""),
                str(payload.get("user_email") or ""),
                str(payload.get("user_id") or ""),
                str(payload.get("provider") or ""),
                str(payload.get("table_id") or ""),
                str(payload.get("table_name") or ""),
                float(payload.get("balance")) if payload.get("balance") is not None else None,
                json.dumps(payload.get("seq") or {}, ensure_ascii=False) if payload.get("seq") is not None else None,
                json.dumps(payload.get("gui") or {}, ensure_ascii=False) if payload.get("gui") is not None else None,
                json.dumps(payload.get("phase") or {}, ensure_ascii=False) if payload.get("phase") is not None else None,
                json.dumps(payload.get("caps") or {}, ensure_ascii=False) if payload.get("caps") is not None else None,
                json.dumps(payload.get("ws") or {}, ensure_ascii=False) if payload.get("ws") is not None else None,
                int(bool(payload.get("bettable"))) if payload.get("bettable") is not None else None,
                int(bool(payload.get("bet_window_open"))) if payload.get("bet_window_open") is not None else None,
                float(payload.get("bet_window_open_age_sec")) if payload.get("bet_window_open_age_sec") is not None else None,
                int(bool(payload.get("session_elsewhere_unresolved"))) if payload.get("session_elsewhere_unresolved") is not None else None,
                float(payload.get("daily_pnl")) if payload.get("daily_pnl") is not None else None,
                str(payload.get("daily_pnl_date") or ""),
                str(payload.get("os") or ""),
                int(bool(payload.get("recovering"))) if payload.get("recovering") is not None else 0,
                str(payload.get("recovering_reason") or ""),
                int(payload.get("inactivity_dismissed_count") or 0),
                int(bool(payload.get("inactivity_modal_unresolved"))) if payload.get("inactivity_modal_unresolved") is not None else 0,
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
            SELECT executor_id, label, username, user_email, user_id, provider, table_id, table_name, balance, seq_json, gui_json, phase_json, caps_json, ws_json, bettable, bet_window_open, bet_window_open_age_sec, session_elsewhere_unresolved, daily_pnl, daily_pnl_date, os, recovering, recovering_reason, inactivity_dismissed_count, inactivity_modal_unresolved, status, error, updated_at
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
                user_email,
                user_id,
                provider,
                table_id,
                table_name,
                balance,
                seq_json,
                gui_json,
                phase_json,
                caps_json,
                ws_json,
                bettable,
                bet_window_open,
                bet_window_open_age_sec,
                session_elsewhere_unresolved,
                daily_pnl,
                daily_pnl_date,
                os_name,
                recovering,
                recovering_reason,
                inactivity_dismissed_count,
                inactivity_modal_unresolved,
                status,
                error,
                updated_at,
            ) = r
            try:
                seq = json.loads(seq_json) if seq_json else {}
            except Exception:
                seq = {}
            try:
                gui = json.loads(gui_json) if gui_json else {}
            except Exception:
                gui = {}
            try:
                phase = json.loads(phase_json) if phase_json else {}
            except Exception:
                phase = {}
            try:
                caps = json.loads(caps_json) if caps_json else {}
            except Exception:
                caps = {}
            try:
                ws = json.loads(ws_json) if ws_json else {}
            except Exception:
                ws = {}
            out.append(
                {
                    "executor_id": executor_id,
                    "label": label or "",
                    "username": username or "",
                    "user_email": user_email or "",
                    "user_id": user_id or "",
                    "provider": provider or "",
                    "table_id": table_id or "",
                    "table_name": table_name or "",
                    "balance": balance,
                    "seq": seq,
                    "gui": gui,
                    "phase": phase,
                    "caps": caps,
                    "ws": ws,
                    "bettable": bool(bettable) if bettable is not None else False,
                    "bet_window_open": (bool(bet_window_open) if bet_window_open is not None else None),
                    "bet_window_open_age_sec": bet_window_open_age_sec,
                    "session_elsewhere_unresolved": bool(session_elsewhere_unresolved) if session_elsewhere_unresolved is not None else False,
                    "daily_pnl": daily_pnl,
                    "daily_pnl_date": daily_pnl_date or "",
                    "os": os_name or "",
                    "recovering": bool(recovering) if recovering is not None else False,
                    "recovering_reason": recovering_reason or "",
                    "inactivity_dismissed_count": int(inactivity_dismissed_count or 0),
                    "inactivity_modal_unresolved": bool(inactivity_modal_unresolved) if inactivity_modal_unresolved is not None else False,
                    "status": status or "",
                    "error": error or "",
                    "updated_at": updated_at,
                }
            )
        return out
    finally:
        conn.close()


def get_executor_email(executor_id: str) -> str:
    """Return the user_email stored for a given executor_id (empty string if not found)."""
    if not executor_id:
        return ""
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_email FROM executors WHERE executor_id=? LIMIT 1", (executor_id,))
        row = cur.fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()
