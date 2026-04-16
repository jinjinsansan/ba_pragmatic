"""Pragmatic Play バカラテーブル内部調査ツール v2

v1 は「Pragmatic 内部ロビー」までしか到達できなかった。
v2 は、内部ロビーから更に特定テーブルに入場する:
  Stake main → game iframe (pragmatic shell) → shell-app iframe (lobby)
     ↓ (shell-app内で table card をクリック)
  → 実テーブルUI (新規 iframe か 同iframe内の差し替え)

戦略:
  1. Stake.com lobby → Pragmatic lobby URL
  2. 15秒待機 (Pragmatic shell + lobby iframe のロード完了待ち)
  3. shell-app frame にアクセス (qpidreoxcc.net/apps/lobby/index.html)
  4. そのiframe内でテーブルカード要素を探してクリック
  5. テーブル入場後のWS + DOM + 30秒トラフィックをダンプ

Usage:
  python capture_pragmatic_table_v2.py [table_name_substring]
  デフォルト: "turbo" (Turbo Baccarat 等に一致)
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

from camoufox.sync_api import Camoufox  # type: ignore

TARGET_SUBSTR = sys.argv[1].lower() if len(sys.argv) > 1 else "turbo"
LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"
PROFILE_DIR = BA_ROOT / "auth_state" / "camoufox_profile"

ws_msgs: list[dict] = []


def on_ws(ws):
    url = ws.url
    print(f"[WS OPEN] {url}")

    def handler(fd):
        try:
            p = fd.payload if hasattr(fd, "payload") else fd
            if isinstance(p, bytes):
                p = p.decode("utf-8", errors="replace")
            ws_msgs.append({"ts": datetime.now().isoformat(), "url": url, "dir": "recv", "payload": p[:3000]})
        except Exception:
            pass

    def sender(fd):
        try:
            p = fd.payload if hasattr(fd, "payload") else fd
            if isinstance(p, bytes):
                p = p.decode("utf-8", errors="replace")
            ws_msgs.append({"ts": datetime.now().isoformat(), "url": url, "dir": "send", "payload": p[:3000]})
        except Exception:
            pass

    ws.on("framereceived", handler)
    ws.on("framesent", sender)


def find_game_frame(page, attempts=30):
    """pragmaticplaylive 系の game frame を探す"""
    for i in range(attempts):
        for f in page.frames:
            if "qpidreoxcc.net" in f.url or "pragmaticplaylive" in f.url:
                return f
        page.wait_for_timeout(1000)
    return None


def find_shell_app_frame(page, attempts=30):
    """shell-app (lobby iframe) を探す"""
    for i in range(attempts):
        for f in page.frames:
            if "apps/lobby" in f.url or f.name == "shell-app":
                return f
        page.wait_for_timeout(1000)
    return None


def dump_all(out_dir, page, stage):
    """現在のページ状態を全dumpする補助"""
    (out_dir / f"{stage}_url.txt").write_text(page.url, encoding="utf-8")
    try:
        page.screenshot(path=str(out_dir / f"{stage}_screenshot.png"), full_page=False)
    except Exception as e:
        print(f"  screenshot {stage}: {e}")
    frames = []
    for f in page.frames:
        frames.append({"name": f.name, "url": f.url, "is_main": f == page.main_frame})
    (out_dir / f"{stage}_frames.json").write_text(
        json.dumps(frames, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main():
    out_dir = Path(__file__).parent / "pragmatic_table_dumps" / f"v2_{TARGET_SUBSTR}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Target substring: {TARGET_SUBSTR}")
    print(f"Output: {out_dir}\n")

    with Camoufox(headless=False, persistent_context=True, user_data_dir=str(PROFILE_DIR)) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("websocket", on_ws)

        # Stage 1: Stakeロビー → Pragmaticロビー
        print("[Stage 1] Navigating to Pragmatic lobby...")
        page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)
        dump_all(out_dir, page, "s1_stake_lobby")

        # Stage 2: Pragmatic shell ロード待ち
        print("[Stage 2] Waiting for Pragmatic shell (qpidreoxcc.net)...")
        game_frame = find_game_frame(page)
        if not game_frame:
            print("  FAIL: Pragmatic shell not found after 30s. Dumping and exiting.")
            dump_all(out_dir, page, "s2_fail")
            return 1
        print(f"  Game frame loaded: {game_frame.url[:100]}")
        page.wait_for_timeout(5000)  # shell-app 初期化待ち
        dump_all(out_dir, page, "s2_game_shell")

        # Stage 3: shell-app (= 内部ロビー iframe) を探す
        print("[Stage 3] Looking for shell-app (internal Pragmatic lobby)...")
        shell = find_shell_app_frame(page)
        if not shell:
            print("  FAIL: shell-app not found. Dumping and exiting.")
            dump_all(out_dir, page, "s3_fail")
            return 1
        print(f"  Shell-app frame: {shell.url[:120]}")
        page.wait_for_timeout(5000)

        # Stage 4: 内部ロビー内の DOM を観察し、テーブルカードを探す
        print("[Stage 4] Probing shell-app DOM for table cards...")
        try:
            shell_html = shell.content()
            (out_dir / "s4_shell_html.html").write_text(shell_html, encoding="utf-8")
            print(f"  Shell HTML: {len(shell_html):,} chars")
        except Exception as e:
            print(f"  Shell HTML failed: {e}")

        # 複数のselectorで table card を試す
        card_selectors = [
            # Pragmatic UIの一般的パターン
            '[data-testid*="table"]',
            '[data-test*="table"]',
            '[data-id]',
            '[data-table-id]',
            '[role="button"]',
            'button',
            'div[class*="tile"]',
            'div[class*="card"]',
            'div[class*="table-item"]',
            'div[class*="game-tile"]',
            'a',
            'img',
        ]
        selector_hits = {}
        for sel in card_selectors:
            try:
                count = shell.locator(sel).count()
                selector_hits[sel] = count
            except Exception:
                selector_hits[sel] = "ERR"
        (out_dir / "s4_selectors.json").write_text(
            json.dumps(selector_hits, indent=2), encoding="utf-8"
        )
        print(f"  Selector hits: {selector_hits}")

        # Stage 5: TARGET_SUBSTR に一致するテキストをクリック試行
        print(f"\n[Stage 5] Searching for element with text containing '{TARGET_SUBSTR}'...")
        clicked = False
        try:
            # テキスト一致で選択 (大文字小文字無視)
            loc = shell.get_by_text(TARGET_SUBSTR, exact=False).first
            if loc.count() > 0:
                loc.scroll_into_view_if_needed(timeout=5000)
                loc.click(timeout=5000)
                clicked = True
                print(f"  Clicked element containing '{TARGET_SUBSTR}'")
        except Exception as e:
            print(f"  get_by_text failed: {e}")

        if not clicked:
            # フォールバック: 最初のbutton/card
            for sel in ['div[class*="tile"]', 'div[class*="card"]', 'button', '[role="button"]']:
                try:
                    if shell.locator(sel).count() > 0:
                        shell.locator(sel).first.click(timeout=5000)
                        clicked = True
                        print(f"  Fallback: clicked first {sel}")
                        break
                except Exception:
                    continue

        if not clicked:
            print("  Could not click any table. Dumping shell content for analysis.")
            dump_all(out_dir, page, "s5_no_click")
            return 1

        # Stage 6: クリック後、新しい frame or URL 変化を観察
        print("\n[Stage 6] Observing state after table entry click...")
        page.wait_for_timeout(15000)
        dump_all(out_dir, page, "s6_after_click")

        # 再度 frame 列挙
        current_frames = [f.url for f in page.frames]
        print(f"  Frames after click ({len(current_frames)}):")
        for u in current_frames:
            print(f"    - {u[:120]}")

        # game frame の内部 HTML を再取得
        game_frame = find_game_frame(page, attempts=5)
        if game_frame:
            try:
                inner = game_frame.content()
                (out_dir / "s6_game_inner.html").write_text(inner, encoding="utf-8")
                print(f"  Game frame content: {len(inner):,} chars")

                # BET UI 要素探索
                bet_selectors = [
                    '[data-role="player"]', '[data-role="banker"]', '[data-role="tie"]',
                    '[data-spot="player"]', '[data-spot="banker"]',
                    'canvas',
                    '[class*="bet-spot"]', '[class*="betSpot"]',
                    '[class*="chip"]', '[data-role="chip"]',
                    'button',
                ]
                hits = {}
                for sel in bet_selectors:
                    try:
                        hits[sel] = game_frame.locator(sel).count()
                    except Exception:
                        pass
                (out_dir / "s6_bet_probe.json").write_text(
                    json.dumps(hits, indent=2), encoding="utf-8"
                )
                print(f"  BET UI probe: {hits}")
            except Exception as e:
                print(f"  inner content failed: {e}")

        # Stage 7: 30秒間 WS + 追加dumpを観察
        print("\n[Stage 7] Capturing 30s of WS traffic during table session...")
        for i in range(3):
            page.wait_for_timeout(10000)
            print(f"  {(i+1)*10}s / WS msgs total: {len(ws_msgs)}")

        # WS messages 保存
        with open(out_dir / "ws_messages.jsonl", "w", encoding="utf-8") as f:
            for m in ws_msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"\nFinal WS messages: {len(ws_msgs)}")

        # 最終dump
        dump_all(out_dir, page, "s7_final")

        # 送信WSの要約 (BETコマンドのヒント)
        sent = [m for m in ws_msgs if m.get("dir") == "send" and "pragmaticplaylive" in m.get("url", "")]
        print(f"\nPragmatic WS send count: {len(sent)}")
        if sent:
            print("Send samples (first 5):")
            for m in sent[:5]:
                print(f"  {m['payload'][:200]}")

    print(f"\n=== Done. Output: {out_dir} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
