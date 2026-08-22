#!/usr/bin/env python3
# Generate hourly win-rate stats JSON for the GUI panel.
# master bacopy-api serves it via GET /api/hourly-stats.
# Reuses hourly_report.py's log parsing (same data as the Telegram hourly report).
# Cron: */10 * * * *
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, "/opt/laplace2")
from hourly_report import parse  # noqa: E402

OUT = "/opt/bacopy/hourly_stats.json"


def build(rows):
    today = datetime.now().date()
    tr = [(ts, w) for ts, w in rows if ts.date() == today]
    hours = {}
    for ts, w in tr:
        h = hours.setdefault(ts.hour, {"h": ts.hour, "n": 0, "w": 0})
        h["n"] += 1
        h["w"] += w
    streak = 0
    for _, w in reversed(tr):
        if w == 0:
            streak += 1
        else:
            break
    return {
        "hours": [hours[k] for k in sorted(hours)],
        "cum_n": len(tr),
        "cum_w": sum(w for _, w in tr),
        "lose_streak": streak,
    }


def main():
    v2, v4 = parse()
    payload = {
        "ok": True,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "v3": build(v2),
        "v4": build(v4),
    }
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    os.chmod(OUT, 0o644)  # bacopy-api(非rootサービス)が読めるように
    print("OK", payload["updated_at"], "v3_n=%d" % payload["v3"]["cum_n"],
          "v4_n=%d" % payload["v4"]["cum_n"])


if __name__ == "__main__":
    main()
