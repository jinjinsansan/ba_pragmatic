"""Pragmatic Play Baccarat lobby 調査用キャプチャツール

使い方:
  python capture_pragmatic.py

動作:
  1. Camoufox を既存のauth_state (ba/ 側) で起動
  2. Pragmatic Play バカラ lobby に移動
  3. 60秒間 WebSocketメッセージを全てキャプチャ
  4. ページ HTML・URL・iframe情報を dump
  5. pragmatic_dumps/ に保存

出力ファイル:
  pragmatic_dumps/page_url.txt        — 最終URL
  pragmatic_dumps/page_html.html      — 完全なHTML
  pragmatic_dumps/iframes.json        — iframe 一覧
  pragmatic_dumps/ws_messages.jsonl   — WebSocketメッセージ (JSONL)
  pragmatic_dumps/ws_urls.txt         — 接続されたWebSocket URL一覧
  pragmatic_dumps/console.log         — ブラウザコンソールログ
"""
import json
import os
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

# ba/ のモジュールを流用 (scraper が必要とする依存)
BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

from camoufox.sync_api import Camoufox  # type: ignore

DUMPS_DIR = Path(__file__).parent / "pragmatic_dumps"
DUMPS_DIR.mkdir(exist_ok=True)

PROFILE_DIR = BA_ROOT / "auth_state" / "camoufox_profile"
LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"
CAPTURE_SECONDS = 60

# 収集バッファ
ws_messages = []
ws_urls = set()
console_logs = []
lock = threading.Lock()


def on_ws(ws):
    """WebSocket接続を検知"""
    url = ws.url
    print(f"[WS OPEN] {url}")
    with lock:
        ws_urls.add(url)

    def on_frame(frame_data, direction):
        try:
            payload = frame_data.payload if hasattr(frame_data, 'payload') else str(frame_data)
            if isinstance(payload, bytes):
                try:
                    payload = payload.decode('utf-8')
                except:
                    payload = payload.hex()
        except Exception as e:
            payload = f"<parse error: {e}>"
        entry = {
            'ts': datetime.now().isoformat(),
            'url': url,
            'dir': direction,
            'payload': payload[:5000],  # 長すぎるpayloadはtruncate
            'payload_len': len(str(payload)),
        }
        with lock:
            ws_messages.append(entry)

    ws.on('framereceived', lambda f: on_frame(f, 'recv'))
    ws.on('framesent', lambda f: on_frame(f, 'send'))
    ws.on('close', lambda: print(f"[WS CLOSE] {url}"))


def on_console(msg):
    entry = f"[{msg.type}] {msg.text}"
    with lock:
        console_logs.append(entry)


def main():
    print(f"Profile: {PROFILE_DIR}")
    print(f"Target:  {LOBBY_URL}")
    print(f"Dumps:   {DUMPS_DIR}\n")

    if not PROFILE_DIR.exists():
        print(f"ERROR: Camoufox profile not found at {PROFILE_DIR}")
        print("Evolution側でログイン済みのprofileが必要です。")
        return 1

    launch_opts = {
        "headless": False,  # 目視確認のため表示
        "persistent_context": True,
        "user_data_dir": str(PROFILE_DIR),
    }

    with Camoufox(**launch_opts) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # WebSocket + Console listener を最初に登録 (login前の接続も捕捉)
        page.on('websocket', on_ws)
        page.on('console', on_console)

        print(f"Navigating to {LOBBY_URL}...")
        page.goto(LOBBY_URL, wait_until='domcontentloaded', timeout=60000)
        print(f"Current URL: {page.url}")

        print(f"\nCapturing WebSocket traffic for {CAPTURE_SECONDS}s...")
        for i in range(CAPTURE_SECONDS):
            time.sleep(1)
            if (i + 1) % 10 == 0:
                print(f"  {i+1}s / WS msgs: {len(ws_messages)} / URLs: {len(ws_urls)}")

        # Dump 最終ページ情報
        final_url = page.url
        (DUMPS_DIR / "page_url.txt").write_text(final_url, encoding='utf-8')
        print(f"\nFinal URL: {final_url}")

        # HTML
        try:
            html = page.content()
            (DUMPS_DIR / "page_html.html").write_text(html, encoding='utf-8')
            print(f"HTML saved: {len(html):,} chars")
        except Exception as e:
            print(f"HTML dump failed: {e}")

        # iframe 一覧
        frames_info = []
        for f in page.frames:
            frames_info.append({
                'url': f.url,
                'name': f.name,
                'is_main': f == page.main_frame,
            })
        (DUMPS_DIR / "iframes.json").write_text(
            json.dumps(frames_info, indent=2, ensure_ascii=False), encoding='utf-8'
        )
        print(f"iframes: {len(frames_info)}")

        # WS messages
        with open(DUMPS_DIR / "ws_messages.jsonl", 'w', encoding='utf-8') as f:
            for m in ws_messages:
                f.write(json.dumps(m, ensure_ascii=False) + '\n')
        print(f"WS messages: {len(ws_messages)}")

        # WS URLs
        (DUMPS_DIR / "ws_urls.txt").write_text('\n'.join(sorted(ws_urls)), encoding='utf-8')
        print(f"WS URLs: {len(ws_urls)}")

        # Console
        (DUMPS_DIR / "console.log").write_text('\n'.join(console_logs), encoding='utf-8')
        print(f"Console entries: {len(console_logs)}")

        # テーブル要素の候補探索 (HTML内のゲームカード等)
        try:
            selectors = [
                '[data-testid*="game"]',
                '[data-testid*="table"]',
                '[class*="table"]',
                '[class*="game-card"]',
                'a[href*="baccarat"]',
            ]
            selector_hits = {}
            for sel in selectors:
                try:
                    count = page.locator(sel).count()
                    selector_hits[sel] = count
                except:
                    selector_hits[sel] = 'ERR'
            (DUMPS_DIR / "selectors.json").write_text(
                json.dumps(selector_hits, indent=2), encoding='utf-8'
            )
            print(f"Selector hits: {selector_hits}")
        except Exception as e:
            print(f"Selector probe failed: {e}")

        print(f"\n=== Done. Output: {DUMPS_DIR} ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())
