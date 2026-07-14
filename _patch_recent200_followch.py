#!/usr/bin/env python3
"""追従チャンネル(DUAL_LINE_RESULT_CHAT_ID)の全メッセージにも
「直近200勝率 + 直近20手絵文字」を追加する冪等パッチ(Phase 2)。

対象: /opt/laplace2/dual_line_pragmatic_bot.py (VPS本番)
前提: _patch_recent200_telegram.py 適用済み(_recent_wl_add_and_lines が存在)。

設計:
  - 窓の更新はパターンch側(v3=_resolve_prediction / v4=_v4_track)で済んでおり、
    追従chの送信は両方ともその後に呼ばれる(順序確認済 2026-07-14)。
    → 追従ch側は result="" の表示専用呼び出し=二重カウントなし・数字は両ch一致。
  - 追従手(🟡)の勝敗は窓に入れない: 直近200は「NOWシステムの成績」の温度計。
    追従の損得は既存の「追従は得か?損か?」ブロックが担当。
  - 挿入3箇所: ①集計外コンパクト ②NOW決済(メイン) ③追従手決済。

使い方 (VPS上): python3 _patch_recent200_followch.py
"""
from __future__ import annotations

import py_compile
import shutil
import time

F = "/opt/laplace2/dual_line_pragmatic_bot.py"

MARKER = "_recent_wl_show"

# ── 1) 表示専用ヘルパー: _is_fast_table_name の直前に挿入 ──────────────
HELPER_ANCHOR = "def _is_fast_table_name(name: str) -> bool:\n"
HELPER = '''def _recent_wl_show(origin):
    """追従chメッセージ用: 系統別の直近200行を表示のみ(窓は更新しない=パターン
    ch側の決済で更新済み。二重カウント防止)。先頭に改行を含めて返す。"""
    try:
        p = _RECENT_WL_V3_PATH if str(origin) == "v3" else _RECENT_WL_V4_PATH
        line = _recent_wl_add_and_lines(p, "")
        return ("\\n" + line) if line else ""
    except Exception:
        return ""


''' + HELPER_ANCHOR

# ── 2) _follow_sim_now_result: 集計外コンパクト配信 ─────────────────────
A_COMPACT = (
    "                    tag, icon, result, table_name, pattern_key, side, outcome))\n"
)
I_COMPACT = (
    "                    tag, icon, result, table_name, pattern_key, side, outcome)"
    " + _recent_wl_show(origin))\n"
)

# ── 3) _follow_sim_now_result: NOW決済メイン配信 ───────────────────────
A_NOW = (
    "                self._follow_sim_stats_line(st), nxt_label))\n"
)
I_NOW = (
    "                self._follow_sim_stats_line(st), nxt_label)"
    " + _recent_wl_show(origin))\n"
)

# ── 4) _follow_sim_on_hand: 追従手決済配信 ─────────────────────────────
A_FOLLOW = (
    "                self._follow_sim_stats_line(st)))\n"
)
I_FOLLOW = (
    "                self._follow_sim_stats_line(st))"
    " + _recent_wl_show(origin))\n"
)


def apply(s: str) -> str:
    if MARKER in s:
        return s  # already patched (idempotent)
    if "_recent_wl_add_and_lines" not in s:
        raise SystemExit("PHASE1_NOT_APPLIED: run _patch_recent200_telegram.py first")
    for anchor in (HELPER_ANCHOR, A_COMPACT, A_NOW, A_FOLLOW):
        n = s.count(anchor)
        if n != 1:
            raise SystemExit("ANCHOR_FAIL count=%d for %r" % (n, anchor[:60]))
    s = s.replace(HELPER_ANCHOR, HELPER)
    s = s.replace(A_COMPACT, I_COMPACT)
    s = s.replace(A_NOW, I_NOW)
    s = s.replace(A_FOLLOW, I_FOLLOW)
    return s


def main() -> int:
    s = open(F, encoding="utf-8").read()
    if MARKER in s:
        print("ALREADY_PATCHED")
        return 0
    out = apply(s)
    bak = F + ".bak_recent200fw_" + time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(F, bak)
    open(F, "w", encoding="utf-8").write(out)
    py_compile.compile(F, doraise=True)
    print("PATCHED_OK backup=" + bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
