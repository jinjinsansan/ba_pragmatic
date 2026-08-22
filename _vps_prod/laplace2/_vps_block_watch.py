#!/usr/bin/env python3
"""stake.com 等が日本向けの法的ブロック(HTTP 451)に変わったらTelegram通知する監視。

背景: 2026-08-06 に総務省→Cloudflare通知で sportsbet.io が日本向け 451 になった
      (RISK_JP_CDN_BLOCKING_2026-08-06.md)。同じ手口が stake.com に及ぶと、
      日本IPで動いている受け子の賭け経路が全機同時に止まる。

正常値は **403「Just a moment...」**(Cloudflareのbot判定=実ブラウザは通る)。
451 への変化、または本文に法的ブロックの痕跡が出た時だけ鳴らす。

- 状態変化時のみ通知(毎時鳴らさない)。復旧時も通知する。
- 一時的な通信エラーでは鳴らさない(FAIL_STREAK 回連続で初めて鳴らす)。
- sportsbet.io は「既に451と分かっている対照」として自己診断に使う
  (検出器が壊れていないことを毎回確認し、壊れていたら通知する)。

VPS: /opt/laplace2/_vps_block_watch.py   cron: 17 * * * *
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

ENV = "/opt/laplace2/.env"
STATE = "/opt/laplace2/block_watch_state.json"
FAIL_STREAK = 3  # 連続失敗がこの回数に達したら「到達不能」として通知

# 監視対象(賭け経路)。Stakeはドメインを増やすことがあるので主要どころを見る。
TARGETS = ["stake.com", "stake.bet", "stake.games"]
# 対照: 既に451と判明している。検出器の自己診断に使う(通知はしない)。
CONTROL = "sportsbet.io"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
LEGAL_MARKERS = ("unavailable for legal reasons", "lumendatabase.org", "法的理由")


def load_env(path):
    d = {}
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d


def probe(host):
    """Return (state, detail). state: 'blocked' | 'ok' | 'error'."""
    url = f"https://{host}/"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            code, body = r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 403(bot判定)も451もここに来る
        code = e.code
        try:
            body = e.read(20000).decode("utf-8", "replace")
        except Exception:
            body = ""
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"

    low = body.lower()
    hit = [m for m in LEGAL_MARKERS if m in low]
    if code == 451 or hit:
        lumen = ""
        i = low.find("lumendatabase.org/notices/")
        if i >= 0:
            lumen = " https://" + body[i:i + 60].split('"')[0].split("<")[0]
        return "blocked", f"HTTP {code}{lumen}"
    return "ok", f"HTTP {code}"


def send(token, chat_ids, text):
    for cid in chat_ids:
        try:
            data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=20) as r:
                print("SENT", cid, r.status)
        except Exception as e:
            print("SEND_FAILED:", cid, e)


def main():
    env = load_env(ENV)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    raw = (env.get("BLOCK_ALERT_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", ""))
    chat_ids = [c.strip() for c in str(raw).split(",") if c.strip()]
    for extra in str(env.get("DUAL_LINE_EXTRA_CHAT_IDS", "")).split(","):
        if extra.strip() and extra.strip() not in chat_ids:
            chat_ids.append(extra.strip())

    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        state = {}

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    alerts = []

    # 対照ドメインで検出器そのものが生きているか毎回確かめる
    cstate, cdetail = probe(CONTROL)
    print(f"[control] {CONTROL} -> {cstate} ({cdetail})")
    detector_ok = (cstate == "blocked")
    if not detector_ok and cstate != "error":
        # 既知の451が検出できない = 検出器か前提が壊れている
        if state.get("_detector") != "broken":
            alerts.append(
                f"⚠️ ブロック監視の自己診断NG\n{CONTROL} は451のはずが {cdetail}\n"
                f"検出条件かCloudflare側の挙動が変わった可能性。監視を信用しないこと。")
        state["_detector"] = "broken"
    else:
        state["_detector"] = "ok"

    for host in TARGETS:
        st, detail = probe(host)
        prev = state.get(host, {})
        prev_state = prev.get("state")
        fails = int(prev.get("fails", 0))
        print(f"[{host}] {st} ({detail}) prev={prev_state}")

        if st == "error":
            fails += 1
            state[host] = {"state": prev_state, "fails": fails, "detail": detail, "at": now}
            if fails == FAIL_STREAK and prev_state != "unreachable":
                alerts.append(f"⚠️ {host} に {FAIL_STREAK} 回連続で到達できません\n{detail}")
                state[host]["state"] = "unreachable"
            continue

        state[host] = {"state": st, "fails": 0, "detail": detail, "at": now}
        if prev_state is None:
            continue  # 初回は基準を記録するだけ
        if st == "blocked" and prev_state != "blocked":
            alerts.append(
                f"🚨 {host} が日本向け法的ブロックになりました\n{detail}\n"
                f"→ 日本IPの受け子は賭けられません(シグナル配信は継続)\n{now}")
        elif st == "ok" and prev_state == "blocked":
            alerts.append(f"✅ {host} のブロックが解除されました\n{detail}\n{now}")
        elif st == "ok" and prev_state == "unreachable":
            alerts.append(f"✅ {host} に再到達できました\n{detail}\n{now}")

    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception as e:
        print("STATE_SAVE_FAILED:", e)

    if alerts and "--dry" not in sys.argv and token and chat_ids:
        send(token, chat_ids, "\n\n".join(alerts))
    elif alerts:
        print("--- 通知内容(未送信) ---")
        print("\n\n".join(alerts))
    else:
        print("変化なし = 通知しない")


if __name__ == "__main__":
    main()
