"""Pragmatic Play バカラ 常駐データ収集ツール

動作:
  1. Camoufox で Stake.com Pragmatic Play バカラ lobby を開く
  2. wss://dga.pragmaticplaylive.net/ws を listen
  3. gameResult メッセージを tableId ごとにバッファリング
  4. shuffle=true で前シューを確定→DB保存、新シュー開始
  5. 無限ループ (WS切断時は goto で再接続試行)

Usage:
  python collector_pragmatic.py [--headless]
  python collector_pragmatic.py --duration 600   # 10分だけ走らせる (テスト用)
  python collector_pragmatic.py --raw-log        # ws_raw テーブルにも記録

Notes:
  - 初期起動時に各テーブルは shuffle=false の履歴を受信するが、
    これは「現在進行中のシュー」の過去ハンドで完全ではない可能性があるため、
    初回shuffle=trueを見るまではバッファを貯めるだけで保存しない。
  - 保存されるシューは session 内で「最初のshuffle→次のshuffle直前まで」が
    完全に揃ったもののみ。
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

from camoufox.sync_api import Camoufox  # type: ignore

import requests

from analytics_pragmatic_db import init_db, save_shoe, log_raw, stats
from snapshot_store import update_snapshot

LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"
# 収集器専用のCamoufoxプロファイル (LAPLACE Evolution側と競合しないよう分離)
DEFAULT_PROFILE = Path(__file__).parent / "auth_state" / "camoufox_profile_collector"
SOURCE_PROFILE = BA_ROOT / "auth_state" / "camoufox_profile"
PRAGMATIC_WS_PATTERN = "dga.pragmaticplaylive.net"
PRAGMATIC_LOBBY_PATTERN = "pragmaticplaylive"

# 最小有効シュー長 (これ未満ならゴミとみなしスキップ)
MIN_SHOE_HANDS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "collector_pragmatic.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("pragmatic.collector")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_qpid(value: str) -> str:
    if not value:
        return ""
    import re as _re
    m = _re.search(r"/snaps/([^/]+)/", value)
    if m:
        return str(m.group(1) or "")
    m = _re.search(r"[?&]tableId=([A-Za-z0-9]+)", value)
    if m:
        return str(m.group(1) or "")
    return ""


def _make_snapshot(table_id: str, buf: "ShoeBuffer") -> dict:
    last_hand = buf.hands[-1] if buf.hands else None
    return {
        "captured_at": _utc_now_iso(),
        "table_id": table_id,                   # operator_table_id (例 "415")
        "table_name": buf.table_name or "",
        "table_type": buf.table_type,
        "hands": len(buf.hands or []),
        "fresh_start": bool(buf.fresh_start),
        "statistics": buf.last_statistics,
        "shoe_summary": buf.last_shoe_summary,
        "good_roads_map": buf.last_good_roads,
        "last_hand": last_hand,
        # 直接 URL 遷移用の内部 ID (LAPLACE hash-nav 相当).
        "qpid_table_id": buf.qpid_table_id or "",
        "table_image": buf.table_image or "",
    }


class ShoeBuffer:
    """per-table シュー状態管理"""

    def __init__(self, table_id: str):
        self.table_id = table_id
        self.table_name: str = ""
        self.table_type: str | None = None
        self.hands: list[dict] = []
        self.game_ids: set[str] = set()
        self.fresh_start = False  # session内でshuffle=trueを見てから貯めているか
        self.last_statistics: str | None = None
        self.last_shoe_summary = None
        self.last_good_roads = None
        # Pragmatic internal ids (for direct-URL table entry)
        self.table_image: str = ""       # .../snaps/<qpid_id>/table.png
        self.qpid_table_id: str = ""     # internal id (e.g. "2q57e43m4ivqwaq3")

    def add_hands(self, new_hands: list[dict]) -> None:
        for h in new_hands:
            gid = h.get("gameId")
            if gid and gid not in self.game_ids:
                self.game_ids.add(gid)
                self.hands.append(h)

    def finalize(self) -> tuple[int, bool]:
        """現状のバッファをDBに保存し、リセット。Returns (shoe_id_or_0, saved)。

        初回shuffleでの buffer は「見えている範囲のシュー履歴」で、
        完全な shoe ではない可能性があるが、 MIN_SHOE_HANDS 以上の hands が
        入っていれば研究価値があるので保存する。
        """
        if len(self.hands) < MIN_SHOE_HANDS:
            self.hands.clear()
            self.game_ids.clear()
            return (0, False)
        shoe_id = save_shoe(
            table_id=self.table_id,
            table_name=self.table_name,
            table_type=self.table_type,
            hands=self.hands,
            statistics=self.last_statistics,
            shoe_summary=self.last_shoe_summary,
            good_roads_map=self.last_good_roads,
        )
        saved = shoe_id is not None
        self.hands.clear()
        self.game_ids.clear()
        return (shoe_id or 0, saved)

    def reset_fresh(self) -> None:
        self.fresh_start = True


class Collector:
    def __init__(self, headless: bool, raw_log: bool):
        self.headless = headless
        self.raw_log = raw_log
        self.buffers: dict[str, ShoeBuffer] = {}
        self.stop_flag = False
        self.stats_msg = 0
        self.stats_save = 0
        self.stats_shuffle = 0
        self.snapshot_push = (os.getenv("BACOPY_PUSH_SNAPSHOTS", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self.snapshot_api_url = (os.getenv("BACOPY_API_URL", "") or "").rstrip("/")
        self.snapshot_api_key = (os.getenv("BACOPY_API_KEY", "") or "").strip()
        self.snapshot_last_at: dict[str, float] = {}
        self.qpid_scan_interval_sec = int(os.getenv("BACOPY_QPID_SCAN_SEC", "3600") or "3600")
        self.last_qpid_scan_at = 0.0

    def _push_snapshot(self, table_id: str, snap: dict) -> None:
        if not (self.snapshot_push and self.snapshot_api_url and self.snapshot_api_key):
            return
        try:
            requests.post(
                f"{self.snapshot_api_url}/api/snapshots/update",
                headers={"Authorization": f"Bearer {self.snapshot_api_key}", "Content-Type": "application/json"},
                json={"provider": "pragmatic", "table_id": table_id, "snapshot": snap},
                timeout=5,
            )
        except Exception:
            return

    def _emit_snapshot(self, table_id: str, buf: ShoeBuffer) -> None:
        now = time.time()
        last = self.snapshot_last_at.get(table_id, 0.0)
        if now - last < 1.0:
            return
        snap = _make_snapshot(table_id, buf)
        update_snapshot("pragmatic", table_id, snap)
        self._push_snapshot(table_id, snap)
        self.snapshot_last_at[table_id] = now

    def _scan_lobby_qpid(self, page) -> None:
        """Lobby DOM から qpid_table_id を抽出して snapshot に反映。"""
        frames = []
        try:
            for f in page.frames:
                try:
                    u = str(getattr(f, "url", "") or "")
                    if PRAGMATIC_LOBBY_PATTERN in u and ("lobby" in u or "apps/lobby" in u):
                        frames.append(f)
                except Exception:
                    continue
        except Exception:
            pass
        if not frames:
            frames = [page]

        js = r"""
        () => {
          const rxSnap = /\/snaps\/([^/]+)\//;
          const rxTable = /[?&]tableId=([A-Za-z0-9]+)/;
          function pickQpid(v){
            if (!v) return '';
            const m = v.match(rxSnap) || v.match(rxTable);
            return m ? m[1] : '';
          }
          function textOf(el){
            const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
            return t.slice(0, 120);
          }
          const nodes = Array.from(document.querySelectorAll(
            '[data-table-id],[data-tableid],[data-table-name],[data-testid*="table"],[class*="table"],[class*="Table"],img,a,[style*="background-image"]'
          ));
          const out = [];
          const seen = new Set();
          function push(e){
            const key = [e.tableId||'', e.tableName||'', e.qpid||''].join('|');
            if (!e.qpid || seen.has(key)) return;
            seen.add(key);
            out.push(e);
          }
          for (const el of nodes) {
            const ds = el.dataset || {};
            const attrs = el.getAttributeNames ? el.getAttributeNames() : [];
            let tableId = ds.tableId || ds.tableid || ds.table || el.getAttribute('data-table-id') || el.getAttribute('data-tableid') || '';
            let tableName = ds.tableName || ds.tablename || el.getAttribute('data-table-name') || textOf(el);
            let qpid = ds.qpid || ds.tableQpid || '';
            let image = '';
            const cand = [];
            if (el.getAttribute) {
              for (const a of attrs) {
                const v = el.getAttribute(a);
                if (v) cand.push(String(v));
              }
              const style = el.getAttribute('style') || '';
              cand.push(style);
            }
            if (el.src) cand.push(String(el.src));
            if (el.href) cand.push(String(el.href));
            for (const v of cand) {
              if (!qpid) qpid = pickQpid(v);
              if (!image && /snaps\//.test(v)) image = v;
            }
            if (!qpid && tableName) {
              // try parent container if text matched
              let p = el.parentElement;
              let depth = 0;
              while (p && depth < 3 && !qpid) {
                const pv = p.getAttribute ? (p.getAttribute('style') || '') : '';
                qpid = pickQpid(pv) || qpid;
                depth += 1;
                p = p.parentElement;
              }
            }
            if (qpid) {
              push({ tableId: String(tableId || ''), tableName: String(tableName || ''), qpid: String(qpid), image: String(image || '') });
            }
          }
          return out;
        }
        """

        updated = 0
        total = 0
        for fr in frames:
            try:
                rows = fr.evaluate(js) or []
            except Exception:
                continue
            total += len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                qpid = _extract_qpid(str(row.get("qpid") or ""))
                if not qpid:
                    continue
                table_id = str(row.get("tableId") or "").strip()
                table_name = str(row.get("tableName") or "").strip()
                image = str(row.get("image") or "")

                buf = self.buffers.get(table_id) if table_id else None
                if not buf and table_name:
                    for b in self.buffers.values():
                        if b.table_name and b.table_name.strip().upper() == table_name.strip().upper():
                            buf = b
                            break
                if not buf or (buf.qpid_table_id and buf.qpid_table_id == qpid):
                    continue
                buf.qpid_table_id = qpid
                if image:
                    buf.table_image = image
                updated += 1
                logger.info(f"[qpid-scan] table_id={buf.table_id} name={buf.table_name} qpid={qpid}")
                try:
                    self._emit_snapshot(buf.table_id, buf)
                except Exception:
                    pass
        if total:
            logger.info(f"[qpid-scan] scanned={total} updated={updated} frames={len(frames)}")

    def on_ws_frame(self, payload):
        """WSフレーム受信コールバック。payload は bytes/str の可能性あり。"""
        self.stats_msg += 1
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            msg = json.loads(payload)
        except Exception:
            return

        if not isinstance(msg, dict):
            return

        table_id = msg.get("tableId")
        if not table_id:
            return

        buf = self.buffers.get(table_id)
        if not buf:
            buf = ShoeBuffer(table_id)
            self.buffers[table_id] = buf

        # metadata 更新
        if "tableName" in msg:
            buf.table_name = msg["tableName"]
        if "tableType" in msg:
            buf.table_type = msg["tableType"]
        if "statistics" in msg:
            buf.last_statistics = msg["statistics"]
        if "baccaratShoeSummary" in msg:
            buf.last_shoe_summary = msg["baccaratShoeSummary"]
        if "goodRoadsMap" in msg:
            buf.last_good_roads = msg["goodRoadsMap"]
        # tableImage: 内部 qpid id を取り出す (直接 URL 遷移用)
        if "tableImage" in msg:
            img = str(msg.get("tableImage") or "")
            if img and img != buf.table_image:
                buf.table_image = img
                import re as _re
                m = _re.search(r"/snaps/([^/]+)/", img)
                if m:
                    buf.qpid_table_id = str(m.group(1) or "")
                    logger.info(f"[qpid] captured table_id={table_id} qpid={buf.qpid_table_id} name={buf.table_name}")
        # 追加パターン: poster / image / thumbnail / backgroundImage なども qpid のソース候補
        for _k in ("posterImage", "posterUrl", "image", "thumbnail", "tableUrl", "backgroundImage"):
            if _k in msg and not buf.qpid_table_id:
                v = str(msg.get(_k) or "")
                import re as _re2
                mm = _re2.search(r"/snaps/([^/]+)/", v) or _re2.search(r"[?&]tableId=([A-Za-z0-9]+)", v)
                if mm:
                    buf.qpid_table_id = str(mm.group(1) or "")
                    buf.table_image = v
                    logger.info(f"[qpid] captured via {_k} table_id={table_id} qpid={buf.qpid_table_id}")
                    break
        # 最初の 3 メッセージの構造を log で可視化 (何が入っているか不明なので)
        if self.stats_msg <= 3:
            try:
                logger.info(f"[dga-msg-keys] msg #{self.stats_msg} keys={list(msg.keys())[:20]}")
            except Exception:
                pass

        # shuffle=True → 前シュー確定、新シュー開始
        if msg.get("shuffle") is True:
            self.stats_shuffle += 1
            shoe_id, saved = buf.finalize()
            if saved:
                self.stats_save += 1
                logger.info(
                    f"[SAVE] table={buf.table_name}({table_id}) hands_prev={len(msg.get('gameResult', []))} "
                    f"→ shoe_id={shoe_id} (total_saved={self.stats_save})"
                )
            buf.reset_fresh()

        # gameResult 追加
        gr = msg.get("gameResult")
        if isinstance(gr, list) and gr:
            buf.add_hands(gr)

        self._emit_snapshot(table_id, buf)

        if self.raw_log:
            log_raw(
                ts=datetime.now().isoformat(),
                table_id=table_id,
                msg_type=",".join(k for k in msg.keys() if k in ("shuffle", "gameResult", "statistics"))[:100],
                payload=payload,
            )

    def _on_ws(self, ws):
        url = ws.url
        if PRAGMATIC_WS_PATTERN not in url:
            return
        logger.info(f"[WS OPEN] {url}")

        def handler(frame_data):
            try:
                p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
                self.on_ws_frame(p)
            except Exception as e:
                logger.debug(f"frame handler error: {e}")

        ws.on("framereceived", handler)
        ws.on("close", lambda: logger.warning(f"[WS CLOSE] {url}"))

    def run(self, duration: int | None = None, profile_dir: Path | None = None,
            cookies_file: Path | None = None):
        import json as _json
        init_db()
        profile = profile_dir or DEFAULT_PROFILE
        profile.mkdir(parents=True, exist_ok=True)

        # 初回起動で profile が空ならソースからコピー (ローカル開発時のみ)
        is_empty = not any(profile.iterdir())
        if is_empty and SOURCE_PROFILE.exists():
            logger.info(f"Cloning profile {SOURCE_PROFILE} -> {profile}")
            import shutil
            # rmdir first then copytree
            profile.rmdir()
            shutil.copytree(str(SOURCE_PROFILE), str(profile))
        logger.info(f"DB initialized. Profile: {profile}")

        launch_opts = {
            "headless": self.headless,
            "persistent_context": True,
            "user_data_dir": str(profile),
        }

        def on_signal(signum, frame):
            logger.warning(f"Signal {signum} received, stopping...")
            self.stop_flag = True

        signal.signal(signal.SIGINT, on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_signal)

        start_ts = time.time()
        report_interval = 60

        with Camoufox(**launch_opts) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("websocket", self._on_ws)

            # VPS 運用パターン: stake_cookies.json があれば復元
            if cookies_file and cookies_file.exists():
                try:
                    with open(cookies_file) as cf:
                        cookies = _json.load(cf)
                    ctx.add_cookies(cookies)
                    logger.info(f"Restored {len(cookies)} cookies from {cookies_file}")
                except Exception as e:
                    logger.warning(f"Cookie restore failed: {e}")

            logger.info(f"Navigating to {LOBBY_URL}")
            page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            try:
                self._scan_lobby_qpid(page)
                self.last_qpid_scan_at = time.time()
            except Exception as e:
                logger.warning(f"[qpid-scan] initial scan failed: {e}")

            last_report = time.time()
            last_db_stats = time.time()
            while not self.stop_flag:
                # page.wait_for_timeout yields to Playwright event loop, letting
                # WebSocket frame handlers fire in real time (time.sleep would block them)
                page.wait_for_timeout(1000)
                now = time.time()
                if now - last_report >= report_interval:
                    elapsed = int(now - start_ts)
                    logger.info(
                        f"[STATUS] elapsed={elapsed}s  msgs={self.stats_msg}  "
                        f"shuffles={self.stats_shuffle}  saved={self.stats_save}  "
                        f"tables={len(self.buffers)}"
                    )
                    last_report = now
                if now - last_db_stats >= 300:
                    s = stats()
                    logger.info(f"[DB] {s}")
                    last_db_stats = now
                if self.qpid_scan_interval_sec > 0 and now - self.last_qpid_scan_at >= self.qpid_scan_interval_sec:
                    try:
                        self._scan_lobby_qpid(page)
                    except Exception as e:
                        logger.warning(f"[qpid-scan] scan failed: {e}")
                    self.last_qpid_scan_at = now
                if duration and (now - start_ts) >= duration:
                    logger.info(f"Duration {duration}s reached, stopping.")
                    break

        logger.info(f"Final: msgs={self.stats_msg}  saved={self.stats_save}")
        logger.info(f"DB stats: {stats()}")
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="ヘッドレスモードで起動")
    parser.add_argument("--duration", type=int, help="指定秒数で自動停止 (テスト用)")
    parser.add_argument("--raw-log", action="store_true", help="全WSメッセージをDBに記録 (デバッグ用)")
    parser.add_argument("--profile", type=str, help="Camoufoxプロファイルパス (デフォルト: auth_state/camoufox_profile_collector)")
    parser.add_argument("--cookies", type=str, help="stake_cookies.json パス (VPS運用: 起動時にcookie復元)")
    args = parser.parse_args()

    c = Collector(headless=args.headless, raw_log=args.raw_log)
    profile = Path(args.profile) if args.profile else None
    cookies = Path(args.cookies) if args.cookies else None
    return c.run(duration=args.duration, profile_dir=profile, cookies_file=cookies)


if __name__ == "__main__":
    sys.exit(main())
