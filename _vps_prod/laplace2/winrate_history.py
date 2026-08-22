#!/usr/bin/env python3
# Accumulate per-hour win-rate buckets into a durable JSON for the GUI FX chart.
# master bacopy-api serves it via GET /api/winrate-history.
# Source = hourly_report.parse() (same as the Telegram hourly report / hourly_stats.json),
# but UNLIKE hourly_stats.json this is NOT reset at midnight: every completed hour is
# merged into a persistent store so 1H/4H/D/W history survives log truncation.
#
# Merge rule (max-n wins): for each (ymd, hour) bucket we keep the observation with the
# largest sample count. A still-growing current hour updates upward; once the hour passes
# and the count stops growing it is final. Old buckets persist even after they leave the
# (truncated) bot log, because the persistent JSON is the source of truth going forward.
#
# Cron: */10 * * * *  (same cadence as hourly_stats_json.py)
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, "/opt/laplace2")
from hourly_report import parse  # noqa: E402

OUT = "/opt/bacopy/winrate_history.json"


def buckets_from_rows(rows):
    """rows = [(datetime, win01), ...] -> {"ymd|h": {ymd,h,n,w}}"""
    out = {}
    for ts, w in rows:
        ymd = ts.strftime("%Y-%m-%d")
        h = ts.hour
        key = f"{ymd}|{h}"
        b = out.setdefault(key, {"ymd": ymd, "h": h, "n": 0, "w": 0})
        b["n"] += 1
        b["w"] += int(w)
    return out


def load_store(pattern_key):
    try:
        data = json.loads(open(OUT, encoding="utf-8").read())
        store = {}
        for b in (data.get(pattern_key) or {}).get("buckets") or []:
            store[f"{b['ymd']}|{b['h']}"] = {"ymd": b["ymd"], "h": int(b["h"]), "n": int(b["n"]), "w": int(b["w"])}
        return store
    except Exception:
        return {}


def merge(store, fresh):
    # max-n wins: keep the most complete observation of each hour.
    for key, b in fresh.items():
        cur = store.get(key)
        if cur is None or b["n"] >= cur["n"]:
            store[key] = b
    return store


def to_buckets(store):
    return sorted(store.values(), key=lambda b: (b["ymd"], b["h"]))


def main():
    v2, v4 = parse()
    s3 = merge(load_store("v3"), buckets_from_rows(v2))
    s4 = merge(load_store("v4"), buckets_from_rows(v4))
    payload = {
        "ok": True,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "v3": {"buckets": to_buckets(s3)},
        "v4": {"buckets": to_buckets(s4)},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    os.chmod(OUT, 0o644)  # bacopy-api(非rootサービス)が読めるように
    print("OK", payload["updated_at"],
          "v3_buckets=%d" % len(payload["v3"]["buckets"]),
          "v4_buckets=%d" % len(payload["v4"]["buckets"]))


if __name__ == "__main__":
    main()
