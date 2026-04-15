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
    return datetime.now(JST).strftime("%Y-%m-%d")


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
    # 入室条件 ENTRY_THRESHOLD (短列率の閾値)
    # ============================================================
    et_pressure = 0  # +なら厳しく、-なら緩める

    # 指標1: テレコ寿命
    if avg_dur < 8:
        et_pressure += 2
    elif avg_dur < 12:
        et_pressure += 1
    elif avg_dur > 18:
        et_pressure -= 1
    elif avg_dur > 25:
        et_pressure -= 2

    # 指標3: テレコ出現率
    if avg_tereko_rate < 35:
        et_pressure -= 1  # テレコが少ない→緩めないと入れない
    elif avg_tereko_rate > 50:
        et_pressure += 1  # テレコが多い→厳しくしても十分入れる

    # 指標4: 短命区間率
    if avg_short5h > 55:
        et_pressure += 1  # 短命が多い→厳しくして確実なテレコだけ
    elif avg_short5h < 25:
        et_pressure -= 1  # 短命が少ない→緩めてOK

    # 指標6: パターンTop10の平均勝率低下
    if avg_top10 < 53:
        et_pressure += 1  # 最強パターンの勝率が落ちた→厳しく
    elif avg_top10 > 58:
        et_pressure -= 1  # 最強パターンが強い→緩めてOK

    if et_pressure >= 2:
        new_et = min(cur_et + 0.05, et_max)
    elif et_pressure == 1:
        new_et = min(cur_et + 0.02, et_max)
    elif et_pressure <= -2:
        new_et = max(cur_et - 0.05, et_min)
    elif et_pressure == -1:
        new_et = max(cur_et - 0.02, et_min)
    else:
        new_et = cur_et
    new_et = round(new_et, 2)
    if new_et != cur_et:
        new_params["entry_threshold"] = new_et
        changes.append(f"ET {cur_et}→{new_et} (p={et_pressure})")
        adjusted = True

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
