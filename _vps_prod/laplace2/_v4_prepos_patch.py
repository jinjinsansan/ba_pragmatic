#!/usr/bin/env python3
"""Idempotent patch: add v4 (10-pattern) preposition Telegram forecasts to the
VPS dual-line bot's _v4_track.

Context (2026-05-31): v4 dual-mode already sends v4 SIGNAL and v4 WIN/LOSE/TIE
Telegrams, but no v4 FORECAST ("予告") — the v3 path sends forecasts via
_maybe_notify_preposition/_send_preposition_legacy, which is v3-only. This adds a
forecast right before the v4 signal check returns (i.e. when no v4 signal is live
yet but one is 1-2 hands away), mirroring the v3 forecast semantics. Dedup per
table by (score, pattern_keys, seq_len) so it doesn't spam every hand.

Idempotent: bails if the v4 forecast block marker is already present. Only edits
inside _v4_track; v3 path and v4 signal/settle paths are untouched. The forecast
uses live_preposition_for_history(seq, V4_PATTERNS) — requires the bot to import
live_preposition_for_history and LIVE_SIGNAL_PATTERNS_V4 (V4_PATTERNS alias).
"""
from __future__ import annotations
import sys, time, py_compile
from pathlib import Path

F = Path("/opt/laplace2/dual_line_pragmatic_bot.py")
MARK = "[v4-prepos]"  # presence => already patched

# Anchor: the v4 signal-check section inside _v4_track. We insert the forecast
# block right before `d = decide(...)` for the signal, so a live signal still
# takes precedence (forecast only fires when no v4 signal is active).
ANCHOR = (
    '        seq = "".join(seq_chars)\n'
    "        d = decide(seq, next_n=len(seq) + 1)\n"
    '        if d.action == "LOOK":\n'
    "            return\n"
)

INSERT = (
    '        seq = "".join(seq_chars)\n'
    "        # [v4-prepos] v4(10パターン) 予告(preposition) Telegram。\n"
    "        # v4 signal がまだ立っていない時だけ、1〜2手先で v4 になる予告を送る。\n"
    "        # dedup: 同一卓で (score, pattern_keys, seq_len) が変わった時のみ送信。\n"
    "        try:\n"
    "            if not hasattr(self, '_v4_last_prepos'):\n"
    "                self._v4_last_prepos = {}\n"
    "            _pv = live_preposition_for_history(seq, V4_PATTERNS)\n"
    "            _pscore = int(_pv.get('score') or 0)\n"
    "            if _pscore > 0:\n"
    "                _pkeys = [str(x) for x in (_pv.get('pattern_keys') or []) if str(x)]\n"
    "                _pside = str(_pv.get('side') or '')\n"
    "                _pkey = '%d|%s|%d' % (_pscore, '/'.join(sorted(_pkeys)), len(seq))\n"
    "                if _pkeys and self._v4_last_prepos.get(table_id) != _pkey:\n"
    "                    self._v4_last_prepos[table_id] = _pkey\n"
    "                    if os.getenv('BACOPY_V4_TELEGRAM', '1').strip() != '0':\n"
    "                        _remain = ('あと1手で10パターン候補' if _pscore >= 2\n"
    "                                   else 'あと2手で10パターン候補')\n"
    "                        _sn = 'BANKER' if _pside == 'B' else ('PLAYER' if _pside == 'P' else '未確定')\n"
    "                        _send_telegram(\n"
    "                            '\\U0001F4E2 [v4] 予告 %s\\n%s\\n\\u2192 %s BET\\nPatterns: %s\\nGUIが事前入場します' % (\n"
    "                                buf.table_name or table_id, _remain, _sn, ', '.join(_pkeys)))\n"
    "            else:\n"
    "                self._v4_last_prepos.pop(table_id, None)\n"
    "        except Exception as _v4ppe:\n"
    "            logger.debug('[v4-prepos] error: %s' % _v4ppe)\n"
    "        d = decide(seq, next_n=len(seq) + 1)\n"
    '        if d.action == "LOOK":\n'
    "            return\n"
)

IMPORT_OLD = "from dual_line_match import live_signal_for_history"
# we only ensure live_preposition_for_history is importable; bot already imports
# live_signal_for_history and V4_PATTERNS is defined via _v4_patch.


def main() -> int:
    if not F.exists():
        print("ABORT: target missing", F); return 1
    s = F.read_text(encoding="utf-8")
    if MARK in s:
        print("ALREADY_PATCHED"); return 0
    if "V4_PATTERNS" not in s or "_v4_track" not in s:
        print("ABORT: V4 not present (Phase1 _v4_patch not applied?)"); return 1
    if "live_preposition_for_history" not in s:
        print("ABORT: live_preposition_for_history not imported in bot"); return 1
    n = s.count(ANCHOR)
    if n != 1:
        print("ANCHOR_FAIL count=%d" % n); return 1
    s2 = s.replace(ANCHOR, INSERT)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = F.with_suffix(F.suffix + f".bak_v4prepos_{stamp}")
    bak.write_text(F.read_text(encoding="utf-8"), encoding="utf-8")
    F.write_text(s2, encoding="utf-8")
    try:
        py_compile.compile(str(F), doraise=True)
    except Exception as e:
        F.write_text(s, encoding="utf-8")
        print("ABORT: py_compile failed, rolled back:", e); return 1
    print("PATCHED_OK backup=", bak.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
