"""SQLiteデータベース管理"""
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import DB_PATH

logger = logging.getLogger("baccarat.db")

JST = timezone(timedelta(hours=9))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """テーブル作成"""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            round_id TEXT,
            result TEXT NOT NULL,          -- 'player', 'banker', 'tie'
            player_pair INTEGER DEFAULT 0, -- 1=ペアあり
            banker_pair INTEGER DEFAULT 0, -- 1=ペアあり
            player_score INTEGER,
            banker_score INTEGER,
            shoe_number TEXT,
            created_at TEXT NOT NULL       -- ISO8601 JST
        );

        CREATE TABLE IF NOT EXISTS shoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            shoe_number INTEGER NOT NULL,
            hand_count INTEGER NOT NULL,
            player_count INTEGER NOT NULL,
            banker_count INTEGER NOT NULL,
            tie_count INTEGER NOT NULL,
            result_sequence TEXT NOT NULL,   -- 例: PPBBPPBBBT
            max_banker_streak INTEGER DEFAULT 0,
            max_player_streak INTEGER DEFAULT 0,
            started_at TEXT,
            ended_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            table_id TEXT NOT NULL,
            shoe_number INTEGER,
            hand_number INTEGER,
            bet_side TEXT NOT NULL,
            bet_amount REAL NOT NULL,
            result TEXT,
            profit REAL DEFAULT 0,
            strategy_name TEXT,
            strategy_reason TEXT,
            regularity_score REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            total_bets INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total_profit REAL DEFAULT 0,
            starting_balance REAL,
            ending_balance REAL
        );

        CREATE INDEX IF NOT EXISTS idx_rounds_table ON rounds(table_name);
        CREATE INDEX IF NOT EXISTS idx_rounds_created ON rounds(created_at);
        CREATE INDEX IF NOT EXISTS idx_rounds_result ON rounds(result);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_unique ON rounds(table_name, round_id);
        CREATE INDEX IF NOT EXISTS idx_shoes_table ON shoes(table_name);
        CREATE INDEX IF NOT EXISTS idx_shoes_created ON shoes(created_at);
        CREATE INDEX IF NOT EXISTS idx_bets_table ON bets(table_name);
        CREATE INDEX IF NOT EXISTS idx_bets_created ON bets(created_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
    """)

    # 分析カラムを追加 (既存DBとの後方互換性)
    analysis_columns = [
        ("regularity", "TEXT DEFAULT ''"),
        ("regularity_score", "REAL DEFAULT 0"),
        ("dominant_pattern", "TEXT DEFAULT ''"),
        ("pattern_breakdown", "TEXT DEFAULT '{}'"),
        ("flow_type", "TEXT DEFAULT ''"),
        ("flow_changes", "INTEGER DEFAULT 0"),
        ("day_of_week", "INTEGER DEFAULT -1"),
        ("hour_of_day", "INTEGER DEFAULT -1"),
        ("day_of_month", "INTEGER DEFAULT -1"),
    ]
    for col_name, col_def in analysis_columns:
        try:
            conn.execute(f"ALTER TABLE shoes ADD COLUMN {col_name} {col_def}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()
    logger.info(f"DB initialized: {DB_PATH}")


def insert_round(
    table_name: str,
    round_id: str,
    result: str,
    player_pair: bool = False,
    banker_pair: bool = False,
    player_score: int | None = None,
    banker_score: int | None = None,
    shoe_number: str = "",
) -> bool:
    """ラウンド結果を保存。重複はスキップ。挿入されたらTrue"""
    now = datetime.now(JST).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO rounds
               (table_name, round_id, result, player_pair, banker_pair,
                player_score, banker_score, shoe_number, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                table_name, round_id, result,
                int(player_pair), int(banker_pair),
                player_score, banker_score,
                shoe_number, now,
            ),
        )
        conn.commit()
        inserted = conn.total_changes > 0
        return inserted
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def insert_shoe(summary: dict) -> bool:
    """シューのサマリー + 分析結果を保存"""
    now = datetime.now(JST).isoformat()
    conn = get_connection()
    try:
        pattern_json = json.dumps(
            summary.get("pattern_breakdown", {}), ensure_ascii=False
        )
        conn.execute(
            """INSERT INTO shoes
               (table_name, shoe_number, hand_count, player_count, banker_count,
                tie_count, result_sequence, max_banker_streak, max_player_streak,
                started_at, ended_at, created_at,
                regularity, regularity_score, dominant_pattern,
                pattern_breakdown, flow_type, flow_changes,
                day_of_week, hour_of_day, day_of_month)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                summary["table_name"],
                summary["shoe_number"],
                summary["hand_count"],
                summary["player_count"],
                summary["banker_count"],
                summary["tie_count"],
                summary["result_sequence"],
                summary["max_banker_streak"],
                summary["max_player_streak"],
                summary.get("started_at", ""),
                summary.get("ended_at", now),
                now,
                summary.get("regularity", ""),
                summary.get("regularity_score", 0),
                summary.get("dominant_pattern", ""),
                pattern_json,
                summary.get("flow_type", ""),
                summary.get("flow_changes", 0),
                summary.get("day_of_week", -1),
                summary.get("hour_of_day", -1),
                summary.get("day_of_month", -1),
            ),
        )
        conn.commit()
        reg = summary.get("regularity", "?")
        score = summary.get("regularity_score", 0)
        logger.info(
            f"シュー #{summary['shoe_number']} 保存: "
            f"{summary['hand_count']}ハンド [{reg}:{score}] "
            f"{summary['result_sequence'][:20]}..."
        )
        return True
    except Exception as e:
        logger.error(f"シュー保存エラー: {e}")
        return False
    finally:
        conn.close()


def get_stats(table_name: str = "", hours: int = 24, table_names: list[str] | None = None) -> dict:
    """統計情報を取得。table_names指定時はそのテーブル群のみ集計"""
    conn = get_connection()
    cutoff = (datetime.now(JST) - timedelta(hours=hours)).isoformat()

    where = "WHERE created_at >= ?"
    params: list = [cutoff]
    if table_names:
        placeholders = ",".join("?" for _ in table_names)
        where += f" AND table_name IN ({placeholders})"
        params.extend(table_names)
    elif table_name:
        where += " AND table_name = ?"
        params.append(table_name)

    row = conn.execute(
        f"SELECT COUNT(*) as total FROM rounds {where}", params
    ).fetchone()
    total = row["total"]

    if total == 0:
        conn.close()
        return {"total": 0, "player": 0, "banker": 0, "tie": 0,
                "player_pct": 0, "banker_pct": 0, "tie_pct": 0,
                "player_pair": 0, "banker_pair": 0}

    results = {}
    for result_type in ("player", "banker", "tie"):
        r = conn.execute(
            f"SELECT COUNT(*) as cnt FROM rounds {where} AND result = ?",
            params + [result_type],
        ).fetchone()
        results[result_type] = r["cnt"]

    pp = conn.execute(
        f"SELECT SUM(player_pair) as cnt FROM rounds {where}", params
    ).fetchone()
    bp = conn.execute(
        f"SELECT SUM(banker_pair) as cnt FROM rounds {where}", params
    ).fetchone()

    conn.close()

    return {
        "total": total,
        "player": results["player"],
        "banker": results["banker"],
        "tie": results["tie"],
        "player_pct": round(results["player"] / total * 100, 1),
        "banker_pct": round(results["banker"] / total * 100, 1),
        "tie_pct": round(results["tie"] / total * 100, 1),
        "player_pair": pp["cnt"] or 0,
        "banker_pair": bp["cnt"] or 0,
    }


def get_recent_results(table_name: str = "", limit: int = 20, table_names: list[str] | None = None) -> list[dict]:
    """直近の結果を取得"""
    conn = get_connection()
    where = ""
    params: list = []
    if table_names:
        placeholders = ",".join("?" for _ in table_names)
        where = f"WHERE table_name IN ({placeholders})"
        params.extend(table_names)
    elif table_name:
        where = "WHERE table_name = ?"
        params.append(table_name)

    rows = conn.execute(
        f"SELECT * FROM rounds {where} ORDER BY id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_streak(table_name: str = "", table_names: list[str] | None = None) -> dict:
    """現在の連勝・連続記録"""
    results = get_recent_results(table_name, limit=100, table_names=table_names)
    if not results:
        return {"current": "", "count": 0}

    current = results[0]["result"]
    count = 0
    for r in results:
        if r["result"] == current:
            count += 1
        else:
            break

    return {"current": current, "count": count}


# === BET関連DB操作 ===

def insert_bet(
    table_name: str,
    table_id: str,
    shoe_number: int,
    hand_number: int,
    bet_side: str,
    bet_amount: float,
    result: str = "",
    profit: float = 0.0,
    strategy_name: str = "",
    strategy_reason: str = "",
    regularity_score: float = 0.0,
) -> int | None:
    """BET記録を保存。IDを返す"""
    now = datetime.now(JST).isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO bets
               (table_name, table_id, shoe_number, hand_number,
                bet_side, bet_amount, result, profit,
                strategy_name, strategy_reason, regularity_score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                table_name, table_id, shoe_number, hand_number,
                bet_side, bet_amount, result, profit,
                strategy_name, strategy_reason, regularity_score, now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error(f"BET保存エラー: {e}")
        return None
    finally:
        conn.close()


def update_bet_result(bet_id: int, result: str, profit: float):
    """BET結果を更新"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE bets SET result = ?, profit = ? WHERE id = ?",
            (result, profit, bet_id),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"BET更新エラー: {e}")
    finally:
        conn.close()


def start_session(starting_balance: float = 0.0) -> int | None:
    """セッション開始。IDを返す"""
    now = datetime.now(JST).isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, starting_balance) VALUES (?, ?)",
            (now, starting_balance),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error(f"セッション開始エラー: {e}")
        return None
    finally:
        conn.close()


def end_session(
    session_id: int,
    total_bets: int,
    wins: int,
    losses: int,
    total_profit: float,
    ending_balance: float = 0.0,
):
    """セッション終了"""
    now = datetime.now(JST).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE sessions
               SET ended_at = ?, total_bets = ?, wins = ?, losses = ?,
                   total_profit = ?, ending_balance = ?
               WHERE id = ?""",
            (now, total_bets, wins, losses, total_profit, ending_balance, session_id),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"セッション終了エラー: {e}")
    finally:
        conn.close()


def get_session_stats(session_id: int) -> dict:
    """セッションの統計を取得"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row:
            return dict(row)
        return {}
    finally:
        conn.close()


def get_bet_stats(hours: int = 24) -> dict:
    """BET統計を取得"""
    conn = get_connection()
    cutoff = (datetime.now(JST) - timedelta(hours=hours)).isoformat()
    try:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM bets WHERE created_at >= ?", (cutoff,)
        ).fetchone()["cnt"]

        if total == 0:
            return {"total": 0, "wins": 0, "losses": 0, "ties": 0,
                    "win_rate": 0, "total_profit": 0}

        wins = conn.execute(
            "SELECT COUNT(*) as cnt FROM bets WHERE created_at >= ? AND result = 'win'",
            (cutoff,),
        ).fetchone()["cnt"]
        losses = conn.execute(
            "SELECT COUNT(*) as cnt FROM bets WHERE created_at >= ? AND result = 'lose'",
            (cutoff,),
        ).fetchone()["cnt"]
        ties = conn.execute(
            "SELECT COUNT(*) as cnt FROM bets WHERE created_at >= ? AND result = 'tie_push'",
            (cutoff,),
        ).fetchone()["cnt"]
        profit = conn.execute(
            "SELECT COALESCE(SUM(profit), 0) as total FROM bets WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()["total"]

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
            "total_profit": round(profit, 2),
        }
    finally:
        conn.close()
