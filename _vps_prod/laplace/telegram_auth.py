"""Telegram Bot経由でEmail認証コードを受け取るモジュール

フロー:
  1. 「📧 メール認証コードを送ってください」とTelegramに送信
  2. ユーザーがTelegramで数字を返信
  3. getUpdatesでポーリングして返信を取得
  4. コードを返す
"""
import time
import logging
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("baccarat.telegram_auth")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _send_message(text: str) -> bool:
    """Telegramにメッセージを送信"""
    try:
        resp = requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        logger.error(f"Telegram送信エラー: {e}")
        return False


def _get_updates(offset: int = 0, timeout: int = 30) -> list[dict]:
    """Telegramからアップデートを取得（ロングポーリング）"""
    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 10,
        )
        if resp.ok:
            data = resp.json()
            return data.get("result", [])
    except Exception as e:
        logger.debug(f"getUpdatesエラー: {e}")
    return []


def _flush_old_updates():
    """古い未処理のアップデートをフラッシュする"""
    updates = _get_updates(offset=0, timeout=0)
    if updates:
        last_id = updates[-1]["update_id"]
        # offset = last_id + 1 で古いメッセージを消費
        _get_updates(offset=last_id + 1, timeout=0)
        logger.debug(f"古いアップデート {len(updates)} 件をフラッシュ")
        return last_id + 1
    return 0


def ask_email_code(timeout: int = 300) -> str:
    """Telegram経由でメール認証コードを取得する

    Args:
        timeout: 最大待機時間（秒）。デフォルト300秒（5分）

    Returns:
        受信したコード文字列。タイムアウト時は空文字列
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram Bot設定がありません")
        return ""

    # 古いメッセージをフラッシュ
    next_offset = _flush_old_updates()

    # コード入力を依頼
    _send_message(
        "📧 Stake.comからメール認証コードが送信されました！\n\n"
        "メールを確認して、6桁のコードをここに送ってください。\n\n"
        "⏳ 5分以内にお願いします"
    )

    logger.info("Telegram経由でメール認証コードを待機中...")

    deadline = time.time() + timeout
    chat_id_str = str(TELEGRAM_CHAT_ID)

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll_timeout = min(15, remaining)  # 最大15秒のロングポーリング

        if poll_timeout <= 0:
            break

        updates = _get_updates(offset=next_offset, timeout=poll_timeout)

        for update in updates:
            next_offset = update["update_id"] + 1

            # メッセージを取得
            msg = update.get("message")
            if not msg:
                continue

            # 正しいチャットからのメッセージか確認
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))
            if msg_chat_id != chat_id_str:
                continue

            # テキストを取得
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            # 数字のみ（4〜8桁）を認証コードとして受け取る
            digits = "".join(c for c in text if c.isdigit())
            if 4 <= len(digits) <= 8:
                logger.info(f"認証コード受信: {digits}")
                _send_message(f"✅ コード {digits} を受け取りました。入力中...")
                return digits
            else:
                logger.debug(f"無視されたメッセージ: {text}")

        # 1分ごとにリマインダー
        if remaining > 0 and remaining % 60 < 16 and remaining < timeout - 10:
            logger.info(f"コード待機中... 残り{remaining}秒")

    # タイムアウト
    _send_message("⏰ 認証コードのタイムアウトです。再度ログインを試みてください。")
    logger.warning("認証コード待機タイムアウト")
    return ""
