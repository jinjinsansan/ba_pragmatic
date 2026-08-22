"""WebSocketメッセージのデバッグ用スクリプト

テーブルに接続し、受信する全WSメッセージをログに記録する。
結果パターンの分析用。
"""
import os
import sys
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta

from camoufox.sync_api import Camoufox
from playwright.sync_api import WebSocket

import config
from db import init_db

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_ws.log"),
            encoding="utf-8",
            mode="w",
        ),
    ],
)
logger = logging.getLogger("debug_ws")

# 興味のあるキーワード（バカラ結果に関連しそうなもの）
INTERESTING_KEYWORDS = [
    "player", "banker", "tie", "winner", "result", "outcome",
    "score", "baccarat", "round", "shoe", "deal", "card",
    "gameresult", "game_result", "roundresult",
    "bead", "road", "hand",
]

ws_message_count = 0
interesting_count = 0

# iframeのWSメッセージを別途記録
iframe_ws_count = 0


def on_ws_message(ws_url: str, payload: str):
    """全WSメッセージを記録"""
    global ws_message_count, interesting_count
    ws_message_count += 1

    # メッセージが短すぎる場合はスキップ（ping/pong等）
    if len(payload) < 5:
        return

    # 全メッセージをDEBUGレベルでログ
    truncated = payload[:500] if len(payload) > 500 else payload
    
    # 興味のあるキーワードが含まれているかチェック
    payload_lower = payload.lower()
    is_interesting = any(kw in payload_lower for kw in INTERESTING_KEYWORDS)

    if is_interesting:
        interesting_count += 1
        logger.info(f"[WS#{ws_message_count}] ★INTERESTING★ url={ws_url[:80]}")
        logger.info(f"  payload({len(payload)}chars): {truncated}")
        
        # JSONとしてパースを試みる
        try:
            # Socket.IO形式を処理
            match = re.match(r'^\d+\["([^"]+)",\s*(.+)\]$', payload, re.DOTALL)
            if match:
                event_name = match.group(1)
                data_str = match.group(2)
                logger.info(f"  Socket.IO event: {event_name}")
                try:
                    data = json.loads(data_str)
                    logger.info(f"  parsed data: {json.dumps(data, indent=2)[:1000]}")
                except:
                    logger.info(f"  raw data: {data_str[:500]}")
            else:
                data = json.loads(payload)
                logger.info(f"  parsed JSON: {json.dumps(data, indent=2)[:1000]}")
        except:
            pass
    else:
        # 非興味メッセージは短縮して記録
        if ws_message_count % 50 == 0:
            logger.debug(f"[WS#{ws_message_count}] (non-interesting) len={len(payload)} url={ws_url[:60]}")


def main():
    global iframe_ws_count
    
    logger.info("=== WebSocketデバッグモード開始 ===")
    init_db()

    logger.info("Camoufox起動中...")
    # Windows Store版Python対応
    launch_opts = {"headless": True}
    try:
        from camoufox.pkgman import get_path, LAUNCH_FILE, OS_NAME
        path = get_path(LAUNCH_FILE[OS_NAME])
        real = os.path.realpath(path)
        if real != path and os.path.isfile(real):
            launch_opts["executable_path"] = real
    except Exception:
        pass

    with Camoufox(**launch_opts) as browser:
        page = browser.new_page()

        # Cookie復元
        cookie_file = config.AUTH_STATE_DIR / "stake_cookies.json"
        if cookie_file.exists():
            with open(cookie_file) as f:
                cookies = json.load(f)
            page.context.add_cookies(cookies)
            logger.info(f"Cookie復元: {len(cookies)}件")

        # ログイン
        logger.info("Stakeにアクセス中...")
        page.goto(config.STAKE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        logger.info(f"ページタイトル: {page.title()}")

        # ログイン確認
        indicators = page.locator(
            '[data-test="balance"], '
            'button:has-text("Wallet"), '
            '[class*="balance"], '
            '[class*="user-menu"]'
        )
        if indicators.count() > 0:
            logger.info("ログイン済み ✅")
        else:
            logger.warning("ログインしていない可能性あり")

        # バカラロビーに移動
        logger.info("バカラロビーに移動中...")
        page.goto(config.BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)
        logger.info(f"バカラロビー到着 — タイトル: {page.title()}")

        # テーブルを選択
        target_tables = ["Japanese Baccarat", "Baccarat", "Speed Baccarat A"]
        selected = False
        for tname in target_tables:
            elem = page.locator(f'text="{tname}"')
            if elem.count() > 0 and elem.first.is_visible():
                elem.first.click()
                logger.info(f"✅ テーブル選択: {tname}")
                time.sleep(8)
                selected = True
                break
        
        if not selected:
            logger.error("テーブルが見つかりません")
            page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug_no_table.png"))
            return

        # 現在のページ構造を確認
        logger.info("=== ページ構造の確認 ===")
        
        # iframeの確認
        frames = page.frames
        logger.info(f"フレーム数: {len(frames)}")
        for i, frame in enumerate(frames):
            logger.info(f"  Frame[{i}]: url={frame.url[:120]}")

        # スクリーンショット
        page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug_table.png"))

        # WebSocket傍受設定
        logger.info("=== WebSocket傍受を設定 ===")

        def on_ws(ws: WebSocket):
            url = ws.url
            logger.info(f"🔌 新WebSocket接続: {url}")

            def on_frame_received(payload: str):
                on_ws_message(url, payload)

            def on_close():
                logger.info(f"🔌 WebSocket切断: {url[:80]}")

            ws.on("framereceived", on_frame_received)
            ws.on("close", on_close)

        page.on("websocket", on_ws)

        # iframeのWebSocketも傍受
        for i, frame in enumerate(frames):
            if frame == page.main_frame:
                continue
            try:
                frame_page = frame.page
                if frame_page:
                    frame_page.on("websocket", on_ws)
                    logger.info(f"  iframe[{i}]のWS傍受も設定")
            except Exception as e:
                logger.debug(f"  iframe[{i}]のWS傍受設定失敗: {e}")

        logger.info("WebSocket傍受設定完了 — 3分間メッセージを監視します...")

        # 3分間メッセージを監視
        start_time = time.time()
        while time.time() - start_time < 180:
            elapsed = int(time.time() - start_time)
            if elapsed % 30 == 0 and elapsed > 0:
                logger.info(
                    f"--- {elapsed}秒経過: WS合計={ws_message_count}, "
                    f"興味深い={interesting_count} ---"
                )
            time.sleep(1)

        # 最終レポート
        logger.info("=== デバッグ完了 ===")
        logger.info(f"合計WSメッセージ: {ws_message_count}")
        logger.info(f"興味深いメッセージ: {interesting_count}")
        
        # DOMからの結果取得も試す
        logger.info("=== DOM結果取得テスト ===")
        for i, frame in enumerate(frames):
            url = frame.url.lower()
            if "evolution" in url or "evo" in url:
                logger.info(f"Evolution iframe検出: Frame[{i}] {frame.url[:120]}")
                try:
                    html = frame.content()[:2000]
                    logger.info(f"  HTML(先頭2000文字): {html}")
                except Exception as e:
                    logger.info(f"  HTML取得失敗: {e}")
        
        # メインページのDOMも確認
        logger.info("=== メインページDOM確認 ===")
        try:
            # バカラ関連の要素を探す
            bac_elements = page.evaluate("""
                () => {
                    const results = [];
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const cls = el.className || '';
                        const id = el.id || '';
                        const tag = el.tagName;
                        if (typeof cls === 'string' && 
                            (cls.toLowerCase().includes('road') || 
                             cls.toLowerCase().includes('bead') ||
                             cls.toLowerCase().includes('result') ||
                             cls.toLowerCase().includes('score') ||
                             cls.toLowerCase().includes('card'))) {
                            results.push({
                                tag: tag,
                                class: cls.substring(0, 100),
                                id: id,
                                text: el.textContent.substring(0, 100)
                            });
                        }
                    }
                    return results.slice(0, 30);
                }
            """)
            if bac_elements:
                logger.info(f"バカラ関連DOM要素: {len(bac_elements)}件")
                for el in bac_elements:
                    logger.info(f"  {el}")
            else:
                logger.info("バカラ関連DOM要素: なし")
        except Exception as e:
            logger.info(f"DOM確認エラー: {e}")

        page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug_final.png"))
        logger.info("デバッグスクリプト終了")


if __name__ == "__main__":
    main()
