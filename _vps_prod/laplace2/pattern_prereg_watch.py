#!/usr/bin/env python3
"""パターン別フォワード勝率の事前登録監視 (2026-06-12 登録)

事前登録ルール (登録時点の成績):
  - niconico|dragon|P  (登録時 645W/655L = 49.6%, n=1300)
  - telecho|niconico|P (登録時 522W/549L = 48.7%, n=1071)
  判定時点: 各パターンの決着数(タイ除く)が 2,000 件に到達した時
  判定基準: 累計勝率 < 50.0% → 「除外候補」通知 / >= 50.0% → 「継続合格」通知
  それまでは行動しない(途中経過は週次サマリーのみ)。

動作 (cron 毎日 09:05 JST):
  1. v3/v4 state の per_pattern を CSV に追記 (推移の監査証跡)
  2. 判定時点に達したパターンがあれば Telegram に1回だけ通知
  3. 月曜は全パターンの現在地サマリーを Telegram に送る
"""
import json
import os
import csv
import datetime
import urllib.request
import urllib.parse

BASE = "/opt/laplace2"
V3_STATE = f"{BASE}/dual_line_pragmatic_state.json"
V4_STATE = f"{BASE}/dual_line_v4_state.json"
HISTORY_CSV = f"{BASE}/pattern_prereg_history.csv"
NOTIFY_STATE = f"{BASE}/pattern_prereg_notify_state.json"
ENV_PATH = f"{BASE}/.env"

# ── 事前登録ルール (変更する場合は新しい登録として日付を改める) ──
PREREG_DATE = "2026-06-12"
RULES = [
    {"system": "v3", "pattern": "niconico|dragon|P", "checkpoint_n": 2000, "threshold": 50.0},
    {"system": "v3", "pattern": "telecho|niconico|P", "checkpoint_n": 2000, "threshold": 50.0},
]
WEEKLY_SUMMARY_WEEKDAY = 0  # 月曜


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def tg_send(env, text):
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("DUAL_LINE_RESULT_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram config missing; skip send")
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"telegram send failed: {e}")
        return False


def per_pattern(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            s = json.load(f)
        return s.get("per_pattern", {}) or {}
    except Exception as e:
        print(f"state read failed {state_path}: {e}")
        return {}


def main():
    today = datetime.date.today()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    env = load_env(ENV_PATH)

    rows = []
    snap = {}
    for system, path in (("v3", V3_STATE), ("v4", V4_STATE)):
        for pat, v in sorted(per_pattern(path).items()):
            w = int(v.get("wins", 0))
            l = int(v.get("losses", 0))
            n = w + l
            wr = 100.0 * w / n if n else 0.0
            rows.append([str(today), system, pat, w, l, n, f"{wr:.2f}"])
            snap[(system, pat)] = (w, l, n, wr)

    # 1) CSV 追記 (1日1回; 同日重複は追記しない)
    write_header = not os.path.exists(HISTORY_CSV)
    already = False
    if not write_header:
        try:
            with open(HISTORY_CSV, encoding="utf-8") as f:
                for line in f:
                    if line.startswith(str(today) + ","):
                        already = True
                        break
        except Exception:
            pass
    if not already:
        with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
            wcsv = csv.writer(f)
            if write_header:
                wcsv.writerow(["date", "system", "pattern", "wins", "losses", "n", "win_rate"])
            wcsv.writerows(rows)
        print(f"history appended: {len(rows)} rows")
    else:
        print("history already has today; skip append")

    # 2) 通知状態
    try:
        with open(NOTIFY_STATE, encoding="utf-8") as f:
            nstate = json.load(f)
    except Exception:
        nstate = {}

    # 初回: 事前登録の宣言を送る
    if not nstate.get("registered_sent"):
        lines = [f"[PREREG] パターン監視 事前登録 ({PREREG_DATE})"]
        for r in RULES:
            key = (r["system"], r["pattern"])
            w, l, n, wr = snap.get(key, (0, 0, 0, 0.0))
            lines.append(
                f"{r['pattern']} ({r['system']}): 現在 {w}W/{l}L {wr:.1f}% (n={n})\n"
                f"  → n={r['checkpoint_n']} 到達時に {r['threshold']:.0f}%未満なら除外候補"
            )
        lines.append("判定到達まで途中経過では行動しない(週次サマリーのみ)。")
        if tg_send(env, "\n".join(lines)):
            nstate["registered_sent"] = True

    # 3) チェックポイント判定 (パターンごとに1回だけ)
    for r in RULES:
        key = f"{r['system']}:{r['pattern']}"
        if nstate.get("checkpoint_done", {}).get(key):
            continue
        w, l, n, wr = snap.get((r["system"], r["pattern"]), (0, 0, 0, 0.0))
        if n >= r["checkpoint_n"]:
            verdict = "❌除外候補" if wr < r["threshold"] else "✅継続合格"
            msg = (
                f"[PREREG] チェックポイント到達 {now}\n"
                f"{r['pattern']} ({r['system']}): {w}W/{l}L {wr:.2f}% (n={n})\n"
                f"事前登録基準(n>={r['checkpoint_n']}, {r['threshold']:.0f}%): {verdict}\n"
                f"※実際の除外はユーザ判断。v4変更は3経路(repo/VPS api/VPS bot)に注意。"
            )
            if tg_send(env, msg):
                nstate.setdefault("checkpoint_done", {})[key] = str(today)
                print(f"checkpoint notified: {key} {verdict}")

    # 4) 週次サマリー (月曜)
    if today.weekday() == WEEKLY_SUMMARY_WEEKDAY and nstate.get("last_weekly") != str(today):
        lines = [f"[PREREG] 週次パターンサマリー {today} (v3累計)"]
        for (system, pat), (w, l, n, wr) in sorted(snap.items()):
            if system != "v3":
                continue
            mark = ""
            for r in RULES:
                if r["pattern"] == pat and r["system"] == system:
                    mark = f" [監視中 {n}/{r['checkpoint_n']}]"
            lines.append(f"{pat}: {wr:.1f}% (n={n}){mark}")
        if tg_send(env, "\n".join(lines)):
            nstate["last_weekly"] = str(today)
            print("weekly summary sent")

    with open(NOTIFY_STATE, "w", encoding="utf-8") as f:
        json.dump(nstate, f, ensure_ascii=False, indent=2)
    print("done")


if __name__ == "__main__":
    main()
