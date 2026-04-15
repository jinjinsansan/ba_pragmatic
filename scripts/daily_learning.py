"""LAPLACE 日次学習バッチ

毎日 JST 0:05 に VPS で cron 実行。
過去24時間の analytics.sqlite3 を分析し、環境指標を計算。
Supabase に記録 + 異常検知時に Telegram アラート。

Usage:
  python daily_learning.py                    # 本日分を計算
  python daily_learning.py --date 2026-04-13  # 特定日を計算

Cron:
  5 15 * * * cd /opt/laplace && venv/bin/python daily_learning.py
"""
import sqlite3
import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from pattern_classifier import classify_pattern

DB_PATH = os.environ.get("ANALYTICS_DB", "analytics.sqlite3")
JST = timezone(timedelta(hours=9))

# Supabase
SITE_URL = os.environ.get("LAPLACE_SITE_URL", "https://www.bafather.uk").rstrip("/")
API_KEY = os.environ.get("LAPLACE_API_KEY", "")

# Telegram alert
ALERT_BOT_TOKEN = os.environ.get("ADMIN_TELEGRAM_BOT_TOKEN", "")
ALERT_CHAT_ID = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

# 閾値
WARN_AVG_DURATION = 10.0   # 平均寿命がこれ以下で警戒
WARN_WIN_RATE = 51.0       # 勝率がこれ以下で警戒
DANGER_AVG_DURATION = 7.0  # 危険
DANGER_WIN_RATE = 50.0     # 危険
CONSECUTIVE_WARN_DAYS = 3  # N日連続で警戒→アラート

TEREKO_WINDOW = 10
TEREKO_THRESH = 0.80
MIN_HANDS = 50
STATIC_WARMUP = 30


def get_target_date():
    for i, a in enumerate(sys.argv):
        if a == "--date" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    # cronは00:05JSTに実行される想定。昨日分の完全24時間データを処理する。
    return (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")


# ============================================================
# データ駆動型パラメータ最適化 (加重中央値アプローチ)
# ============================================================
# 設計:
#   全履歴 / 月 / 週 / 同曜日 の各データスライスで backtest を走らせ、
#   各スライスで最も PNL が高かった ENTRY_THRESHOLD を求める。
#   データ量と最新度を加味した加重中央値で最終 T を決定する。
#   → 0.85 固定やハードコード閾値に依存せず、常にデータから最適値を抽出。

THRESHOLD_GRID = [0.80, 0.82, 0.84, 0.85, 0.87, 0.88, 0.90, 0.92, 0.94, 0.95]

# スライス名: (発見した最適Tの重み)
# 全履歴が最も信頼できるので最大重み。短期は重みを抑える。
SLICE_WEIGHTS = {
    "all": 0.40,      # 全履歴 (23K+シュー)
    "month": 0.25,    # 直近30日
    "week": 0.20,     # 直近7日
    "same_dow": 0.15, # 同じ曜日の履歴
}


def _load_shoes_range(db_path: str, start_date: str, end_date: str) -> list:
    """[start_date, end_date) の期間のシューを読み込む。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? AND started_at < ? ORDER BY started_at",
        (MIN_HANDS, start_date, end_date)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def _load_shoes_by_dow(db_path: str, target_date: str, weekday: int) -> list:
    """target_date以前の履歴から、指定曜日 (0=月〜6=日) のシューのみ返す。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at < ? ORDER BY started_at",
        (MIN_HANDS, target_date)
    )
    rows = []
    for seq, ts in cur.fetchall():
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
            if d.weekday() == weekday:
                rows.append((seq, ts))
        except Exception:
            continue
    conn.close()
    return rows


def _compute_columns(seq: str) -> list:
    """seq (PBT文字列) から P/B の列長リストを返す。Tは無視。"""
    cols = []
    cur_len = 0
    cur_side = None
    for ch in seq:
        if ch not in ('P', 'B'):
            continue
        if ch == cur_side:
            cur_len += 1
        else:
            if cur_side is not None:
                cols.append(cur_len)
            cur_side = ch
            cur_len = 1
    if cur_len > 0:
        cols.append(cur_len)
    return cols


# パターンDB を読み込むキャッシュ (プロセス初回呼出しで Supabase からロード)
_PATTERN_CACHE = None


def _load_pattern_cache():
    """Supabase の pattern_winrates を dict {"1-1-2-..."→wr} 形式で返す。"""
    global _PATTERN_CACHE
    if _PATTERN_CACHE is not None:
        return _PATTERN_CACHE
    cache = {}
    if not API_KEY:
        _PATTERN_CACHE = cache
        return cache
    try:
        url = f"{SITE_URL}/api/pattern-winrates?api_key={API_KEY}&limit=500"
        req = urllib.request.Request(url, headers={"User-Agent": "LAPLACE-learning/1.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8")).get("patterns", [])
        for p in data:
            h = p.get("pattern_hash")
            if h:
                cache[h] = float(p.get("win_rate", 50.0))
    except Exception as e:
        print(f"  [pattern-cache] load failed: {e}")
    _PATTERN_CACHE = cache
    return cache


def _count_effective_bets(seq: str,
                           exit_drop3_limit: int = 2,
                           exit_drop5: bool = True) -> int:
    """エントリー後、退出条件に達するまでの BET 数 (Tie除外) を返す。"""
    n = 0
    last_non_tie = None
    cur_side = None
    cur_len = 0
    columns_since_entry = []
    for ch in seq:
        if ch not in ('P', 'B', 'T'):
            continue
        if ch == 'T':
            continue
        if last_non_tie is None:
            last_non_tie = ch
            cur_side = ch
            cur_len = 1
            continue
        n += 1
        if ch == cur_side:
            cur_len += 1
        else:
            columns_since_entry.append(cur_len)
            cur_side = ch
            cur_len = 1
        if exit_drop5 and (cur_len >= 5 or any(c >= 5 for c in columns_since_entry)):
            break
        drop3_count = sum(1 for c in columns_since_entry if c >= 3) + (1 if cur_len >= 3 else 0)
        if drop3_count >= exit_drop3_limit:
            break
        last_non_tie = ch
    return n


def _simulate_counter(shoes: list, threshold: float,
                       entry_window: int = 15,
                       exit_drop3_limit: int = 2,
                       exit_drop5: bool = True,
                       fallback_wr: float = 53.5) -> float:
    """各シューの 10列パターンから pattern_winrates DB を引いて期待値を積算。

    Flat BET 近似ではなく、実データの履歴勝率を使う。
    パターンが DB に無ければ fallback_wr (全体平均) を使う。
    """
    BANKER_COMM_HALF = 0.025  # 逆張りは P/B 交互にBETされるので平均 2.5%
    patterns = _load_pattern_cache()
    total = 0.0
    for seq, _ts in shoes:
        pb_only = [c for c in seq if c in ('P', 'B')]
        if len(pb_only) < MIN_HANDS:
            continue
        cols = _compute_columns(seq)
        if len(cols) < entry_window:
            continue
        # エントリー判定
        last_cols = cols[-entry_window:]
        short_ratio = sum(1 for c in last_cols if c <= 2) / len(last_cols)
        if short_ratio < threshold:
            continue
        # シューの 10列パターンハッシュを生成
        if len(cols) < 10:
            wr_pct = fallback_wr
        else:
            p_key = '-'.join(str(x) for x in cols[-10:])
            wr_pct = patterns.get(p_key, fallback_wr)
        wr = wr_pct / 100.0
        # エントリー後の有効BET数 (退出条件まで)
        n_bets = _count_effective_bets(seq, exit_drop3_limit, exit_drop5)
        if n_bets <= 0:
            continue
        # 期待値 (銀行手数料半分平均)
        avg_win = 1.0 * (1.0 - BANKER_COMM_HALF)
        expected_per_bet = wr * avg_win - (1 - wr) * 1.0
        total += expected_per_bet * n_bets
    return total


def _optimal_threshold_for_slice(shoes: list, label: str = "") -> tuple:
    """シューリストに対し THRESHOLD_GRID で grid search → (best_T, best_PNL, n_shoes) を返す。"""
    if not shoes or len(shoes) < 50:
        return (None, None, len(shoes) if shoes else 0)
    best_t = THRESHOLD_GRID[0]
    best_pnl = float("-inf")
    for t in THRESHOLD_GRID:
        pnl = _simulate_counter(shoes, t)
        if pnl > best_pnl:
            best_pnl = pnl
            best_t = t
    return (best_t, best_pnl, len(shoes))


def _weighted_median(values: list, weights: list) -> float:
    """加重中央値。累計重み 0.5 を超える値を返す。"""
    if not values:
        return 0.85
    pairs = sorted(zip(values, weights))
    total_w = sum(weights)
    cum = 0.0
    for v, w in pairs:
        cum += w
        if cum >= total_w / 2:
            return v
    return pairs[-1][0]


def compute_optimal_threshold_data_driven(target_date: str) -> dict:
    """target_date を基準に、全履歴/月/週/同曜日の各データスライスで最適Tを求め、
    加重中央値で最終 T を返す。ログ用の詳細 dict を返す。

    例:
      target_date = 2026-04-14 → この日を含まない過去を参照
      返値: {
          "T_all": 0.85,   "n_all": 23437,   "pnl_all": 12345.6,
          "T_month": 0.87, "n_month": 3500,
          "T_week":  0.85, "n_week": 800,
          "T_dow":   0.85, "n_dow": 3300,
          "final_T": 0.85
      }
    """
    db_path = DB_PATH
    result = {}

    # 1. 全履歴 (target_date 未満)
    shoes_all = _load_shoes_range(db_path, "1970-01-01", target_date)
    t_all, pnl_all, n_all = _optimal_threshold_for_slice(shoes_all, "all")
    result.update({"T_all": t_all, "n_all": n_all, "pnl_all": pnl_all})

    # 2. 月次 (target_date の 30日前〜target_date 未満)
    dt_target = datetime.strptime(target_date, "%Y-%m-%d")
    start_month = (dt_target - timedelta(days=30)).strftime("%Y-%m-%d")
    shoes_month = _load_shoes_range(db_path, start_month, target_date)
    t_month, pnl_month, n_month = _optimal_threshold_for_slice(shoes_month, "month")
    result.update({"T_month": t_month, "n_month": n_month, "pnl_month": pnl_month})

    # 3. 週次 (直近7日)
    start_week = (dt_target - timedelta(days=7)).strftime("%Y-%m-%d")
    shoes_week = _load_shoes_range(db_path, start_week, target_date)
    t_week, pnl_week, n_week = _optimal_threshold_for_slice(shoes_week, "week")
    result.update({"T_week": t_week, "n_week": n_week, "pnl_week": pnl_week})

    # 4. 同曜日履歴 (target_date と同曜日のみ、過去全て)
    target_weekday = dt_target.weekday()  # 0=月, 6=日
    shoes_dow = _load_shoes_by_dow(db_path, target_date, target_weekday)
    t_dow, pnl_dow, n_dow = _optimal_threshold_for_slice(shoes_dow, "same_dow")
    result.update({"T_dow": t_dow, "n_dow": n_dow, "pnl_dow": pnl_dow})

    # 加重中央値で最終 T を決定 (Noneは除外)
    values, weights = [], []
    for key, w in SLICE_WEIGHTS.items():
        t_key = {"all": "T_all", "month": "T_month", "week": "T_week", "same_dow": "T_dow"}[key]
        t = result.get(t_key)
        if t is None:
            continue
        values.append(t)
        weights.append(w)
    if not values:
        final_t = 0.85  # 全スライス空はあり得ないが fallback
    else:
        final_t = _weighted_median(values, weights)
    result["final_T"] = round(final_t, 2)
    result["weekday"] = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][target_weekday]
    return result


def load_shoes(date_str):
    """指定日のシューを読み込み"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    next_date = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? AND started_at < ? ORDER BY started_at",
        (MIN_HANDS, date_str, next_date)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def load_recent_metrics(days=7):
    """直近N日分のメトリクスを Supabase から取得 (閾値チェック用)"""
    if not API_KEY:
        return []
    try:
        url = f"{SITE_URL}/api/daily-metrics?days={days}&api_key={API_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "LAPLACE-learning/1.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read().decode("utf-8")).get("metrics", [])
    except Exception:
        return []


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def compute_columns(seq):
    cols = []
    cur = 0
    last = None
    for ch in seq:
        if ch == 'T':
            continue
        if ch == last:
            cur += 1
        else:
            if last is not None:
                cols.append(cur)
            cur = 1
            last = ch
    if cur > 0:
        cols.append(cur)
    return cols


def analyze(shoes):
    """日次メトリクスを計算"""
    total_shoes = len(shoes)
    tereko_shoes = 0
    total_hands = 0
    counter_wins = 0
    counter_losses = 0
    tereko_durations = []
    hourly_wr = defaultdict(lambda: {'w': 0, 'l': 0})

    # パターン別勝率 (Top 100)
    pattern_stats = defaultdict(lambda: {'w': 0, 'l': 0})

    for table_name, seq, started_at in shoes:
        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            continue

        # JST hour
        try:
            hour = int(started_at.split('T')[1].split(':')[0]) if 'T' in started_at else int(started_at.split(' ')[1].split(':')[0])
        except Exception:
            hour = -1

        # テレコ判定
        warmup = clean[:STATIC_WARMUP]
        pattern = classify_pattern(warmup, min_cols=3)
        is_tereko = (pattern == "テレコ+ニコ混合")
        if is_tereko:
            tereko_shoes += 1

        # テレコ寿命
        cols = compute_columns(clean)
        if len(cols) >= TEREKO_WINDOW:
            in_t = False
            t_start = 0
            for ci in range(TEREKO_WINDOW, len(cols)):
                recent = cols[ci - TEREKO_WINDOW:ci]
                short = sum(1 for L in recent if L <= 2)
                is_t = (short / len(recent)) >= TEREKO_THRESH
                if is_t and not in_t:
                    in_t = True
                    t_start = ci
                elif not is_t and in_t:
                    tereko_durations.append(ci - t_start)
                    in_t = False
            if in_t:
                tereko_durations.append(len(cols) - t_start)

        # 逆張り勝率 (テレコシューのみ)
        if is_tereko:
            last_nt = None
            for ch in seq:
                if ch not in ('P', 'B'):
                    continue
                if last_nt is not None:
                    bet_side = 'P' if last_nt == 'B' else 'B'
                    won = (ch == bet_side)
                    total_hands += 1
                    if won:
                        counter_wins += 1
                    else:
                        counter_losses += 1
                    if hour >= 0:
                        if won:
                            hourly_wr[hour]['w'] += 1
                        else:
                            hourly_wr[hour]['l'] += 1

                    # パターン勝率 (10列窓)
                    if len(cols) >= 10:
                        p_key = tuple(cols[-10:])
                        if won:
                            pattern_stats[p_key]['w'] += 1
                        else:
                            pattern_stats[p_key]['l'] += 1

                last_nt = ch

    # 集計
    wr = counter_wins / total_hands * 100 if total_hands > 0 else 0
    tereko_rate = tereko_shoes / total_shoes * 100 if total_shoes > 0 else 0
    avg_duration = sum(tereko_durations) / len(tereko_durations) if tereko_durations else 0
    short5h_rate = sum(1 for d in tereko_durations if d <= 5) / len(tereko_durations) * 100 if tereko_durations else 0

    # 列長分布 (全シュー)
    all_col_lens = []
    for table_name, seq, started_at in shoes:
        all_col_lens.extend(compute_columns(strip_ties(seq)))
    total_cols = len(all_col_lens) if all_col_lens else 1
    drop3_plus_rate = sum(1 for L in all_col_lens if L >= 3) / total_cols * 100
    avg_col_len = sum(all_col_lens) / total_cols if all_col_lens else 1.0

    # 時間帯別ベスト/ワースト + 分散
    best_hour = -1
    worst_hour = -1
    best_wr = 0
    worst_wr = 100
    hourly_wrs = []
    for h in range(24):
        d = hourly_wr[h]
        t = d['w'] + d['l']
        if t < 50:
            continue
        hr = d['w'] / t * 100
        hourly_wrs.append(hr)
        if hr > best_wr:
            best_wr = hr
            best_hour = h
        if hr < worst_wr:
            worst_wr = hr
            worst_hour = h
    hourly_spread = max(hourly_wrs) - min(hourly_wrs) if len(hourly_wrs) >= 2 else 0

    # パターン Top 100 + Top平均勝率
    pattern_top = []
    for p_key, d in pattern_stats.items():
        t = d['w'] + d['l']
        if t < 30:
            continue
        pattern_top.append({
            'pattern': '-'.join(str(x) for x in p_key),
            'win_rate': d['w'] / t * 100,
            'samples': t,
        })
    pattern_top.sort(key=lambda x: -x['win_rate'])
    pattern_top = pattern_top[:100]
    top10_avg_wr = sum(p['win_rate'] for p in pattern_top[:10]) / min(10, len(pattern_top)) if pattern_top else 0

    return {
        'total_shoes': total_shoes,
        'tereko_shoes': tereko_shoes,
        'tereko_rate': round(tereko_rate, 2),
        'total_hands': total_hands,
        'counter_wr': round(wr, 2),
        'avg_duration': round(avg_duration, 1),
        'short5h_rate': round(short5h_rate, 1),
        'drop3_plus_rate': round(drop3_plus_rate, 2),
        'avg_col_len': round(avg_col_len, 2),
        'hourly_spread': round(hourly_spread, 2),
        'top10_avg_wr': round(top10_avg_wr, 2),
        'best_hour': best_hour,
        'worst_hour': worst_hour,
        'best_wr': round(best_wr, 2),
        'worst_wr': round(worst_wr, 2),
        'pattern_top': pattern_top,
    }


def post_to_supabase(date_str, metrics):
    """Supabase に日次メトリクスを送信"""
    if not API_KEY:
        print("  [skip] No API_KEY — Supabase upload skipped")
        return False
    payload = json.dumps({
        "api_key": API_KEY,
        "date": date_str,
        "metrics": metrics,
    }).encode("utf-8")
    url = f"{SITE_URL}/api/daily-metrics"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-learning/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("  [ok] Supabase upload success")
            return True
    except Exception as e:
        print(f"  [err] Supabase upload failed: {e}")
        return False


def post_pattern_winrates(pattern_top):
    """パターン勝率 Top100 を Supabase に送信"""
    if not API_KEY or not pattern_top:
        return
    url = f"{SITE_URL}/api/pattern-winrates"
    batch_size = 25
    total = len(pattern_top)
    for start in range(0, total, batch_size):
        batch = pattern_top[start:start + batch_size]
        payload = json.dumps({
            "api_key": API_KEY,
            "patterns": batch,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-learning/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                print(f"  [ok] Pattern winrates uploaded ({min(start + batch_size, total)}/{total})")
        except Exception as e:
            print(f"  [err] Pattern winrates upload failed ({start + 1}-{start + len(batch)}): {e}")
            return


def send_telegram_alert(message):
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        print(f"  [alert] {message}")
        return
    payload = json.dumps({
        "chat_id": ALERT_CHAT_ID,
        "text": message,
    }).encode("utf-8")
    url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("  [ok] Telegram alert sent")
    except Exception as e:
        print(f"  [err] Telegram alert failed: {e}")


def check_thresholds(metrics, recent_metrics):
    """閾値チェック → アラート"""
    alerts = []

    wr = metrics['counter_wr']
    dur = metrics['avg_duration']

    # 単日チェック
    if dur <= DANGER_AVG_DURATION:
        alerts.append(f"DANGER: Avg duration {dur}h (< {DANGER_AVG_DURATION}h)")
    elif dur <= WARN_AVG_DURATION:
        alerts.append(f"WARNING: Avg duration {dur}h (< {WARN_AVG_DURATION}h)")

    if wr <= DANGER_WIN_RATE:
        alerts.append(f"DANGER: Win rate {wr}% (< {DANGER_WIN_RATE}%)")
    elif wr <= WARN_WIN_RATE:
        alerts.append(f"WARNING: Win rate {wr}% (< {WARN_WIN_RATE}%)")

    # 連続日チェック
    if recent_metrics and len(recent_metrics) >= CONSECUTIVE_WARN_DAYS:
        recent_wrs = [m.get('counter_wr', 55) for m in recent_metrics[-CONSECUTIVE_WARN_DAYS:]]
        if all(w <= WARN_WIN_RATE for w in recent_wrs):
            alerts.append(f"CRITICAL: Win rate below {WARN_WIN_RATE}% for {CONSECUTIVE_WARN_DAYS} consecutive days: {recent_wrs}")

        recent_durs = [m.get('avg_duration', 20) for m in recent_metrics[-CONSECUTIVE_WARN_DAYS:]]
        if all(d <= WARN_AVG_DURATION for d in recent_durs):
            alerts.append(f"CRITICAL: Avg duration below {WARN_AVG_DURATION}h for {CONSECUTIVE_WARN_DAYS} consecutive days: {recent_durs}")

    return alerts


def main():
    date_str = get_target_date()
    print(f"LAPLACE Daily Learning — {date_str}")
    print(f"DB: {DB_PATH}")

    shoes = load_shoes(date_str)
    print(f"Loaded {len(shoes)} shoes for {date_str}")

    if not shoes:
        print("No data — skipping")
        return

    metrics = analyze(shoes)
    pattern_top = metrics.pop('pattern_top', [])

    print(f"\n=== Daily Metrics ===")
    print(f"  Shoes: {metrics['total_shoes']} (tereko: {metrics['tereko_shoes']}, {metrics['tereko_rate']}%)")
    print(f"  Counter WR: {metrics['counter_wr']}% ({metrics['total_hands']} hands)")
    print(f"  Avg Duration: {metrics['avg_duration']}h")
    print(f"  Short5h Rate: {metrics['short5h_rate']}%")
    print(f"  Best Hour: {metrics['best_hour']:02d}:00 ({metrics['best_wr']}%)")
    print(f"  Worst Hour: {metrics['worst_hour']:02d}:00 ({metrics['worst_wr']}%)")
    print(f"  Drop3+ Rate: {metrics['drop3_plus_rate']}%")
    print(f"  Avg Col Len: {metrics['avg_col_len']}")
    print(f"  Hourly Spread: {metrics['hourly_spread']}%")
    print(f"  Top10 Avg WR: {metrics['top10_avg_wr']}%")
    print(f"  Pattern Top: {len(pattern_top)} patterns")

    # Supabase に送信
    post_to_supabase(date_str, metrics)
    post_pattern_winrates(pattern_top)

    # 閾値チェック
    recent = load_recent_metrics(days=CONSECUTIVE_WARN_DAYS + 1)
    alerts = check_thresholds(metrics, recent)

    if alerts:
        msg = f"⚠ LAPLACE ENVIRONMENT ALERT — {date_str}\n\n"
        msg += "\n".join(alerts)
        msg += f"\n\nMetrics: WR={metrics['counter_wr']}% Dur={metrics['avg_duration']}h Tereko={metrics['tereko_rate']}%"
        send_telegram_alert(msg)
    else:
        print("\n  [ok] All thresholds normal")

    # パラメータ自動調整
    param_changes = auto_adjust_params(metrics, recent)

    # 日次サマリー通知 (常時)
    param_note = ""
    if param_changes:
        param_note = "\n" + " | ".join(param_changes)
    summary = (
        f"📊 LAPLACE Daily — {date_str}\n"
        f"WR: {metrics['counter_wr']}% | Dur: {metrics['avg_duration']}h | Tereko: {metrics['tereko_rate']}%\n"
        f"Best: {metrics['best_hour']:02d}:00 ({metrics['best_wr']}%) | Worst: {metrics['worst_hour']:02d}:00 ({metrics['worst_wr']}%)"
        f"{param_note}"
    )
    send_telegram_alert(summary)


# ============================================================
# パラメータ自動調整
# ============================================================

# デフォルト値
DEFAULT_PARAMS = {
    "entry_window": 15,
    "entry_threshold": 0.85,
    "exit_drop3_limit": 2,
    "exit_drop5_immediate": True,
    "profit_target": 30,
}

# 調整範囲
PARAM_LIMITS = {
    "entry_window": (8, 20),
    "entry_threshold": (0.70, 0.95),
    "exit_drop3_limit": (1, 4),
}


def load_current_params() -> dict:
    """Supabase から現在のパラメータを取得"""
    if not API_KEY:
        return dict(DEFAULT_PARAMS)
    try:
        url = f"{SITE_URL}/api/optimal-params?api_key={API_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "LAPLACE-learning/1.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
            params = data.get("params", {})
            if not params or params.get("status") == "default":
                return dict(DEFAULT_PARAMS)
            return params
    except Exception:
        return dict(DEFAULT_PARAMS)


def save_params(params: dict, reason: str):
    """Supabase にパラメータを保存"""
    if not API_KEY:
        print(f"  [skip] No API_KEY — param save skipped ({reason})")
        return
    payload = json.dumps({
        "api_key": API_KEY,
        "entry_window": params["entry_window"],
        "entry_threshold": params["entry_threshold"],
        "exit_drop3_limit": params["exit_drop3_limit"],
        "exit_drop5_immediate": params.get("exit_drop5_immediate", True),
        "profit_target": params.get("profit_target", 30),
        "status": "active",
        "reason": reason,
    }).encode("utf-8")
    url = f"{SITE_URL}/api/optimal-params"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-learning/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print(f"  [ok] Params saved: {reason}")
    except Exception as e:
        print(f"  [err] Param save failed: {e}")


def auto_adjust_params(today_metrics: dict, recent_metrics: list) -> list[str]:
    """全指標のトレンドからパラメータを自動調整

    7つの指標:
      1. テレコ平均寿命 (avg_duration)
      2. 逆張り勝率 (counter_wr)
      3. テレコ出現率 (tereko_rate)
      4. 短命区間率 (short5h_rate)
      5. 時間帯別勝率の分散 (hourly_spread)
      6. パターンTop10平均勝率 (top10_avg_wr)
      7. 3落ち以上の出現率 (drop3_plus_rate)
    """
    changes = []
    current = load_current_params()
    new_params = dict(current)
    adjusted = False

    if not recent_metrics or len(recent_metrics) < 3:
        print("  [params] Not enough history (< 3 days) — skip adjustment")
        return changes

    recent_3 = recent_metrics[-3:] if len(recent_metrics) >= 3 else recent_metrics
    n = len(recent_3)

    # 3日平均を計算
    avg_wr = sum(m.get('counter_wr', 53) for m in recent_3) / n
    avg_dur = sum(m.get('avg_duration', 15) for m in recent_3) / n
    avg_tereko_rate = sum(m.get('tereko_rate', 40) for m in recent_3) / n
    avg_short5h = sum(m.get('short5h_rate', 30) for m in recent_3) / n
    avg_spread = sum(m.get('hourly_spread', 3) for m in recent_3) / n
    avg_top10 = sum(m.get('top10_avg_wr', 55) for m in recent_3) / n
    avg_drop3 = sum(m.get('drop3_plus_rate', 20) for m in recent_3) / n

    ew_min, ew_max = PARAM_LIMITS["entry_window"]
    et_min, et_max = PARAM_LIMITS["entry_threshold"]
    d3_min, d3_max = PARAM_LIMITS["exit_drop3_limit"]

    cur_et = current.get("entry_threshold", 0.85)
    cur_ew = current.get("entry_window", 15)
    cur_d3 = current.get("exit_drop3_limit", 2)
    cur_d5 = current.get("exit_drop5_immediate", True)

    # ============================================================
    # 入室条件 ENTRY_THRESHOLD (データ駆動型 grid search + 加重中央値)
    # ============================================================
    # 旧ロジック (et_pressure によるヒューリスティック) は廃止。
    # 全履歴 / 月 / 週 / 同曜日 の各スライスで backtest → 最適T → 加重中央値。
    try:
        td = today_metrics.get("date") or get_target_date()
        ensemble = compute_optimal_threshold_data_driven(td)
        new_et = float(ensemble["final_T"])
        # PARAM_LIMITS の範囲内に収める (安全装置)
        new_et = max(et_min, min(new_et, et_max))
        new_et = round(new_et, 2)
        # ログ: 各スライスの結果を全部出す
        print(
            f"  [params] Data-driven T search ({ensemble['weekday']}):\n"
            f"           all:     T={ensemble['T_all']}    n={ensemble['n_all']}      PNL={ensemble.get('pnl_all', 0):+.0f}\n"
            f"           month:   T={ensemble['T_month']}  n={ensemble['n_month']}    PNL={ensemble.get('pnl_month', 0):+.0f}\n"
            f"           week:    T={ensemble['T_week']}   n={ensemble['n_week']}     PNL={ensemble.get('pnl_week', 0):+.0f}\n"
            f"           dow:     T={ensemble['T_dow']}    n={ensemble['n_dow']}      PNL={ensemble.get('pnl_dow', 0):+.0f}\n"
            f"           → weighted_median = {new_et}"
        )
        if new_et != cur_et:
            new_params["entry_threshold"] = new_et
            changes.append(
                f"ET {cur_et}→{new_et} "
                f"(ensemble: all={ensemble['T_all']} mon={ensemble['T_month']} "
                f"wk={ensemble['T_week']} dow={ensemble['T_dow']})"
            )
            adjusted = True
    except Exception as e:
        print(f"  [params] Data-driven T search failed: {e} — keeping current {cur_et}")

    # ============================================================
    # 入室ウィンドウ ENTRY_WINDOW (何列で判定するか)
    # ============================================================
    ew_pressure = 0

    # 指標2: 勝率
    if avg_wr < 51:
        ew_pressure += 2  # 勝率低い→慎重に (長い窓)
    elif avg_wr < 52:
        ew_pressure += 1
    elif avg_wr > 55:
        ew_pressure -= 1  # 勝率高い→素早く入る (短い窓)
    elif avg_wr > 57:
        ew_pressure -= 2

    # 指標5: 時間帯別勝率の分散
    if avg_spread > 6:
        ew_pressure += 1  # 時間帯差が大きい→不安定→慎重に
    elif avg_spread < 2:
        ew_pressure -= 1  # 安定→攻めてOK

    # 指標7: 3落ち以上の出現率
    if avg_drop3 > 30:
        ew_pressure += 1  # 長い列が多い→環境悪化→慎重に
    elif avg_drop3 < 15:
        ew_pressure -= 1  # 短い列ばかり→攻めてOK

    if ew_pressure >= 2:
        new_ew = min(cur_ew + 2, ew_max)
    elif ew_pressure == 1:
        new_ew = min(cur_ew + 1, ew_max)
    elif ew_pressure <= -2:
        new_ew = max(cur_ew - 2, ew_min)
    elif ew_pressure == -1:
        new_ew = max(cur_ew - 1, ew_min)
    else:
        new_ew = cur_ew
    if new_ew != cur_ew:
        new_params["entry_window"] = new_ew
        changes.append(f"EW {cur_ew}→{new_ew} (p={ew_pressure})")
        adjusted = True

    # ============================================================
    # 退室条件 EXIT_DROP3_LIMIT (3落ち何回で退室か)
    # ============================================================
    d3_pressure = 0

    # 指標1: テレコ寿命
    if avg_dur < 7:
        d3_pressure -= 2  # 寿命短い→早く逃げる
    elif avg_dur < 10:
        d3_pressure -= 1
    elif avg_dur > 20:
        d3_pressure += 1  # 寿命長い→粘ってOK
    elif avg_dur > 30:
        d3_pressure += 2

    # 指標4: 短命区間率
    if avg_short5h > 60:
        d3_pressure -= 1  # 短命多い→早く逃げる
    elif avg_short5h < 20:
        d3_pressure += 1  # 短命少ない→粘ってOK

    # 指標7: 3落ち以上の出現率
    if avg_drop3 > 35:
        d3_pressure -= 1  # 長い列が多い→早く逃げる
    elif avg_drop3 < 15:
        d3_pressure += 1  # 短い列ばかり→粘ってOK

    if d3_pressure <= -2:
        new_d3 = max(cur_d3 - 1, d3_min)
    elif d3_pressure >= 2:
        new_d3 = min(cur_d3 + 1, d3_max)
    else:
        new_d3 = cur_d3
    if new_d3 != cur_d3:
        new_params["exit_drop3_limit"] = new_d3
        changes.append(f"D3 {cur_d3}→{new_d3} (p={d3_pressure})")
        adjusted = True

    # ============================================================
    # EXIT_DROP5: 5落ち即退室の切替
    # ============================================================
    # テレコ寿命が非常に短い → 4落ちで退室 (drop5→drop4)
    # テレコ寿命が長い → 5落ちのまま
    if avg_dur < 6 and cur_d5:
        # 5落ちではなく4落ちで退室すべき状況だが、
        # drop4_exit のパラメータは現在ないので、drop3_limit=1 で代替
        pass  # 将来拡張用
    elif avg_dur > 25 and not cur_d5:
        new_params["exit_drop5_immediate"] = True
        changes.append("D5 OFF→ON (dur↑↑)")
        adjusted = True

    # パラメータ変更があればSupabaseに保存
    if adjusted:
        reason = (
            f"auto: wr={avg_wr:.1f}% dur={avg_dur:.1f}h tereko={avg_tereko_rate:.0f}% "
            f"short5h={avg_short5h:.0f}% spread={avg_spread:.1f} top10={avg_top10:.1f}% drop3={avg_drop3:.0f}%"
        )
        save_params(new_params, reason)
        print(f"  [params] Adjusted: {', '.join(changes)}")
        print(f"  [params] Reason: {reason}")
    else:
        print(f"  [params] No adjustment (wr={avg_wr:.1f}% dur={avg_dur:.1f}h tereko={avg_tereko_rate:.0f}%)")

    return changes


if __name__ == "__main__":
    main()
