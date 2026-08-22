#!/usr/bin/env python3
"""USDT (TRC-20) deposit poller for bafather.uk crypto payments.

Runs one pass per invocation (cron, ~every 90s). No third-party processor:
1. GET pending orders from bafather.uk /api/payments/pending (shared secret).
2. Query TronGrid (free) for recent USDT TRC-20 transfers INTO the receiving address.
3. Match transfer amount to a pending order's expected_amount (unique cents).
4. POST /api/payments/credit {order_id, tx_hash, amount} -> web credits balance/bot_paid.
   Credit is idempotent on tx_hash, so re-posting is harmless.

Config via env (see /opt/laplace2/.env.crypto):
  PAYMENTS_BASE_URL   = https://www.bafather.uk
  PAYMENTS_SECRET     = <shared secret = web PAYMENTS_WEBHOOK_SECRET>
  PAY_USDT_ADDRESS    = <your USDT TRC-20 receiving address (public)>
  TRONGRID_API_KEY    = <optional, raises rate limit; blank uses public endpoint>
  PAY_MIN_AGE_SEC     = 60   (ignore transfers younger than this, reorg safety)
  PAY_LOOKBACK        = 50   (recent transfers to scan)
"""
import json, os, time, urllib.request, urllib.error, sys

USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # official USDT TRC-20 contract


def env(k, d=""):
    return os.environ.get(k, d)


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [crypto-poller] {msg}", flush=True)


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


ALERTED_FILE = "/opt/laplace2/crypto_alerted.txt"


def tg_notify(text):
    token = env("PAYMENTS_TELEGRAM_BOT_TOKEN")
    chat = env("PAYMENTS_TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        http_post(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        log(f"tg notify failed: {e}")


def load_alerted():
    try:
        with open(ALERTED_FILE) as f:
            return set(x.strip() for x in f if x.strip())
    except Exception:
        return set()


def mark_alerted(txid):
    try:
        with open(ALERTED_FILE, "a") as f:
            f.write(txid + "\n")
    except Exception:
        pass


def http_post(url, payload, headers=None, timeout=20):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": "http error"}


def main():
    base = env("PAYMENTS_BASE_URL", "https://www.bafather.uk").rstrip("/")
    secret = env("PAYMENTS_SECRET")
    addr = env("PAY_USDT_ADDRESS")
    tg_key = env("TRONGRID_API_KEY")
    min_age = int(env("PAY_MIN_AGE_SEC", "60") or 60)
    lookback = int(env("PAY_LOOKBACK", "50") or 50)
    if not secret or not addr:
        log("MISSING config: PAYMENTS_SECRET / PAY_USDT_ADDRESS"); sys.exit(1)

    # 1) pending orders
    try:
        pend = http_get(f"{base}/api/payments/pending", headers={"x-payments-secret": secret}).get("pending", [])
    except Exception as e:
        log(f"pending fetch failed: {e}"); sys.exit(1)
    if not pend:
        log("no pending orders"); return
    # expected_amount(2dp) -> order
    by_amount = {round(float(o["expected_amount"]), 2): o for o in pend}
    log(f"pending={len(pend)} amounts={sorted(by_amount.keys())}")

    # 2) recent incoming USDT transfers to addr
    tg = f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20?only_to=true&limit={lookback}&contract_address={USDT_TRC20}"
    tg_headers = {"TRON-PRO-API-KEY": tg_key} if tg_key else {}
    try:
        rows = http_get(tg, headers=tg_headers).get("data", [])
    except Exception as e:
        log(f"trongrid fetch failed: {e}"); sys.exit(1)

    now_ms = time.time() * 1000
    alerted = load_alerted()
    matched = 0
    amounts = sorted(by_amount.keys())
    for tx in rows:
        try:
            if tx.get("to") != addr:
                continue
            ts = float(tx.get("block_timestamp") or 0)
            if min_age and ts and (now_ms - ts) < min_age * 1000:
                continue  # too young, wait (reorg safety)
            decimals = int((tx.get("token_info") or {}).get("decimals") or 6)
            amount = round(int(tx.get("value") or 0) / (10 ** decimals), 2)
            order = by_amount.get(amount)
            txid = tx.get("transaction_id")
            if not order:
                # 金額違い着金アラート: 保留注文があり、その金額に近い(±$2)のに一致しない
                # =ユーザーの金額入力ミスの疑い。専用Botへ1回だけ通知(誤検知抑制)。
                if amounts and txid and txid not in alerted:
                    near = min(amounts, key=lambda a: abs(a - amount))
                    if abs(near - amount) <= 2.0:
                        tg_notify(
                            f"⚠️ <b>金額違いの着金</b>(自動反映されていません)\n"
                            f"受信額: {amount} USDT\n"
                            f"近い保留注文額: {near} USDT\n"
                            f"tx: <code>{txid}</code>\n"
                            f"※管理画面で手動確認/充当してください"
                        )
                        mark_alerted(txid)
                        log(f"UNMATCHED alert sent amount={amount} near={near} tx={txid[:16]}")
                continue
            st, resp = http_post(
                f"{base}/api/payments/credit",
                {"order_id": order["id"], "tx_hash": txid, "amount": amount},
                headers={"x-payments-secret": secret},
            )
            if st == 200 and resp.get("ok"):
                matched += 1
                tag = "ALREADY" if resp.get("already") else "CREDITED"
                log(f"{tag} order={order['id']} kind={order.get('kind')} amount={amount} tx={txid[:16]}")
            else:
                log(f"credit FAILED st={st} order={order['id']} amount={amount} resp={resp}")
        except Exception as e:
            log(f"tx process error: {e}")
    log(f"done matched={matched} scanned={len(rows)}")


if __name__ == "__main__":
    main()
