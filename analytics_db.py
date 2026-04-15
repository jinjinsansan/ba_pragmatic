"""Shoe analytics DB — separate from legacy db.py.

Stores shoe-level analytics (regularity, pattern breakdown, flow, time features)
and per-hand sequences for future AI training.
"""
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("baccarat.analytics_db")

JST = timezone(timedelta(hours=9))

DB_PATH = Path(__file__).parent / "analytics.sqlite3"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shoes_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT NOT NULL,
            -- Time features for hypothesis testing (day-of-week, month-end, etc.)
            day_of_week INTEGER NOT NULL,    -- 0=Mon ... 6=Sun
            day_of_month INTEGER NOT NULL,
            hour_of_day INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            is_weekend INTEGER NOT NULL,     -- 1 if Sat/Sun
            is_month_end INTEGER NOT NULL,   -- 1 if day >= 26
            -- Hand counts
            hand_count INTEGER NOT NULL,
            player_count INTEGER NOT NULL,
            banker_count INTEGER NOT NULL,
            tie_count INTEGER NOT NULL,
            -- Sequence (compact: P/B/T chars)
            result_sequence TEXT NOT NULL,
            -- Streak statistics
            max_player_streak INTEGER DEFAULT 0,
            max_banker_streak INTEGER DEFAULT 0,
            -- Regularity analysis
            regularity_label TEXT,           -- "規則性" / "不規則性" / "判定不可"
            regularity_score REAL,
            dominant_pattern TEXT,           -- "テレコ", "ニコニコ・ニコイチ", etc.
            pattern_breakdown TEXT,          -- JSON dict
            flow_changes INTEGER,
            flow_type TEXT,
            big_road_text TEXT,
            -- Metadata
            created_at TEXT NOT NULL,
            UNIQUE(table_id, ended_at)
        );

        CREATE INDEX IF NOT EXISTS idx_shoes_table ON shoes_analytics(table_id);
        CREATE INDEX IF NOT EXISTS idx_shoes_time ON shoes_analytics(ended_at);
        CREATE INDEX IF NOT EXISTS idx_shoes_dow ON shoes_analytics(day_of_week);
        CREATE INDEX IF NOT EXISTS idx_shoes_dom ON shoes_analytics(day_of_month);
        CREATE INDEX IF NOT EXISTS idx_shoes_weekend ON shoes_analytics(is_weekend);
        CREATE INDEX IF NOT EXISTS idx_shoes_month_end ON shoes_analytics(is_month_end);
        CREATE INDEX IF NOT EXISTS idx_shoes_regularity ON shoes_analytics(regularity_score);

        -- Per-hand storage for sequence learning
        CREATE TABLE IF NOT EXISTS hands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shoe_id INTEGER NOT NULL,
            hand_index INTEGER NOT NULL,
            result TEXT NOT NULL,    -- 'player' / 'banker' / 'tie'
            FOREIGN KEY(shoe_id) REFERENCES shoes_analytics(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_hands_shoe ON hands(shoe_id);
    """)
    conn.commit()
    conn.close()
    logger.info(f"analytics DB ready at {DB_PATH}")


def save_shoe(
    table_id: str,
    table_name: str,
    started_at: datetime | None,
    ended_at: datetime,
    results: list[str],
    analysis: dict,
) -> int | None:
    """Save a completed shoe with its analysis. Returns the row id."""
    try:
        sequence = "".join(
            {"player": "P", "banker": "B", "tie": "T"}.get(r, "?") for r in results
        )
        p_count = results.count("player")
        b_count = results.count("banker")
        t_count = results.count("tie")

        # Compute streaks
        max_p = max_b = 0
        cur_p = cur_b = 0
        for r in results:
            if r == "player":
                cur_p += 1
                cur_b = 0
                max_p = max(max_p, cur_p)
            elif r == "banker":
                cur_b += 1
                cur_p = 0
                max_b = max(max_b, cur_b)
            else:
                cur_p = cur_b = 0

        ended_jst = ended_at.astimezone(JST) if ended_at.tzinfo else ended_at.replace(tzinfo=JST)
        dow = ended_jst.weekday()
        dom = ended_jst.day
        hour = ended_jst.hour
        month = ended_jst.month
        year = ended_jst.year
        is_weekend = 1 if dow >= 5 else 0
        is_month_end = 1 if dom >= 26 else 0

        started_str = started_at.isoformat() if started_at else None
        ended_str = ended_jst.isoformat()

        conn = get_conn()
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO shoes_analytics (
                    table_id, table_name, started_at, ended_at,
                    day_of_week, day_of_month, hour_of_day, month, year,
                    is_weekend, is_month_end,
                    hand_count, player_count, banker_count, tie_count,
                    result_sequence, max_player_streak, max_banker_streak,
                    regularity_label, regularity_score, dominant_pattern,
                    pattern_breakdown, flow_changes, flow_type, big_road_text,
                    created_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?
                )
                """,
                (
                    table_id, table_name, started_str, ended_str,
                    dow, dom, hour, month, year,
                    is_weekend, is_month_end,
                    len(results), p_count, b_count, t_count,
                    sequence, max_p, max_b,
                    analysis.get("regularity", ""),
                    analysis.get("regularity_score", 0.0),
                    analysis.get("dominant_pattern", ""),
                    json.dumps(analysis.get("pattern_breakdown", {}), ensure_ascii=False),
                    analysis.get("flow_changes", 0),
                    analysis.get("flow_type", ""),
                    analysis.get("big_road_text", ""),
                    datetime.now(JST).isoformat(),
                ),
            )
            if cur.rowcount == 0:
                conn.close()
                return None
            shoe_row_id = cur.lastrowid

            # Insert individual hands
            conn.executemany(
                "INSERT INTO hands (shoe_id, hand_index, result) VALUES (?, ?, ?)",
                [(shoe_row_id, i, r) for i, r in enumerate(results)],
            )
            conn.commit()
            return shoe_row_id
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"save_shoe failed: {e}", exc_info=True)
        return None


def count_shoes() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) as n FROM shoes_analytics").fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


def count_hands() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) as n FROM hands").fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"DB path: {DB_PATH}")
    print(f"Shoes: {count_shoes()}  Hands: {count_hands()}")
