#!/usr/bin/env python3
"""全ユーザー日次帳簿プラー (LAPLACE2)。
Supabase billing.session_state を単一ソースに全ユーザーの残高/日次損益/推定入出金を出力。
- 鍵は web/.env.local の SUPABASE_SERVICE_ROLE_KEY を自動読込(リポジトリ内・コミットしない)。
- 推定入出金 = (current_balance - daily_open.balance) - daily_bet_pnl
    プラス=入金 / マイナス=出金。|>=200| or 残高20%超で「明らかな入出金」フラグ。
使い方:
    python _ledger_pull.py            # 整形テーブルを表示
    python _ledger_pull.py --md       # LEDGER_DAILY_2026.md 追記用のMarkdown行を出力
このスクリプトを走らせて出力を LEDGER_DAILY_2026.md に貼れば帳簿更新完了。
"""
import os, re, sys, json, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(ROOT, "web", ".env.local")

# email -> 表示名(受け子)。メールが真実、ラベルは便宜。
NAME = {
    "goldbenchan@gmail.com":       "00 オーナー",
    "lselfloveself@gmail.com":     "bafather(検証機)",
    "sara01.maria30@icloud.com":   "02 sara",
    "ageagetony@gmail.com":        "03 つたいし",
    "shojihashimoto0922@gmail.com":"04 橋本",
    "hikata26621146@gmail.com":    "05 梶原",
    "hikata11462662@gmail.com":    "06 ひろゆき",
    "masakatsu.ogawa@gmail.com":   "07 小川",
    "black0213black@gmail.com":    "08 れいじ",
    "hiroyuki2626123@gmail.com":   "01? 休止アカ",
}
ORDER = {n: i for i, n in enumerate(
    ["00 オーナー","bafather(検証機)","02 sara","03 つたいし","04 橋本",
     "05 梶原","06 ひろゆき","07 小川","08 れいじ","01? 休止アカ"])}


def read_env():
    url = key = None
    with open(ENV, encoding="utf-8") as f:
        for ln in f:
            m = re.match(r'^([A-Z_]+)=(.*)$', ln.strip())
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
            if k == "NEXT_PUBLIC_SUPABASE_URL":
                url = v
            elif k == "SUPABASE_SERVICE_ROLE_KEY":
                key = v
    if not url or not key:
        sys.exit("web/.env.local に SUPABASE_URL / SERVICE_ROLE_KEY が見つかりません")
    return url, key


def get(url, key, path):
    req = urllib.request.Request(url + "/rest/v1/" + path,
                                 headers={"apikey": key, "Authorization": "Bearer " + key})
    return json.load(urllib.request.urlopen(req, timeout=30))


def main():
    url, key = read_env()
    prof = {p["id"]: p.get("email") for p in get(url, key, "profiles?select=id,email")}
    rows = get(url, key, "billing?select=user_id,balance,is_free,bot_paid,suspended,session_state,updated_at")
    out = []
    for r in rows:
        em = prof.get(r["user_id"], "?")
        nm = NAME.get(em, em)
        ss = r.get("session_state") or {}
        do = ss.get("daily_open") or {}
        dd = do.get("date") if isinstance(do, dict) else None
        ob = do.get("balance") if isinstance(do, dict) else None
        cur = ss.get("current_balance")
        bp = ss.get("daily_bet_pnl")
        upd = (r.get("updated_at") or "")[:16]
        delta = est = None
        if cur is not None and ob is not None:
            delta = round(cur - ob, 2)
            est = round(delta - (bp or 0.0), 2)
        flag = ""
        big = est is not None and (abs(est) >= 200 or (ob and abs(est) >= 0.20 * ob))
        if big:
            flag = "🔺入金" if est > 0 else "🔻出金"
        out.append(dict(nm=nm, em=em, paid=int(bool(r.get("bot_paid"))),
                        free=int(bool(r.get("is_free"))), dd=dd, ob=ob, cur=cur,
                        bp=bp, delta=delta, est=est, flag=flag, big=big, upd=upd))
    out.sort(key=lambda d: ORDER.get(d["nm"], 99))

    if "--md" in sys.argv:
        print("| 受け子 | 開始残高 | 現在残高 | Δ残高 | GUI損益(bet) | 推定入出金 | フラグ | 最終更新 |")
        print("|---|---|---|---|---|---|---|---|")
        for d in out:
            f = lambda x: "-" if x is None else x
            print(f"| {d['nm']} | {f(d['ob'])} | {f(d['cur'])} | {f(d['delta'])} | {f(d['bp'])} | {f(d['est'])} | {d['flag']} | {d['dd']} {d['upd'][11:]} |")
        return

    print(f"{'uke':<18}{'date':<11}{'open':>10}{'cur':>10}{'dBal':>10}{'betPnl':>10}{'est_cash':>11}  flag")
    print("-" * 92)
    for d in out:
        f = lambda x: " " * 10 if x is None else (f"{x:>10.2f}" if isinstance(x, (int, float)) else f"{x:>10}")
        tag = "" if not d["big"] else ("+IN " if d["est"] > 0 else "-OUT")
        est = " " * 11 if d["est"] is None else f"{d['est']:>11.2f}"
        print(f"{d['nm']:<18}{str(d['dd'] or ''):<11}{f(d['ob'])}{f(d['cur'])}{f(d['delta'])}{f(d['bp'])}{est}  {tag}")


if __name__ == "__main__":
    main()
