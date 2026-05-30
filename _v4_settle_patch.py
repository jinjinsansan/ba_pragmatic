#!/usr/bin/env python3
"""Phase2-fix: VPS dual_line_pragmatic_bot.py の v4(10パターン) decision を
master に settled post する冪等パッチ。

背景 (2026-05-30 ライブテストで発覚):
  Phase2 publish パッチは v4 decision を「配信」するだけで「決済」を実装して
  いなかった。v3 (_publish_live_decision→_post_decision_settlement) は VPS が
  結果を観測して settled を post するため master 行が done になり、bafather の
  照合 poll が GUI 〇× を更新する。v4 は publish のみだったため master 行が
  永久 processing(bet_sent) のまま → GUI 〇× 凍結 / NOW lock 滞留 / 後続BET阻害。

設計 (v3 を鏡写し・v3 完全独立):
  - _v4_track が _publish_v4_decision の戻り did を _v4_pending[table_id] に保持。
  - 結果観測時 (_v4_track の resolve ブロック) に _settle_v4_decision を呼び、
    publish と同じ env で URL/鍵を解決して /api/decisions/{did}/result に
    settled(status=done) を post する。
  - BACOPY_V4_PUBLISH=1 のときのみ動作 (publish と同じガード)。
  - 冪等: _settle_v4_decision が既にあれば ALREADY_PATCHED。
  - 前提: Phase2 publish パッチ適用済 (_publish_v4_decision 存在)。

使い方 (VPS 上): python3 _v4_settle_patch.py
"""
from __future__ import annotations

F = "/opt/laplace2/dual_line_pragmatic_bot.py"

# ── 注入する settled post メソッド (4 space indent = クラスメソッド) ───────
_SETTLE_METHOD = '''    def _settle_v4_decision(self, pend, outcome, new_hand):
        """Phase2-fix: v4 decision を master に settled post (v3 と同等)。
        publish と同じ env で URL/鍵解決。BACOPY_V4_PUBLISH=1 のときのみ。"""
        if os.getenv("BACOPY_V4_PUBLISH", "0").strip() != "1":
            return
        did = str((pend or {}).get("did") or "").strip()
        if not did:
            return
        side = str((pend or {}).get("side") or "").upper()
        pk = str((pend or {}).get("pattern_key") or "")
        tie = outcome == "T"
        won = None if tie else (outcome == side)
        result = "TIE" if tie else ("WIN" if won else "LOSE")
        try:
            unit = float(getattr(getattr(self, "money", None), "unit", 0.0) or 0.0)
        except Exception:
            unit = 0.0
        pnl = 0.0 if tie else (
            unit * COMMISSION_BANKER if won and side == "B"
            else unit if won else -unit
        )
        base_url = (
            os.getenv("BACOPY_SIGNAL_API_URL", "").rstrip("/")
            or os.getenv("BACOPY_REMOTE_API_URL", "").rstrip("/")
            or "https://master.bafather.uk"
        )
        api_key = (
            os.getenv("BACOPY_SIGNAL_API_KEY", "").strip()
            or os.getenv("BACOPY_REMOTE_API_KEY", "").strip()
            or os.getenv("BACOPY_API_KEY", "").strip()
            or os.getenv("LAPLACE_API_KEY", "").strip()
        )
        if not api_key:
            logger.error("[v4-settle] skipped: remote API key is not configured")
            return
        payload = {
            "mode": "dual_line_live",
            "observed_at": _utc_now_iso(),
            "provider": "pragmatic",
            "table_id": str((pend or {}).get("qpid") or ""),
            "table_name": str((pend or {}).get("table_name") or ""),
            "pattern_key": pk,
            "phase": "settled",
            "settled": True,
            "outcome": outcome,
            "result": result,
            "won": None if tie else bool(won),
            "tie": bool(tie),
            "pnl": float(pnl),
            "mode_tag": "v4",
            "hand": {
                "game_id": str(new_hand.get("gameId") or new_hand.get("game_id") or ""),
                "winner": str(new_hand.get("winner") or ""),
            },
        }
        res = self._api_post(
            "/api/decisions/%s/result" % did,
            {"result": payload, "status": "done"},
            base_url=base_url, api_key=api_key,
        )
        if res.get("ok"):
            logger.info("[v4-settle] posted settled did=%s result=%s outcome=%s" % (did, result, outcome))
        else:
            logger.warning("[v4-settle] failed did=%s res=%s" % (did, res))

'''

# ── CHANGE 1: publish 戻り did を _v4_pending に保持 ──────────────────────
_PUB_ANCHOR = (
    '        self._v4_pending[table_id] = {"side": side, "pattern_key": pk}\n'
    '        self._save_v4_state()\n'
    '        try:\n'
    '            self._publish_v4_decision(table_id, buf, side, pk)\n'
    '        except Exception as _v4pe:\n'
    '            logger.debug("[v4-publish] error: %s" % _v4pe)\n'
)
_PUB_INSERT = (
    '        self._v4_pending[table_id] = {"side": side, "pattern_key": pk}\n'
    '        self._save_v4_state()\n'
    '        try:\n'
    '            _v4_did = self._publish_v4_decision(table_id, buf, side, pk)\n'
    '            if _v4_did:\n'
    '                self._v4_pending[table_id]["did"] = _v4_did\n'
    '                self._v4_pending[table_id]["qpid"] = str(getattr(buf, "qpid_table_id", "") or table_id)\n'
    '                self._v4_pending[table_id]["table_name"] = str(buf.table_name or table_id)\n'
    '        except Exception as _v4pe:\n'
    '            logger.debug("[v4-publish] error: %s" % _v4pe)\n'
)

# ── CHANGE 2: resolve ブロックで settled post ───────────────────────────
_RES_ANCHOR = (
    '            pp["wins" if res == "WIN" else ("losses" if res == "LOSE" else "ties")] += 1\n'
    '            self._save_v4_state()\n'
)
_RES_INSERT = _RES_ANCHOR + (
    '            try:\n'
    '                self._settle_v4_decision(pend, outcome, new_hand)\n'
    '            except Exception as _v4se:\n'
    '                logger.debug("[v4-settle] error: %s" % _v4se)\n'
)

_METHOD_ANCHOR = "    def _save_v4_state(self):\n"


def apply(s: str) -> str:
    if "_settle_v4_decision" in s:
        return s  # idempotent
    if "_publish_v4_decision" not in s:
        raise SystemExit("PUBLISH_PATCH_NOT_APPLIED: _publish_v4_decision not found")
    for anchor in (_PUB_ANCHOR, _RES_ANCHOR, _METHOD_ANCHOR):
        n = s.count(anchor)
        if n != 1:
            raise SystemExit("ANCHOR_FAIL count=%d for %r" % (n, anchor[:60]))
    s = s.replace(_PUB_ANCHOR, _PUB_INSERT)
    s = s.replace(_RES_ANCHOR, _RES_INSERT)
    s = s.replace(_METHOD_ANCHOR, _SETTLE_METHOD + _METHOD_ANCHOR)
    return s


def main() -> int:
    s = open(F, encoding="utf-8").read()
    if "_settle_v4_decision" in s:
        print("ALREADY_PATCHED")
        return 0
    out = apply(s)
    open(F, "w", encoding="utf-8").write(out)
    print("PATCHED_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
