"""Pragmatic Play バカラテーブル内部調査ツール

lobby から1つのテーブルに入場し、BET UI 構造をキャプチャ:
  - iframe の URL / 構造
  - Player/Banker/Tie のBETエリア要素
  - チップ選択UI
  - 残高表示
  - BET確定ボタン
  - 履歴・大路・珠盤路の表示要素

出力: pragmatic_table_dumps/<tableId>/
  - table_url.txt
  - iframes.json
  - outer_html.html
  - inner_html.html (game iframe内)
  - ws_messages.jsonl
  - selectors.json (BET関連要素のヒット確認)
  - screenshot.png

Usage:
  python capture_pragmatic_table.py [TABLE_ID]
  デフォルト: 426 (Turbo Baccarat)
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

DEFAULT_TABLE_ID = "426"  # Turbo Baccarat (観測で shoe が保存できたテーブル)
LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"
PROFILE_DIR = BA_ROOT / "auth_state" / "camoufox_profile"

ws_msgs = []


def on_ws(ws):
    url = ws.url
    print(f"[WS OPEN] {url}")
    def handler(fd):
        try:
            p = fd.payload if hasattr(fd, "payload") else fd
            if isinstance(p, bytes):
                p = p.decode("utf-8", errors="replace")
            ws_msgs.append({"ts": datetime.now().isoformat(), "url": url, "payload": p[:3000]})
        except Exception:
            pass
    ws.on("framereceived", handler)


def main():
    table_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TABLE_ID
    out_dir = Path(__file__).parent / "pragmatic_table_dumps" / table_id
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Target table ID: {table_id}")
    print(f"Output: {out_dir}")

    with Camoufox(headless=False, persistent_context=True, user_data_dir=str(PROFILE_DIR)) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("websocket", on_ws)

        # ① Lobby に遷移
        print("Navigating to lobby...")
        page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # ② lobby 内で「テーブル」要素を探しクリック試行
        print("Looking for table entry points...")
        # Pragmatic のテーブルは <div class="game-card"> または <a href="...baccarat..."> で開く
        # 現時点では table_id での直接 deep link を試す
        # 試行1: gameLoaderKey / tableId を含む直接URL
        # lobby UI 経由で特定テーブルに入る方法はDOM解析が必要
        # 最も確実なのは任意のテーブルカードをクリック

        # スクショ保存
        page.screenshot(path=str(out_dir / "lobby.png"), full_page=False)
        print(f"Lobby screenshot saved")

        # テーブルカードを探す
        card_selectors = [
            'a[href*="baccarat"]',
            '[class*="game-card"]',
            '[data-testid*="game"]',
        ]
        clicked = False
        for sel in card_selectors:
            try:
                count = page.locator(sel).count()
                if count > 0:
                    print(f"  Selector {sel}: {count} hits")
                    # 最初の要素をクリック
                    page.locator(sel).first.click(timeout=5000)
                    clicked = True
                    print(f"  Clicked first {sel}")
                    break
            except Exception as e:
                print(f"  {sel}: {e}")

        if not clicked:
            print("No clickable table found, dumping lobby state only")
            page.wait_for_timeout(3000)
            html = page.content()
            (out_dir / "lobby_html.html").write_text(html, encoding="utf-8")
            return 0

        # ③ テーブル入場後の画面をキャプチャ
        print("Waiting for table UI to load...")
        page.wait_for_timeout(15000)
        print(f"URL after entry: {page.url}")

        # スクショ
        try:
            page.screenshot(path=str(out_dir / "table_entered.png"), full_page=False)
        except Exception as e:
            print(f"Screenshot failed: {e}")

        # iframe 情報
        frames_info = []
        for f in page.frames:
            frames_info.append({
                "url": f.url,
                "name": f.name,
                "is_main": f == page.main_frame,
            })
        (out_dir / "iframes.json").write_text(
            json.dumps(frames_info, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"iframes: {len(frames_info)}")
        for fi in frames_info:
            print(f"  - {fi['name'] or '(main)'}: {fi['url'][:100]}")

        # outer HTML
        (out_dir / "outer_html.html").write_text(page.content(), encoding="utf-8")

        # game iframe 内部の HTML (shell-app や game frame)
        game_frame = None
        for f in page.frames:
            if f.name in ("game", "shell-app") or "qpidreoxcc" in f.url or "pragmaticplaylive" in f.url:
                game_frame = f
                break

        if game_frame:
            print(f"Inspecting game frame: {game_frame.url[:80]}")
            try:
                inner_html = game_frame.content()
                (out_dir / f"inner_html_{game_frame.name or 'game'}.html").write_text(inner_html, encoding="utf-8")
                print(f"Inner HTML saved: {len(inner_html):,} chars")
            except Exception as e:
                print(f"Inner HTML failed: {e}")

            # BET UI 要素の探索
            probe_selectors = {
                # Playerエリア候補
                "player_area": [
                    '[data-role="player"]', '[data-spot="player"]',
                    '.player-spot', '[class*="player"][class*="area"]',
                    '[data-bet="player"]',
                ],
                # Banker
                "banker_area": [
                    '[data-role="banker"]', '[data-spot="banker"]',
                    '.banker-spot', '[class*="banker"][class*="area"]',
                    '[data-bet="banker"]',
                ],
                # Tie
                "tie_area": [
                    '[data-role="tie"]', '[data-spot="tie"]',
                    '.tie-spot', '[data-bet="tie"]',
                ],
                # チップ選択
                "chip": [
                    '[data-role="chip"]', '[data-value]',
                    '[class*="chip"]',
                ],
                # 残高
                "balance": [
                    '[class*="balance"]', '[data-role="balance"]',
                ],
                # BET確定
                "confirm": [
                    'button[class*="confirm"]', '[data-action="confirm"]',
                    'button:has-text("Confirm")', 'button:has-text("OK")',
                ],
                # 大路・罫線
                "scorecard": [
                    '[class*="scorecard"]', '[class*="bead-road"]', '[class*="big-road"]',
                    'canvas',
                ],
            }
            probe_results = {}
            for category, selectors in probe_selectors.items():
                for sel in selectors:
                    try:
                        count = game_frame.locator(sel).count()
                        if count > 0:
                            probe_results.setdefault(category, []).append({"sel": sel, "count": count})
                    except Exception:
                        pass
            (out_dir / "bet_ui_probe.json").write_text(
                json.dumps(probe_results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"BET UI probe: {sum(len(v) for v in probe_results.values())} matches")

        else:
            print("No game frame found, dumping all frame URLs for reference")

        # WS messages dump
        with open(out_dir / "ws_messages.jsonl", "w", encoding="utf-8") as f:
            for m in ws_msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"WS messages: {len(ws_msgs)}")

        (out_dir / "table_url.txt").write_text(page.url, encoding="utf-8")

        # もう少し待ってBET中の挙動も観察
        print("\nWaiting 30s to observe BETing phase WS traffic...")
        page.wait_for_timeout(30000)

        # 再dump (BETタイミングのWSを追加収集)
        with open(out_dir / "ws_messages.jsonl", "w", encoding="utf-8") as f:
            for m in ws_msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"Final WS messages: {len(ws_msgs)}")

    print(f"\n=== Done. Output: {out_dir} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
