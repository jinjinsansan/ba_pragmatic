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

        -- Crowd / bettingStats events (table-enter WS only)
        CREATE TABLE IF NOT EXISTS crowd_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            table_id TEXT,
            table_name TEXT,
            game_id TEXT,
            bet_side TEXT,
            bet_amount REAL,
            result TEXT,
            watchers INTEGER,
            bettors INTEGER,
            player_amount REAL,
            player_players INTEGER,
            player_percentage REAL,
            banker_amount REAL,
            banker_players INTEGER,
            banker_percentage REAL,
            tie_amount REAL,
            tie_players INTEGER,
            tie_percentage REAL,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_crowd_time ON crowd_events(captured_at);
        CREATE INDEX IF NOT EXISTS idx_crowd_table ON crowd_events(table_id);
        CREATE INDEX IF NOT EXISTS idx_crowd_game ON crowd_events(game_id);
        CREATE INDEX IF NOT EXISTS idx_crowd_user ON crowd_events(user_id);
    """)
    # ---- lightweight migrations (ADD COLUMN) ----
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(shoes_analytics)").fetchall()]
        if "frontend_app" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN frontend_app TEXT")
        if "lang" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN lang TEXT")
        if "min_bet" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN min_bet REAL")
        if "players_online_avg" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN players_online_avg REAL")
        if "players_online_max" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN players_online_max INTEGER")
        if "players_online_min" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN players_online_min INTEGER")
        if "shoe_duration_sec" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN shoe_duration_sec REAL")
        if "ws_silence_max" not in cols:
            conn.execute("ALTER TABLE shoes_analytics ADD COLUMN ws_silence_max REAL")

        hcols = [r[1] for r in conn.execute("PRAGMA table_info(hands)").fetchall()]
        if "observed_at" not in hcols:
            conn.execute("ALTER TABLE hands ADD COLUMN observed_at TEXT")
        if "players_online" not in hcols:
            conn.execute("ALTER TABLE hands ADD COLUMN players_online INTEGER")
        if "ws_silence" not in hcols:
            conn.execute("ALTER TABLE hands ADD COLUMN ws_silence REAL")

        ccols = [r[1] for r in conn.execute("PRAGMA table_info(crowd_events)").fetchall()]
        if "event_type" not in ccols:
            conn.execute("ALTER TABLE crowd_events ADD COLUMN event_type TEXT DEFAULT ''")
        # index depends on event_type; create only after migration
        conn.execute("CREATE INDEX IF NOT EXISTS idx_crowd_type ON crowd_events(event_type)")
    except Exception as e:
        logger.warning(f"analytics DB migration skipped/failed: {e}")
    conn.commit()
    conn.close()
    logger.info(f"analytics DB ready at {DB_PATH}")


def save_crowd_event(
    *,
    user_id: str,
    event_type: str = "bettingStats",
    captured_at: str,
    table_id: str | None = None,
    table_name: str | None = None,
    game_id: str | None = None,
    bet_side: str | None = None,
    bet_amount: float | None = None,
    result: str | None = None,
    crowd: dict | None = None,
) -> None:
    """Save a single bettingStats snapshot (best-effort)."""
    try:
        stats = (crowd or {}).get("stats") if isinstance(crowd, dict) else None
        if not isinstance(stats, dict):
            stats = {}

        def _get(side: str, key: str):
            d = stats.get(side) or {}
            return d.get(key) if isinstance(d, dict) else None

        conn = get_conn()
        try:
            conn.execute(
                """
                INSERT INTO crowd_events (
                    user_id, event_type, captured_at, table_id, table_name, game_id,
                    bet_side, bet_amount, result,
                    watchers, bettors,
                    player_amount, player_players, player_percentage,
                    banker_amount, banker_players, banker_percentage,
                    tie_amount, tie_players, tie_percentage,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?,
                          ?, ?, ?,
                          ?, ?,
                          ?, ?, ?,
                          ?, ?, ?,
                          ?, ?, ?,
                          ?)
                """,
                (
                    user_id,
                    event_type,
                    captured_at,
                    table_id,
                    table_name,
                    game_id,
                    bet_side,
                    bet_amount,
                    result,
                    (crowd or {}).get("watchers") if isinstance(crowd, dict) else None,
                    (crowd or {}).get("bettors") if isinstance(crowd, dict) else None,
                    _get("Player", "amount"),
                    _get("Player", "players"),
                    _get("Player", "percentage"),
                    _get("Banker", "amount"),
                    _get("Banker", "players"),
                    _get("Banker", "percentage"),
                    _get("Tie", "amount"),
                    _get("Tie", "players"),
                    _get("Tie", "percentage"),
                    json.dumps(crowd or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"save_crowd_event failed: {e}")


def save_shoe(
    table_id: str,
    table_name: str,
    started_at: datetime | None,
    ended_at: datetime,
    results: list[str],
    analysis: dict,
    *,
    table_meta: dict | None = None,
    hand_meta: list[dict] | None = None,
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

        # extra meta (optional)
        table_meta = table_meta or {}
        frontend_app = table_meta.get("frontend_app")
        lang = table_meta.get("lang")
        min_bet = table_meta.get("min_bet")

        # per-hand meta aggregation (optional)
        players_vals: list[int] = []
        ws_sil_vals: list[float] = []
        if hand_meta:
            for hm in hand_meta:
                try:
                    po = hm.get("players_online")
                    if isinstance(po, int) and po >= 0:
                        players_vals.append(po)
                except Exception:
                    pass
                try:
                    ws = hm.get("ws_silence")
                    if isinstance(ws, (int, float)) and ws >= 0:
                        ws_sil_vals.append(float(ws))
                except Exception:
                    pass

        players_online_avg = (sum(players_vals) / len(players_vals)) if players_vals else None
        players_online_max = (max(players_vals) if players_vals else None)
        players_online_min = (min(players_vals) if players_vals else None)
        ws_silence_max = (max(ws_sil_vals) if ws_sil_vals else None)

        shoe_duration_sec = None
        if hand_meta:
            try:
                ts = [hm.get("observed_at") for hm in hand_meta if hm.get("observed_at")]
                if ts and len(ts) >= 2:
                    # ISO文字列の辞書順＝時系列順（JST固定で書く前提）
                    ts_sorted = sorted(ts)
                    # parse minimally
                    t0 = datetime.fromisoformat(ts_sorted[0])
                    t1 = datetime.fromisoformat(ts_sorted[-1])
                    shoe_duration_sec = (t1 - t0).total_seconds()
            except Exception:
                shoe_duration_sec = None

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
                    created_at,
                    frontend_app, lang, min_bet,
                    players_online_avg, players_online_max, players_online_min,
                    shoe_duration_sec, ws_silence_max
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?
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
                    frontend_app, lang, min_bet,
                    players_online_avg, players_online_max, players_online_min,
                    shoe_duration_sec, ws_silence_max,
                ),
            )
            if cur.rowcount == 0:
                conn.close()
                return None
            shoe_row_id = cur.lastrowid

            # Insert individual hands
            hm = hand_meta or []
            if hm and len(hm) != len(results):
                logger.warning(f"hand_meta length mismatch: results={len(results)} meta={len(hm)} (pad/trim)")
            rows = []
            for i, r in enumerate(results):
                meta = hm[i] if i < len(hm) else {}
                rows.append(
                    (
                        shoe_row_id,
                        i,
                        r,
                        meta.get("observed_at"),
                        meta.get("players_online"),
                        meta.get("ws_silence"),
                    )
                )
            conn.executemany(
                "INSERT INTO hands (shoe_id, hand_index, result, observed_at, players_online, ws_silence) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
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
