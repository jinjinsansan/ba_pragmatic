"""Pragmatic Play バカラ shoe データの蓄積DB

Evolution版 (analytics_db.py) と互換性ある `shoes` テーブル
+ Pragmatic固有の豊富なメタデータを保持する `shoes_pragmatic_extra`。
"""
from __future__ import annotations
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "analytics_pragmatic.sqlite3"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS shoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            table_type TEXT,
            started_at TEXT,
            ended_at TEXT NOT NULL,
            day_of_week INTEGER,
            day_of_month INTEGER,
            hour_of_day INTEGER,
            month INTEGER,
            year INTEGER,
            is_weekend INTEGER,
            is_month_end INTEGER,
            hand_count INTEGER NOT NULL,
            player_count INTEGER NOT NULL,
            banker_count INTEGER NOT NULL,
            tie_count INTEGER NOT NULL,
            result_sequence TEXT NOT NULL,
            winners TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(table_id, ended_at)
        );
        CREATE INDEX IF NOT EXISTS idx_pshoes_table ON shoes(table_id);
        CREATE INDEX IF NOT EXISTS idx_pshoes_time ON shoes(ended_at);

        -- Pragmatic固有データ (カード・statistics・goodRoads等)
        CREATE TABLE IF NOT EXISTS shoes_extra (
            shoe_id INTEGER PRIMARY KEY,
            game_ids TEXT,          -- JSON array of gameIds
            player_cards TEXT,      -- JSON array of arrays
            banker_cards TEXT,      -- JSON array of arrays
            statistics_raw TEXT,
            shoe_summary TEXT,
            good_roads_map TEXT,
            FOREIGN KEY(shoe_id) REFERENCES shoes(id) ON DELETE CASCADE
        );

        -- 生メッセージログ (解析用・初期のみ)
        CREATE TABLE IF NOT EXISTS ws_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            table_id TEXT,
            msg_type TEXT,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wsraw_table ON ws_raw(table_id);
        CREATE INDEX IF NOT EXISTS idx_wsraw_ts ON ws_raw(ts);
    """)
    conn.commit()
    conn.close()


def save_shoe(
    table_id: str,
    table_name: str,
    table_type: str | None,
    hands: list[dict],
    statistics: str | None = None,
    shoe_summary: Any = None,
    good_roads_map: Any = None,
) -> int | None:
    """完了したシューを保存。hands = gameResult配列。
    Returns shoe DB id or None if invalid.
    """
    if not hands:
        return None

    result_seq = ""
    winners = []
    p_cnt = b_cnt = t_cnt = 0
    game_ids = []
    p_cards_list = []
    b_cards_list = []
    started_at = None
    ended_at = None

    for h in hands:
        winner = h.get("winner", "").upper()
        if "PLAYER" in winner:
            result_seq += "P"
            winners.append("P")
            p_cnt += 1
        elif "BANKER" in winner:
            result_seq += "B"
            winners.append("B")
            b_cnt += 1
        elif "TIE" in winner:
            result_seq += "T"
            winners.append("T")
            t_cnt += 1
        else:
            continue  # 未知のwinnerはスキップ
        game_ids.append(h.get("gameId", ""))
        p_cards_list.append(h.get("playerCards", []))
        b_cards_list.append(h.get("bankerCards", []))
        t = h.get("time")
        if t and not started_at:
            started_at = t
        if t:
            ended_at = t

    if not ended_at or not result_seq:
        return None

    # Time features (ended_at ベース; Pragmatic は "Apr 15, 2026 10:17:07 PM" 形式)
    try:
        dt = datetime.strptime(ended_at, "%b %d, %Y %I:%M:%S %p")
    except Exception:
        dt = datetime.now()

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO shoes (
                table_id, table_name, table_type,
                started_at, ended_at,
                day_of_week, day_of_month, hour_of_day, month, year,
                is_weekend, is_month_end,
                hand_count, player_count, banker_count, tie_count,
                result_sequence, winners, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            table_id, table_name, table_type,
            started_at, ended_at,
            dt.weekday(), dt.day, dt.hour, dt.month, dt.year,
            1 if dt.weekday() >= 5 else 0,
            1 if dt.day >= 26 else 0,
            len(result_seq), p_cnt, b_cnt, t_cnt,
            result_seq, ",".join(winners),
            datetime.now().isoformat(),
        ))
        shoe_id = cur.lastrowid
        cur.execute("""
            INSERT INTO shoes_extra (
                shoe_id, game_ids, player_cards, banker_cards,
                statistics_raw, shoe_summary, good_roads_map
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            shoe_id,
            json.dumps(game_ids),
            json.dumps(p_cards_list),
            json.dumps(b_cards_list),
            statistics,
            json.dumps(shoe_summary) if shoe_summary else None,
            json.dumps(good_roads_map) if good_roads_map else None,
        ))
        conn.commit()
        return shoe_id
    except sqlite3.IntegrityError:
        # UNIQUE制約違反 (重複シュー)
        conn.rollback()
        return None
    finally:
        conn.close()


def log_raw(ts: str, table_id: str | None, msg_type: str | None, payload: str) -> None:
    """生メッセージ保存 (解析用)。1ヶ月後に削除検討。"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO ws_raw (ts, table_id, msg_type, payload) VALUES (?, ?, ?, ?)",
            (ts, table_id, msg_type, payload[:10000]),  # 長すぎるpayloadはtruncate
        )
        conn.commit()
    finally:
        conn.close()


def stats() -> dict:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM shoes")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT table_id) FROM shoes")
        tables = cur.fetchone()[0]
        cur.execute("SELECT MIN(ended_at), MAX(ended_at) FROM shoes")
        first, last = cur.fetchone()
        return {
            "total_shoes": total,
            "distinct_tables": tables,
            "first_shoe": first,
            "last_shoe": last,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    s = stats()
    print(f"DB: {DB_PATH}")
    print(f"Stats: {s}")
