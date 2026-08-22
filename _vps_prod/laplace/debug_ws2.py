"""WebSocketデバッグ v2 — テーブルに正しく入る

問題: ロビーのカードをクリックしてもテーブルに入れていない
対策: 正しいURLで直接ゲームテーブルに遷移する
"""
import os
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
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_ws2.log"),
            encoding="utf-8",
            mode="w",
        ),
    ],
)
logger = logging.getLogger("debug_ws2")

INTERESTING_KEYWORDS = [
    "player", "banker", "tie", "winner", "result", "outcome",
    "score", "baccarat", "round", "shoe", "deal", "card",
    "gameresult", "game_result", "roundresult",
]

ws_message_count = 0
interesting_count = 0


def on_ws_message(ws_url: str, payload: str):
    global ws_message_count, interesting_count
    ws_message_count += 1

    if len(payload) < 3:
        return

    truncated = payload[:800] if len(payload) > 800 else payload
    payload_lower = payload.lower()
    is_interesting = any(kw in payload_lower for kw in INTERESTING_KEYWORDS)

    if is_interesting:
        interesting_count += 1
        logger.info(f"[WS#{ws_message_count}] ★INTERESTING★ url={ws_url[:80]}")
        logger.info(f"  payload({len(payload)}chars): {truncated}")
    else:
        # 全メッセージもINFOで記録（最初の200文字）
        logger.info(f"[WS#{ws_message_count}] url={ws_url[:80]} len={len(payload)}")
        logger.debug(f"  payload: {truncated}")


def main():
    logger.info("=== WebSocketデバッグ v2 開始 ===")
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

        # WS傍受を先に設定（ページ遷移前に）
        def on_ws(ws: WebSocket):
            url = ws.url
            logger.info(f"🔌 新WebSocket接続: {url}")
            ws.on("framereceived", lambda payload: on_ws_message(url, payload))
            ws.on("close", lambda: logger.info(f"🔌 WebSocket切断: {url[:80]}"))

        page.on("websocket", on_ws)

        # Stakeにアクセス
        logger.info("Stakeにアクセス中...")
        page.goto(config.STAKE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        logger.info(f"ページタイトル: {page.title()}")

        # バカラロビーではなく、直接ゲームURLに行く
        # Stake.comのバカラゲームURLパターンを試す
        game_urls = [
            "https://stake.com/casino/games/evolution-baccarat",
            "https://stake.com/casino/games/evolution-baccarat-a",
            "https://stake.com/casino/games/evolution-japanese-baccarat",
            "https://stake.com/casino/games/evolution-speed-baccarat-a",
        ]

        # まずロビーに行ってリンクURLを取得
        logger.info("バカラロビーに移動して直接リンクを探す...")
        page.goto(config.BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)

        # 全てのゲームカードのリンクURLを収集
        links = page.evaluate("""
            () => {
                const results = [];
                const cards = document.querySelectorAll('.game-card-wrap a, a[href*="casino/games"]');
                cards.forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = a.textContent.trim().split('\\n')[0].trim();
                    if (href && text) {
                        results.push({href, text: text.substring(0, 80)});
                    }
                });
                return results.slice(0, 50);
            }
        """)
        logger.info(f"ゲームリンク一覧: {len(links)}件")
        baccarat_links = []
        for link in links:
            if 'baccarat' in link['href'].lower() or 'baccarat' in link['text'].lower():
                logger.info(f"  バカラ: {link['text']} → {link['href']}")
                baccarat_links.append(link)

        # Japanese Baccaratまたは最初のバカラゲームのURLに遷移
        target_url = None
        for link in baccarat_links:
            if 'japanese' in link['href'].lower():
                target_url = link['href']
                break
        if not target_url and baccarat_links:
            # Baccarat Lobbyを避けて最初の個別テーブルを選ぶ
            for link in baccarat_links:
                if 'lobby' not in link['href'].lower():
                    target_url = link['href']
                    break
            if not target_url:
                target_url = baccarat_links[0]['href']

        if target_url:
            if target_url.startswith('/'):
                target_url = f"https://stake.com{target_url}"
            logger.info(f"ゲームURLに遷移: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=90000)
            time.sleep(10)
        else:
            logger.error("バカラゲームURLが見つかりません")
            page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug2_no_url.png"))
            return

        logger.info(f"ゲームページタイトル: {page.title()}")
        page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug2_game_page.png"))

        # フレーム構造を確認
        frames = page.frames
        logger.info(f"フレーム数: {len(frames)}")
        for i, frame in enumerate(frames):
            logger.info(f"  Frame[{i}]: url={frame.url[:150]}")

        # Evolution iframeを探す
        evo_frame = None
        for frame in frames:
            if 'evolution' in frame.url.lower() or 'evo' in frame.url.lower():
                evo_frame = frame
                logger.info(f"Evolution iframe検出: {frame.url[:150]}")
                break

        # iframe内の内容を確認
        if evo_frame:
            try:
                html = evo_frame.content()[:3000]
                logger.info(f"Evolution iframe HTML: {html}")
            except Exception as e:
                logger.info(f"Evolution iframe HTML取得失敗: {e}")

        # ゲームが読み込まれるまで待つ
        logger.info("ゲーム読み込み待機中（20秒）...")
        time.sleep(20)

        # 再度フレームチェック
        frames = page.frames
        logger.info(f"フレーム数（再確認）: {len(frames)}")
        for i, frame in enumerate(frames):
            logger.info(f"  Frame[{i}]: url={frame.url[:150]}")

        page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug2_after_wait.png"))

        # 3分間メッセージ監視
        logger.info(f"=== 3分間WS監視開始 (現在のWS数: {ws_message_count}) ===")
        start_time = time.time()
        while time.time() - start_time < 180:
            elapsed = int(time.time() - start_time)
            if elapsed % 30 == 0 and elapsed > 0:
                logger.info(
                    f"--- {elapsed}秒経過: WS合計={ws_message_count}, "
                    f"興味深い={interesting_count} ---"
                )
            time.sleep(1)

        logger.info("=== デバッグ完了 ===")
        logger.info(f"合計WSメッセージ: {ws_message_count}")
        logger.info(f"興味深いメッセージ: {interesting_count}")

        page.screenshot(path=str(config.SCREENSHOTS_DIR / "debug2_final.png"))


if __name__ == "__main__":
    main()
