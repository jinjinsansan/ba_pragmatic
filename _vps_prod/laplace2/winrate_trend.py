#!/usr/bin/env python3
# Build the GUI "winrate range" chart data: the SAME cumulative win-rate numbers the
# Telegram dry-run channels show, as a time series, so the chart matches the channel exactly.
#
#   pattern tab  -> v3 = self.wins  (log "resolved=N  W/L=w/l")
#                   v4 = self._v4   (log "[v4 STATUS] ... W/L/T=w/l/t")
#                   == the 6パターン/10パターン channel %  (byte-identical counters)
#   follow tab   -> follow-included = (w+fw)/(w+fw+l+fl) from dual_line_follow_sim.json
#                   == the result-channel 追従込み %  (same source, not logged so forward-only)
#
# Counters reset on bot restart; we keep only the LAST monotonic segment (the current run,
# which is what the channel currently displays). floor/ceil = min/max wr over that segment.
#
# Cron: */10 * * * *   ->  /opt/bacopy/winrate_trend.json   (served by /api/winrate-trend)
import json
import os
import re
import sys
import tempfile
from datetime import datetime

BOT_LOG = "/opt/laplace2/dual_line_pragmatic_bot.log"
FOLLOW = "/opt/laplace2/dual_line_follow_sim.json"
OUT = "/opt/bacopy/winrate_trend.json"
MAX_POINTS = 600          # cap series length (downsample older points)
FOLLOW_MAX = 4000         # cap forward-built follow series
MIN_N = 500              # restored after follow-v3 restore 2026-06-27
                          # (n>=MIN_N) toward the line + floor/ceil, so the range matches the
                          # tight band the channel actually shows (~50.2-52%), not n=23 noise.

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
V3_RE = re.compile(r"resolved=\d+\s+W/L=(\d+)/(\d+)")
V4_RE = re.compile(r"v4 STATUS\]\s*signals=\d+\s+resolved=\d+\s+W/L/T=(\d+)/(\d+)/(\d+)")


def _ts(line):
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def parse_pattern(rx, groups_wl):
    """Return list of (datetime, w, l) for matching cumulative lines in the bot log."""
    pts = []
    try:
        with open(BOT_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = rx.search(line)
                if not m:
                    continue
                ts = _ts(line)
                if ts is None:
                    continue
                w = int(m.group(groups_wl[0]))
                l = int(m.group(groups_wl[1]))
                pts.append((ts, w, l))
    except FileNotFoundError:
        return []
    return pts


def last_segment(pts):
    """Counters reset on restart -> keep only the final monotonic-by-w run (current run)."""
    if not pts:
        return []
    start = 0
    for i in range(1, len(pts)):
        prev_r = pts[i - 1][1] + pts[i - 1][2]
        cur_r = pts[i][1] + pts[i][2]
        # a reset shows up as total resolved dropping below the previous point
        if cur_r < prev_r:
            restored = any((pts[j][1] + pts[j][2]) >= prev_r for j in range(i + 1, len(pts)))
            if not restored:
                start = i   # genuine (un-restored) reset -> this is the current run
    return pts[start:]


def dedup_and_downsample(pts):
    """Drop consecutive duplicates (same w,l), then cap to MAX_POINTS keeping the latest."""
    out = []
    for p in pts:
        if out and out[-1][1] == p[1] and out[-1][2] == p[2]:
            out[-1] = p  # keep latest ts for an unchanged count
            continue
        out.append(p)
    if len(out) > MAX_POINTS:
        # keep the most recent MAX_POINTS-50 verbatim, downsample the older head
        head = out[:-(MAX_POINTS - 50)]
        tail = out[-(MAX_POINTS - 50):]
        step = max(1, len(head) // 50)
        out = head[::step] + tail
    return out


def to_series(pts):
    points = []
    floor = None
    ceil = None
    for ts, w, l in pts:
        n = w + l
        if n < MIN_N:
            continue
        wr = round(100.0 * w / n, 2)
        points.append({"ts": ts.strftime("%Y-%m-%dT%H:%M:%S"), "w": w, "l": l, "n": n, "wr": wr})
        floor = wr if floor is None else min(floor, wr)
        ceil = wr if ceil is None else max(ceil, wr)
    cur = points[-1]["wr"] if points else None
    return {"points": points, "floor": floor, "ceil": ceil, "cur": cur}


def build_pattern():
    v3 = to_series(dedup_and_downsample(last_segment(parse_pattern(V3_RE, (1, 2)))))
    v4 = to_series(dedup_and_downsample(last_segment(parse_pattern(V4_RE, (1, 2)))))
    return {"v3": v3, "v4": v4}


def follow_point():
    """Current follow-included cumulative per system from follow_sim.json (== 追従込み channel)."""
    try:
        d = json.loads(open(FOLLOW, encoding="utf-8").read())
    except Exception:
        return None
    out = {}
    for k in ("v3", "v4"):
        s = d.get(k) or {}
        try:
            tw = int(s["w"]) + int(s["fw"])
            tl = int(s["l"]) + int(s["fl"])
        except Exception:
            continue
        out[k] = (tw, tl)
    return out


def build_follow(prev):
    """Append the current follow-included snapshot to the forward-built series."""
    pt = follow_point()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    res = {}
    for k in ("v3", "v4"):
        prev_pts = (((prev or {}).get("follow") or {}).get(k) or {}).get("points") or []
        raw = [(p["ts"], p["w"], p["l"]) for p in prev_pts]
        if pt and k in pt:
            tw, tl = pt[k]
            if not raw or raw[-1][1] != tw or raw[-1][2] != tl:
                raw.append((now, tw, tl))
            else:
                raw[-1] = (now, tw, tl)
        if len(raw) > FOLLOW_MAX:
            raw = raw[-FOLLOW_MAX:]
        res[k] = to_series([(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S"), w, l) for ts, w, l in raw])
    return res


def main():
    try:
        prev = json.loads(open(OUT, encoding="utf-8").read())
    except Exception:
        prev = {}
    data = {
        "ok": True,
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "pattern": build_pattern(),
        "follow": build_follow(prev),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    try:
        os.chmod(OUT, 0o644)  # API (and any user) must be able to read it
    except Exception:
        pass
    p = data["pattern"]
    print("[winrate_trend] v3 cur=%s floor=%s ceil=%s n_pts=%d | v4 cur=%s floor=%s ceil=%s n_pts=%d" % (
        p["v3"]["cur"], p["v3"]["floor"], p["v3"]["ceil"], len(p["v3"]["points"]),
        p["v4"]["cur"], p["v4"]["floor"], p["v4"]["ceil"], len(p["v4"]["points"])))


if __name__ == "__main__":
    main()
