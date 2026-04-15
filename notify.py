"""Telegram通知

3種類のNotifier:
- TelegramNotifier: 基底クラス (従来互換のためエイリアス)
- PublicNotifier: 検証用公開チャンネル (シュー進行、セット完了、利確の宣伝投稿)
- AdminNotifier: 管理者プライベート (エラー、損切り警告、ユーザー監視)
- UserNotifier: 個別ユーザー通知 (既存のユーザー別通知)
"""
import logging
import os
import socket
import requests
from datetime import datetime

logger = logging.getLogger("baccarat.notify")


class TelegramNotifier:
    """基底クラス: Telegram BOT への生送信機能"""

    def __init__(self, bot_token: str, chat_id: str, label: str = "notify"):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.label = label
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.info(f"Telegram [{label}] 無効 (設定なし)")
        else:
            logger.info(f"Telegram [{label}] 有効 chat={chat_id}")

    def send(self, message: str, parse_mode: str = ""):
        if not self.enabled:
            logger.info(f"[{self.label}] {message}")
            return
        try:
            payload = {"chat_id": self.chat_id, "text": message}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json=payload,
                timeout=10,
            )
            if not resp.ok:
                logger.error(f"Telegram [{self.label}] 送信エラー: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"Telegram [{self.label}] 送信エラー: {e}")

    def notify_startup(self, table_name: str):
        self.send(
            f"🃏 バカラモニター起動\n"
            f"テーブル: {table_name}"
        )

    def notify_shutdown(self, reason: str = ""):
        self.send(f"⛔ バカラモニター停止{': ' + reason if reason else ''}")

    # === BET通知 (既存互換) ===

    def notify_bet_placed(self, bet_info: dict):
        side_emoji = "🔵" if bet_info["side"] == "player" else "🔴"
        side_name = "Player" if bet_info["side"] == "player" else "Banker"
        self.send(
            f"🎯 BET実行\n"
            f"📍 {bet_info.get('table_name', '')}\n"
            f"{side_emoji} {side_name} ${bet_info.get('amount', 0):.2f}"
        )

    def notify_bet_result(self, bet_result: dict):
        result = bet_result.get("result", "")
        profit = bet_result.get("profit", 0)
        emoji = "✅" if result == "win" else "❌" if result == "lose" else "➖"
        self.send(
            f"{emoji} BET結果: {result.upper()}\n"
            f"📍 {bet_result.get('table_name', '')}\n"
            f"💰 収支: ${profit:+.2f}"
        )


# ==========================================================
#  PublicNotifier — 検証用公開チャンネル向け
#  (ロジックが機能していることを公に証明する、宣伝・購読用)
# ==========================================================

class PublicNotifier(TelegramNotifier):
    """公開チャンネル向け。エンドユーザー個人情報は絶対に含めない。"""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        token = bot_token or os.getenv("PUBLIC_BOT_TOKEN", "")
        chat = chat_id or os.getenv("PUBLIC_CHANNEL_ID", "")
        super().__init__(token, chat, label="PUBLIC")

    def notify_verification_start(self, table_name: str):
        self.send(
            f"━━━━━━━━━━━━━━━━\n"
            f"∫ LAPLACE Verification\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Table: {table_name}\n"
            f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}\n"
            f"Mode: Live logic verification\n"
            f"━━━━━━━━━━━━━━━━"
        )

    def notify_set_complete(self, set_data: dict, cumulative_dollars: float):
        """セット完了時のパブリック投稿"""
        marks = set_data["results"].replace("O", "🟢").replace("X", "🔴")
        sign = "+" if set_data["set_profit"] >= 0 else ""
        cum_sign = "+" if cumulative_dollars >= 0 else ""
        self.send(
            f"Set #{set_data['set_index']}\n"
            f"{marks}\n"
            f"{set_data['wins']}W/{set_data['losses']}L  "
            f"{sign}{set_data['set_profit']}u\n"
            f"Cumulative: {cum_sign}${cumulative_dollars:.2f}"
        )

    def notify_profit_target(self, session_num: int, amount_dollars: float, hands_played: int):
        """利確到達 — 最重要宣伝ポイント"""
        self.send(
            f"🏆 PROFIT TARGET REACHED\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Session #{session_num}\n"
            f"Profit: +${amount_dollars:.2f}\n"
            f"Hands: {hands_played}\n"
            f"Time: {datetime.now().strftime('%H:%M JST')}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"∫ LAPLACE"
        )

    def notify_loss_cut(self, session_num: int, amount_dollars: float, hands_played: int):
        """損切り — 正直に記録 (信頼性向上)"""
        self.send(
            f"⛔ Loss Cut\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Session #{session_num}\n"
            f"Loss: -${abs(amount_dollars):.2f}\n"
            f"Hands: {hands_played}\n"
            f"Time: {datetime.now().strftime('%H:%M JST')}\n"
            f"━━━━━━━━━━━━━━━━"
        )

    def notify_daily_summary(self, date_str: str, total_sessions: int,
                              profit_sessions: int, loss_sessions: int,
                              net_profit: float):
        """日次サマリー (毎日1回投稿)"""
        sign = "+" if net_profit >= 0 else ""
        emoji = "📈" if net_profit >= 0 else "📉"
        self.send(
            f"{emoji} Daily Summary — {date_str}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Sessions: {total_sessions}\n"
            f"Profit sessions: {profit_sessions}\n"
            f"Loss sessions: {loss_sessions}\n"
            f"Net: {sign}${net_profit:.2f}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"∫ LAPLACE"
        )


# ==========================================================
#  AdminNotifier — 管理者プライベート向け
#  (エラー、ライセンス、ユーザー監視、異常検知)
# ==========================================================

class AdminNotifier(TelegramNotifier):
    """管理者向け。ユーザー名・エラー詳細など機密情報を含む。"""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        token = bot_token or os.getenv("ADMIN_BOT_TOKEN", "")
        chat = chat_id or os.getenv("ADMIN_CHAT_ID", "")
        super().__init__(token, chat, label="ADMIN")
        self.hostname = socket.gethostname()

    def _prefix(self, user: str = None) -> str:
        u = user or os.getenv("LAPLACE_USER", self.hostname)
        return f"[{u}]"

    def notify_user_startup(self, user: str, config: dict):
        self.send(
            f"{self._prefix(user)} 🟢 LAPLACE started\n"
            f"Mode: {'DRY' if config.get('dry_run') else 'LIVE'}\n"
            f"Base: ${config.get('chip_base', 0)}\n"
            f"Target: ${config.get('profit_target', 0)}\n"
            f"LossCut: ${config.get('loss_cut', 0)}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}"
        )

    def notify_user_shutdown(self, user: str, reason: str = ""):
        self.send(
            f"{self._prefix(user)} 🔴 LAPLACE stopped\n"
            f"Reason: {reason or 'Normal'}\n"
            f"Time: {datetime.now().strftime('%H:%M JST')}"
        )

    def notify_user_profit(self, user: str, session_num: int, amount: float, cumulative_today: float):
        self.send(
            f"{self._prefix(user)} 🎉 Profit target\n"
            f"+${amount:.2f} locked in (Session #{session_num})\n"
            f"Today total: +${cumulative_today:.2f}"
        )

    def notify_user_loss_cut(self, user: str, session_num: int, amount: float, cumulative_today: float):
        self.send(
            f"{self._prefix(user)} ⚠️ Loss cut\n"
            f"-${abs(amount):.2f} (Session #{session_num})\n"
            f"Today total: ${'+' if cumulative_today >= 0 else ''}${cumulative_today:.2f}"
        )

    def notify_error(self, user: str, error_type: str, detail: str):
        self.send(
            f"{self._prefix(user)} ❌ ERROR [{error_type}]\n"
            f"{detail[:500]}\n"
            f"Time: {datetime.now().strftime('%H:%M JST')}"
        )

    def notify_license_event(self, user: str, event: str, detail: str = ""):
        self.send(
            f"{self._prefix(user)} 🔑 License {event}\n"
            f"{detail}"
        )

    def notify_anomaly(self, user: str, anomaly: str):
        self.send(
            f"{self._prefix(user)} 🚨 Anomaly detected\n"
            f"{anomaly}"
        )


# ==========================================================
#  UserNotifier — 個別ユーザー向け (既存互換)
# ==========================================================

class UserNotifier(TelegramNotifier):
    """個別ユーザー向け通知 (既存の TelegramNotifier と同等)"""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        super().__init__(token, chat, label="USER")

    def notify_startup(self, table_name: str, config: dict | None = None):
        self.send("▶ Engine online")

    def notify_shutdown(self, reason: str = ""):
        self.send(f"⏹ Engine offline{' — ' + reason if reason else ''}")

    def notify_profit_target(self, session_num: int, amount: float,
                             cumulative_today: float, table_name: str = ""):
        self.send(
            f"🎉 TARGET REACHED #{session_num}\n"
            f"+${amount:.2f} | Today: {cumulative_today:+.2f}"
        )

    def notify_loss_cut(self, session_num: int, amount: float,
                        cumulative_today: float, table_name: str = ""):
        self.send(
            f"🛑 LIMIT REACHED #{session_num}\n"
            f"-${abs(amount):.2f} | Today: {cumulative_today:+.2f}"
        )

    def notify_daily_summary(self, date_str: str, total_sessions: int,
                              profit_sessions: int, loss_sessions: int,
                              net_profit: float, table_name: str = ""):
        sign = "+" if net_profit >= 0 else ""
        self.send(
            f"📊 Daily | {date_str}\n"
            f"{total_sessions} rounds ({profit_sessions}P / {loss_sessions}L) | {sign}${net_profit:.2f}"
        )


# ==========================================================
#  Composite — 複数のNotifierを1つにまとめる
# ==========================================================

class CompositeNotifier:
    """複数のNotifierへ同時送信。イベント種類ごとに送信先を分岐。"""

    def __init__(self, public: PublicNotifier = None,
                 admin: AdminNotifier = None,
                 user: UserNotifier = None):
        self.public = public
        self.admin = admin
        self.user = user

    def send(self, message: str):
        """旧 TelegramNotifier.send() 互換 — UserNotifier へ送信"""
        if self.user:
            self.user.send(message)

    def on_startup(self, user: str, config: dict, table_name: str):
        if self.admin:
            self.admin.notify_user_startup(user, config)
        if self.public and config.get("verification_mode"):
            self.public.notify_verification_start(table_name)
        if self.user:
            self.user.notify_startup(table_name, config)

    def on_shutdown(self, user: str, reason: str = ""):
        if self.admin:
            self.admin.notify_user_shutdown(user, reason)
        if self.user:
            self.user.notify_shutdown(reason)

    def on_set_complete(self, set_data: dict, cumulative_dollars: float, verification: bool):
        if self.public and verification:
            self.public.notify_set_complete(set_data, cumulative_dollars)

    def on_profit_target(self, user: str, session_num: int, amount: float,
                           hands: int, cumulative_today: float, verification: bool,
                           table_name: str = ""):
        if self.admin:
            self.admin.notify_user_profit(user, session_num, amount, cumulative_today)
        if self.public and verification:
            self.public.notify_profit_target(session_num, amount, hands)
        if self.user:
            self.user.notify_profit_target(session_num, amount, cumulative_today, table_name)

    def on_loss_cut(self, user: str, session_num: int, amount: float,
                     hands: int, cumulative_today: float, verification: bool,
                     table_name: str = ""):
        if self.admin:
            self.admin.notify_user_loss_cut(user, session_num, amount, cumulative_today)
        if self.public and verification:
            self.public.notify_loss_cut(session_num, amount, hands)
        if self.user:
            self.user.notify_loss_cut(session_num, amount, cumulative_today, table_name)

    def on_daily_summary(self, date_str: str, total_sessions: int,
                          profit_sessions: int, loss_sessions: int,
                          net_profit: float, table_name: str = ""):
        if self.user:
            self.user.notify_daily_summary(date_str, total_sessions, profit_sessions, loss_sessions, net_profit, table_name)

    def on_error(self, user: str, error_type: str, detail: str):
        if self.admin:
            self.admin.notify_error(user, error_type, detail)
