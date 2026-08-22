"""AI Vision module for LAPLACE baccarat bot.

Uses Anthropic Claude Haiku for real-time screen state detection.
- シャッフル検知 (BET前の安全確認)
- 観戦モード中の state 判定 (BB後の再開タイミング)
- エラー状態の識別 (TRY AGAIN / SESSION EXPIRED / iframe死亡)

設計方針:
- 既存コードに非侵襲 (AI が無効 = 完全に素通り)
- ANTHROPIC_API_KEY 環境変数で on/off
- レート制限 (最低2秒間隔) + キャッシュ (2秒)
- 例外時は None を返してfallback可能
"""
from __future__ import annotations

import os
import json
import base64
import logging
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger("baccarat.ai_vision")

# シングルトン
_client = None
_client_init_tried = False
_last_call_at: float = 0.0
_lock = Lock()

# 設定
MIN_CALL_INTERVAL = 2.0   # 最低2秒の間隔 (rate limit + コスト節約)
SCREENSHOT_TIMEOUT_MS = 5000
API_TIMEOUT = 10.0

# 直近のキャッシュ (短時間連続呼出での再利用)
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 2.0  # 2秒間キャッシュ


def is_enabled() -> bool:
    """AI Vision が使用可能か (環境変数 ANTHROPIC_API_KEY が設定されているか)"""
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _get_client():
    """遅延初期化でクライアント取得"""
    global _client, _client_init_tried
    if _client is not None:
        return _client
    if _client_init_tried:
        return None
    _client_init_tried = True
    if not is_enabled():
        return None
    try:
        import anthropic
        _client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            timeout=API_TIMEOUT,
        )
        logger.info("[ai_vision] Anthropic client initialized")
        return _client
    except ImportError:
        logger.error("[ai_vision] anthropic package not installed — disabling AI Vision")
        return None
    except Exception as e:
        logger.error(f"[ai_vision] client init failed: {e}")
        return None


def _rate_limit():
    """レート制限: 最低 MIN_CALL_INTERVAL 秒間隔"""
    global _last_call_at
    with _lock:
        elapsed = time.time() - _last_call_at
        if elapsed < MIN_CALL_INTERVAL:
            time.sleep(MIN_CALL_INTERVAL - elapsed)
        _last_call_at = time.time()


def take_screenshot(page) -> Optional[bytes]:
    """Playwright page からスクリーンショット取得

    page: scraper.page (Playwright Page object)
    Returns: PNG bytes, or None on failure
    """
    if page is None:
        return None
    try:
        return page.screenshot(type="png", full_page=False, timeout=SCREENSHOT_TIMEOUT_MS)
    except Exception as e:
        logger.warning(f"[ai_vision] screenshot failed: {e}")
        return None


def _extract_json(text: str) -> Optional[dict]:
    """AI レスポンスから JSON を抽出 (markdown fence 対応)"""
    text = text.strip()
    if text.startswith("```"):
        # ```json ... ``` を剥がす
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # JSON として解釈できない → 部分抽出試行
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


_PROMPT_GENERAL = """このEvolution バカラテーブルの画面を分析して JSON で回答してください。

判定項目:
1. state (現在の状態):
   - "betting_phase": ベッティングフェーズ (チップを置けるタイマー円形表示あり)
   - "shuffling": シャッフル中 / "Please wait" / "Shuffling" 表示
   - "dealing": カード配布中 (ディーラーがカードを扱っている)
   - "settled": 結果表示中 (勝敗表示)
   - "error_dialog": TRY AGAIN / BACK TO LOBBY / エラーダイアログ表示
   - "session_expired": SESSION EXPIRED / EV.5 / Authentication failed
   - "iframe_dead": 画面真っ白 / テーブル描画されず
   - "unknown": 判別不能

2. can_bet (今このタイミングで BET クリックすれば受理されるか): true/false

3. latest_result (直近のラウンドの結果、bead road から読み取れれば):
   - "P" (Player勝ち)
   - "B" (Banker勝ち)
   - "T" (Tie)
   - null (読めない/不明)

4. reason: 短い判定理由 (30文字以内)

JSON のみを返答してください (他の文章は不要):
{"state": "...", "can_bet": true/false, "latest_result": "P"|"B"|"T"|null, "reason": "..."}
"""


def check_game_state(screenshot_bytes: bytes, purpose: str = "general") -> Optional[dict]:
    """画面状態を AI で判定

    screenshot_bytes: PNG bytes (take_screenshot で取得)
    purpose: ログ用の用途タグ ("bet_check" / "observe" / "error_check")

    Returns:
      {
        "state": str,
        "can_bet": bool,
        "latest_result": str | None,
        "reason": str,
        "_purpose": str,
      }
      None: 失敗時 (呼出側で fallback ロジック発動)
    """
    if not screenshot_bytes:
        return None

    client = _get_client()
    if client is None:
        return None

    # キャッシュチェック
    cache_key = f"{purpose}_{len(screenshot_bytes)}"
    now = time.time()
    with _lock:
        if cache_key in _cache:
            cached_at, cached_result = _cache[cache_key]
            if now - cached_at < CACHE_TTL:
                return cached_result

    try:
        _rate_limit()
        b64 = base64.b64encode(screenshot_bytes).decode()

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _PROMPT_GENERAL},
                ],
            }],
        )

        text = response.content[0].text if response.content else ""
        parsed = _extract_json(text)
        if parsed is None:
            logger.warning(f"[ai_vision:{purpose}] JSON parse failed: {text[:200]}")
            return None

        parsed["_purpose"] = purpose
        state = parsed.get("state", "unknown")
        can_bet = parsed.get("can_bet", False)
        latest = parsed.get("latest_result")
        reason = str(parsed.get("reason", ""))[:60]
        logger.info(f"[ai_vision:{purpose}] state={state} can_bet={can_bet} latest={latest} reason={reason}")

        # キャッシュ保存
        with _lock:
            _cache[cache_key] = (now, parsed)

        return parsed
    except Exception as e:
        logger.warning(f"[ai_vision:{purpose}] API call failed: {e}")
        return None


def check_can_bet(page) -> Optional[bool]:
    """BET直前の判定ショートカット

    Returns:
      True: AI が BET OK と判断
      False: AI が NG と判断 (シャッフル中/エラー等)
      None: AI 無効 or 失敗 (fallback 呼出)
    """
    if not is_enabled():
        return None
    screenshot = take_screenshot(page)
    if not screenshot:
        return None
    result = check_game_state(screenshot, purpose="bet_check")
    if result is None:
        return None
    return bool(result.get("can_bet", False))


def check_observe_state(page) -> Optional[dict]:
    """観戦モード中の状態確認

    Returns: check_game_state の結果 (purpose="observe")
      呼出側は result['state'] / result['can_bet'] / result['latest_result'] を見る
    """
    if not is_enabled():
        return None
    screenshot = take_screenshot(page)
    if not screenshot:
        return None
    return check_game_state(screenshot, purpose="observe")


def identify_error(page) -> Optional[dict]:
    """エラー状態の識別

    Returns: check_game_state の結果 (purpose="error_check")
      state が "error_dialog" / "session_expired" / "iframe_dead" のいずれか
    """
    if not is_enabled():
        return None
    screenshot = take_screenshot(page)
    if not screenshot:
        return None
    return check_game_state(screenshot, purpose="error_check")


def clear_cache():
    """キャッシュクリア (状態変化が確実な時に呼ぶ)"""
    with _lock:
        _cache.clear()


# ─── シャッフル専用検知 (executor.is_shuffle_state Layer 2) ───
# DOM テキストでは取れない Canvas 描画のシャッフル画面を検知する。
# 5秒キャッシュで連続BET時のコストとレイテンシを抑える。
_shuffle_cache_at: float = 0.0
_shuffle_cache_result: bool = False
SHUFFLE_CACHE_TTL = 5.0   # 5秒キャッシュ

_PROMPT_SHUFFLE = """このEvolution バカラ画面はシャッフル中ですか?

シャッフル中の特徴:
- "Shuffling" / "Please wait" / "Dealer change" / "Shoe change" / "Be right back" 等の表示
- カードがディーラーによって混ぜられている
- ベットエリアが非アクティブで、チップを置けない
- カウントダウンタイマー (BETベッティングフェーズの円形タイマー) が無い

JSON のみで返答 (他の文章は不要):
{"shuffling": true/false, "reason": "短い理由 30文字以内"}
"""


def check_shuffle(page) -> bool:
    """シャッフル中か AI で判定 (5秒キャッシュ付き)

    executor.is_shuffle_state() の Layer 2 として呼ばれる。
    DOM テキスト検知が空振りした場合のみ呼び出される設計。

    Returns:
      True:  シャッフル中 → BET スキップすべき
      False: BET 可能 or 判定不能 (素通り)
    """
    global _shuffle_cache_at, _shuffle_cache_result

    if not is_enabled() or page is None:
        return False

    # キャッシュチェック (5秒以内なら再利用)
    now = time.time()
    with _lock:
        if now - _shuffle_cache_at < SHUFFLE_CACHE_TTL:
            return _shuffle_cache_result

    client = _get_client()
    if client is None:
        return False

    screenshot = take_screenshot(page)
    if not screenshot:
        return False

    try:
        _rate_limit()
        b64 = base64.b64encode(screenshot).decode()

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _PROMPT_SHUFFLE},
                ],
            }],
        )

        text = response.content[0].text if response.content else ""
        parsed = _extract_json(text)
        if parsed is None:
            logger.warning(f"[ai_vision:shuffle] JSON parse failed: {text[:100]}")
            return False

        shuffling = bool(parsed.get("shuffling", False))
        reason = str(parsed.get("reason", ""))[:60]
        logger.info(f"[ai_vision:shuffle] shuffling={shuffling} reason={reason}")

        # キャッシュ更新
        with _lock:
            _shuffle_cache_at = now
            _shuffle_cache_result = shuffling

        return shuffling
    except Exception as e:
        logger.warning(f"[ai_vision:shuffle] API call failed: {e}")
        return False
