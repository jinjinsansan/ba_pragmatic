#!/usr/bin/env python3
# Patch /opt/bacopy/bacopy_api.py: add GET /api/hourly-stats (idempotent).
import shutil
import time

PATH = "/opt/bacopy/bacopy_api.py"
src = open(PATH, encoding="utf-8").read()
if "/api/hourly-stats" in src:
    print("ALREADY_PATCHED")
    raise SystemExit(0)
anchor = '        if u.path == "/api/preposition":'
assert anchor in src, "anchor not found"
block = '''        if u.path == "/api/hourly-stats":
            # GUI 毎時勝率パネル: cron(/opt/laplace2/hourly_stats_json.py)が10分毎に
            # 生成するJSONをそのまま返す(時間帯別 勝敗集計・当日JST)。
            try:
                _hs = Path("/opt/bacopy/hourly_stats.json")
                _hs_data = json.loads(_hs.read_text(encoding="utf-8")) if _hs.exists() else {"ok": False, "error": "not_ready"}
            except Exception as _hse:
                _hs_data = {"ok": False, "error": str(_hse)}
            return _send_json(self, 200, _hs_data)
'''
shutil.copy(PATH, PATH + ".bak_hourly_" + time.strftime("%H%M%S"))
open(PATH, "w", encoding="utf-8").write(src.replace(anchor, block + anchor, 1))
print("PATCHED")
