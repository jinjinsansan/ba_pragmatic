"""Stake.com Evolution バカラ スクレイパー (ロビーWS方式)

EvolutionロビーのWebSocketから全バカラテーブルの結果をリアルタイム監視。
テーブル個別ページに入る必要なし — ロビーに留まるだけで全テーブルのデータが取得可能。

データソース:
  - lobby.configs: テーブルID→テーブル名マッピング
  - lobby.histories: 初期履歴（全テーブル分）
  - lobby.historyUpdated: リアルタイム結果更新

Camoufoxを使用してCloudflare Turnstileを回避する。
"""
import json
import os
import re
import shutil
import subprocess
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

from camoufox.sync_api import Camoufox
from playwright.sync_api import Page, WebSocket

import config
# Optional local round logging. Available only in full-repo installs; the
# shipped client distribution omits db.py and this becomes a no-op.
try:
    from db import insert_round as _insert_round  # type: ignore
except Exception:  # pragma: no cover - client-only path
    def _insert_round(**_kwargs) -> bool:
        return True


def insert_round(**kwargs) -> bool:
    return _insert_round(**kwargs)
from telegram_auth import ask_email_code

logger = logging.getLogger("baccarat.scraper")

JST = timezone(timedelta(hours=9))

# Evolution Big Road の色マッピング
# B = Blue = Player, R = Red = Banker
EVO_COLOR_MAP = {"B": "player", "R": "banker"}


class BaccaratScraper:
    """Stake.com EvolutionロビーWSを経由してバカラ結果を監視"""

    def __init__(self):
        self._camoufox_ctx = None
        self.browser = None
        self.page: Page | None = None
        self.table_name = config.TARGET_TABLE or ""
        self.round_count = 0
        self.last_round_id = ""
        self._ws_results: list[dict] = []
        self._lock = threading.Lock()

        # Evolution テーブル管理
        self._evo_table_configs: dict[str, dict] = {}  # table_id → config
        self._evo_table_histories: dict[str, list] = {}  # table_id → last known history
        self._evo_table_raw_histories: dict[str, list] = {}  # table_id → raw history entries (with c=B/R, ties)
        self._evo_players_count: dict[str, int] = {}  # table_id → player count
        self._evo_ws_connected = False
        self._last_ws_message_time: float = 0.0
        self._consecutive_reload_fails: int = 0

        # ゲーム内WS監視
        from game_ws import GameWSMonitor
        self.game_ws = GameWSMonitor()

        # マルチテーブル監視 (全て _lock で保護)
        self._target_table_ids: set[str] = set()  # 監視対象テーブルID群
        self._target_table_names: dict[str, str] = {}  # table_id → table_name
        self._shoe_epochs: dict[str, int] = {}  # table_id → shoe epoch
        self._new_shoe_signals: dict[str, bool] = {}  # table_id → signal
        self._last_result_per_table: dict[str, float] = {}  # table_id → last result time

        # 後方互換用
        self._target_table_id: str = ""
        self._profile_state_path = config.AUTH_STATE_DIR / "camoufox_profile_state.json"
        self._profile_dir = config.AUTH_STATE_DIR / "camoufox_profile"
        self._evo_lobby_frame_samples = 0
        self._evo_lobby_json_fail_samples = 0

    def _load_profile_state(self) -> dict:
        try:
            if self._profile_state_path.exists():
                with open(self._profile_state_path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                    return s if isinstance(s, dict) else {}
        except Exception:
            pass
        return {}

    def _save_profile_state(self, state: dict) -> None:
        try:
            with open(self._profile_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    def _rotate_profile_dir(self, reason: str) -> None:
        ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        backup = config.AUTH_STATE_DIR / f"camoufox_profile_broken_{ts}"
        try:
            if self._profile_dir.exists():
                logger.warning(f"Camoufoxプロファイルを退避: {self._profile_dir} -> {backup} (reason={reason})")
                try:
                    self._profile_dir.rename(backup)
                except Exception:
                    shutil.move(str(self._profile_dir), str(backup))
            self._profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"プロファイル退避に失敗（続行）: {e}")

    def _kill_camoufox_processes(self, reason: str) -> None:
        # Windows only. On crash loops we prefer "hard reset" over attempting to reuse a broken browser process.
        if os.name != "nt":
            return
        try:
            logger.warning(f"Camoufoxプロセス強制終了: {reason}")
            for img in ("camoufox.exe", "firefox.exe"):
                subprocess.call(
                    ["taskkill", "/F", "/T", "/IM", img],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def _repair_profile_dir(self) -> None:
        self._profile_dir.mkdir(parents=True, exist_ok=True)

        # Crash/kill の残骸を掃除（Windows: parent.lock, Linux: .parentlock）
        for lock_name in ("parent.lock", ".parentlock", "lock"):
            try:
                p = self._profile_dir / lock_name
                if p.exists():
                    p.unlink()
                    logger.warning(f"プロファイル lock 削除: {p}")
            except Exception:
                pass

        # キャッシュ起因のクラッシュを避ける（セッション情報は残す）
        for d in ("cache2", "startupCache", "sessionstore-backups"):
            try:
                dp = self._profile_dir / d
                if dp.exists() and dp.is_dir():
                    shutil.rmtree(dp, ignore_errors=True)
                    logger.warning(f"プロファイル cache クリア: {dp}")
            except Exception:
                pass

    def _prepare_profile_for_launch(self) -> None:
        state = self._load_profile_state()
        now = time.time()
        crash_streak = int(state.get("crash_streak", 0) or 0)
        lock_exists = any((self._profile_dir / n).exists() for n in ("parent.lock", ".parentlock", "lock"))

        # 前回「booting=true」のままなら、異常終了（クラッシュ/kill/電源断）とみなす
        if state.get("booting") is True:
            last_ts = float(state.get("boot_ts", 0) or 0)
            if last_ts and (now - last_ts) < 20 * 60:
                crash_streak += 1

        # 前回異常終了 or lock残存なら、プロセス自体がゾンビになっていることがあるため先に殺す
        if state.get("booting") is True or crash_streak > 0 or lock_exists:
            self._kill_camoufox_processes(reason=f"booting={state.get('booting')} crash_streak={crash_streak} lock={lock_exists}")

        # 連続クラッシュ時はプロファイル腐敗を疑ってローテーション
        if crash_streak >= 3:
            self._rotate_profile_dir(reason=f"crash_streak={crash_streak}")
            crash_streak = 0
        else:
            self._repair_profile_dir()

        state.update({"booting": True, "boot_ts": now, "crash_streak": crash_streak})
        self._save_profile_state(state)

    def _mark_launch_success(self) -> None:
        state = self._load_profile_state()
        state.update({"booting": False, "crash_streak": 0, "last_success_ts": time.time()})
        self._save_profile_state(state)

    def _mark_launch_failed(self) -> None:
        state = self._load_profile_state()
        crash_streak = int(state.get("crash_streak", 0) or 0) + 1
        state.update({"booting": False, "crash_streak": crash_streak, "last_fail_ts": time.time()})
        self._save_profile_state(state)

    def _apply_video_constraints(self) -> None:
        if not self.page:
            return
        try:
            if config.VIDEO_VIEWPORT_WIDTH > 0 and config.VIDEO_VIEWPORT_HEIGHT > 0:
                self.page.set_viewport_size({
                    "width": config.VIDEO_VIEWPORT_WIDTH,
                    "height": config.VIDEO_VIEWPORT_HEIGHT,
                })
                logger.info(f"Video viewport set: {config.VIDEO_VIEWPORT_WIDTH}x{config.VIDEO_VIEWPORT_HEIGHT}")
        except Exception as e:
            logger.warning(f"Viewport設定失敗（続行）: {e}")

    @staticmethod
    def _resolve_executable_path() -> str | None:
        """Windows Store版Python対応: realpathでCamoufox実行パスを解決"""
        try:
            from camoufox.pkgman import get_path, LAUNCH_FILE, OS_NAME
            path = get_path(LAUNCH_FILE[OS_NAME])
            real = os.path.realpath(path)
            if real != path and os.path.isfile(real):
                logger.info(f"Camoufox実行パス解決: {real}")
                return real
        except Exception:
            pass
        return None

    def start(self):
        """ブラウザ起動 → ログイン → WS傍受設定 → ロビーに移動"""
        self._prepare_profile_for_launch()
        try:
            logger.info(f"Camoufox起動中... (profile={config.PROFILE_NAME})")
            exe_path = self._resolve_executable_path()

            # Use persistent context so cookies + localStorage survive restarts
            profile_dir = str(self._profile_dir)
            launch_opts = {
                "headless": config.HEADLESS,
                "persistent_context": True,
                "user_data_dir": profile_dir,
            }
            if exe_path:
                launch_opts["executable_path"] = exe_path
            self._camoufox_ctx = Camoufox(**launch_opts)
            # persistent_context returns BrowserContext directly (not Browser)
            ctx = self._camoufox_ctx.__enter__()
            self.browser = ctx
            # Reuse existing page or create new one
            if ctx.pages:
                self.page = ctx.pages[0]
            else:
                self.page = ctx.new_page()
            self._apply_video_constraints()

            # WSは Stake の最初のページロード中に張られることがあるため、
            # login/goto の前に必ずリスナー登録して取りこぼしを防ぐ
            self._register_ws_listener()

            # Cookie復元
            self._restore_cookies()

            # ログイン
            self._login()

            # バカラロビーに移動（テーブルに入らない）
            self._navigate_to_lobby()

            # ロビー遷移後にEvolutionが再ログインを要求する場合がある
            # "This game is not available in demo mode" → ログインモーダルが再表示される
            if not self._is_logged_in():
                logger.warning("ロビー遷移後にログアウト検出 — 再ログイン試行")
                self._login_from_lobby()

            # EvolutionロビーWS接続を待機（未接続ならリロードして再試行）
            self.setup_ws_intercept()

            self._mark_launch_success()
        except Exception:
            self._mark_launch_failed()
            raise

    def rebuild_page(self) -> bool:
        """ページを完全に破棄して新規ページを作成する (Lv4a nuclear recovery)

        TRY AGAIN / SESSION EXPIRED / iframe死亡など、
        page.goto やリロードでは復活できない深刻な状態からの救済。
        Browser context は維持されるため Cookie は保持される (再ログイン不要)。

        Returns: True=成功, False=失敗
        """
        try:
            old_page = self.page
            logger.info("[rebuild_page] 古いページを破棄して新規ページ作成")
            # Browser context (persistent_context) で new_page
            new_page = self.browser.new_page()
            # 古いページを閉じる
            try:
                old_page.close()
            except Exception as _ce:
                logger.warning(f"[rebuild_page] 古いページクローズ例外: {_ce}")
            # 新しいページを参照に設定
            self.page = new_page
            # WS リスナー再登録 (新しいページに対して)
            try:
                self._register_ws_listener()
            except Exception as _we:
                logger.warning(f"[rebuild_page] WSリスナー登録例外: {_we}")
            logger.info("[rebuild_page] ✅ 新規ページ作成完了")
            return True
        except Exception as e:
            logger.error(f"[rebuild_page] ❌ 失敗: {e}")
            return False

    def _restore_cookies(self):
        """保存済みCookieを復元"""
        cookie_file = config.AUTH_STATE_DIR / "stake_cookies.json"
        if not cookie_file.exists():
            logger.info("保存済みCookieなし")
            return
        try:
            with open(cookie_file) as f:
                cookies = json.load(f)
            self.page.context.add_cookies(cookies)
            logger.info(f"Cookie復元: {len(cookies)}件")
        except Exception as e:
            logger.warning(f"Cookie復元失敗: {e}")

    def _login(self):
        """Stake.comにログイン"""
        logger.info("Stakeにアクセス中...")
        self.page.goto(config.STAKE_URL, wait_until="domcontentloaded", timeout=60000)
        # Stake is a SPA — wait for the page to fully render before
        # checking login status.  The domcontentloaded event fires long
        # before React hydrates, so we poll for known UI elements.
        # Priority: detect logged-IN state first (balance/wallet), then fall back to login-link.
        _logged_in_early = False
        for _wait in range(30):  # up to ~30s
            time.sleep(1)
            try:
                # Check logged-in state first (balance/wallet visible)
                is_in = self.page.evaluate("""() => {
                    return !!(document.querySelector('[data-testid="balance"]')
                           || document.querySelector('[class*="wallet"]')
                           || (document.body.innerText && /wallet/i.test(document.body.innerText)));
                }""")
                if is_in:
                    _logged_in_early = True
                    logger.info("SPA検出: ログイン済み (balance/wallet)")
                    break
                # If not logged in, check if login-link appeared (= definitely logged out)
                has_login_link = self.page.evaluate("""() => {
                    return !!document.querySelector('[data-testid="login-link"]');
                }""")
                if has_login_link and _wait >= 10:
                    # Wait at least 10s before concluding logged out (Cookie may still be loading)
                    logger.info("SPA検出: ログアウト状態 (login-link)")
                    break
            except Exception:
                pass

        self.page.screenshot(path=str(config.SCREENSHOTS_DIR / "after_goto.png"))
        logger.info(f"ページタイトル: {self.page.title()}")

        if _logged_in_early or self._is_logged_in():
            logger.info("すでにログイン済み")
            self._save_cookies()
            return

        # If credentials are empty, fall back to manual login mode:
        # the user logs in via the visible browser window, and we poll
        # until we detect the logged-in state.
        has_credentials = bool(config.STAKE_USERNAME and config.STAKE_PASSWORD)
        if not has_credentials:
            logger.info("STAKE_USERNAME/PASSWORD not set -- waiting for manual login")
            self._wait_for_manual_login(timeout=300)
            return

        # ログインフォームを開く
        logger.info("ログインフォームを開く...")
        _form_opened = False
        for _open_try in range(3):
            try:
                self.page.evaluate("""() => {
                    const btn = document.querySelector('[data-testid="login-link"]');
                    if (btn) { btn.click(); return true; }
                    const els = Array.from(document.querySelectorAll('button, a'));
                    for (const el of els) {
                        if (/sign.?in|log.?in/i.test(el.textContent)) { el.click(); return true; }
                    }
                    return false;
                }""")
            except Exception as e:
                logger.warning(f"ログインリンククリック失敗: {e}")

            # モーダルが開くまで待機 (Email inputが出現するまで)
            for _modal_wait in range(10):
                time.sleep(1)
                try:
                    has_input = self.page.evaluate("""() => {
                        const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="radio"])');
                        for (const inp of inputs) {
                            if (inp.offsetParent !== null) return true;
                        }
                        return false;
                    }""")
                    if has_input:
                        _form_opened = True
                        logger.info(f"ログインフォーム表示確認 ({_modal_wait+1}秒)")
                        break
                except Exception:
                    pass
            if _form_opened:
                break
            logger.warning(f"ログインフォーム未表示 (試行{_open_try+1}/3)")
            time.sleep(2)

        if not _form_opened:
            logger.error("ログインフォームを開けませんでした — 手動ログインに切り替え")
            self._wait_for_manual_login(timeout=300)
            return

        # Google One Tapポップアップ除去
        self.page.evaluate("""() => {
            document.querySelectorAll('iframe[src*="google"]').forEach(f => f.remove());
            document.querySelectorAll('#credential_picker_container').forEach(d => d.remove());
        }""")
        time.sleep(0.5)

        self.page.screenshot(path=str(config.SCREENSHOTS_DIR / "login_01_before_input.png"))

        # Email入力 (複数セレクタでフォールバック)
        logger.info("認証情報を入力中...")
        found_email = self.page.evaluate("""() => {
            // 方法1: name属性
            let el = document.querySelector('input[name="emailOrName"], input[name="email"], input[type="email"]');
            if (el && el.offsetParent !== null) { el.focus(); el.click(); return 'name:' + (el.name || el.type); }
            // 方法2: 可視inputの最初のもの (モーダル内)
            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])'));
            const visible = inputs.filter(i => i.offsetParent !== null);
            if (visible.length > 0) { visible[0].focus(); visible[0].click(); return 'visible:' + (visible[0].name || visible[0].type || visible[0].placeholder); }
            return null;
        }""")
        logger.info(f"Email入力フィールド: {found_email}")
        if not found_email:
            logger.error("Emailフィールドが見つかりません — 手動ログインに切り替え")
            self._wait_for_manual_login(timeout=300)
            return
        time.sleep(0.3)
        self.page.keyboard.type(config.STAKE_USERNAME, delay=30)
        time.sleep(0.5)

        # Tab → パスワード入力
        self.page.keyboard.press("Tab")
        time.sleep(0.3)
        self.page.keyboard.type(config.STAKE_PASSWORD, delay=30)
        time.sleep(0.5)

        self.page.screenshot(path=str(config.SCREENSHOTS_DIR / "login_02_after_input.png"))

        # ログインボタン送信
        submitted = self.page.evaluate("""() => {
            // 方法1: type="submit"
            const btn = document.querySelector('button[type="submit"], [data-testid="button-login"]');
            if (btn) { btn.click(); return 'submit:' + btn.textContent.trim(); }
            // 方法2: Sign Inテキスト
            const els = Array.from(document.querySelectorAll('button'));
            for (const el of els) {
                if (/sign.?in|log.?in/i.test(el.textContent)) { el.click(); return 'matched:' + el.textContent.trim(); }
            }
            return null;
        }""")
        logger.info(f"ログインボタン: {submitted}")
        time.sleep(5)

        self.page.screenshot(path=str(config.SCREENSHOTS_DIR / "login_03_after_submit.png"))

        # Email Code（2FA）
        if not self._is_logged_in():
            self._handle_email_code()

        if self._is_logged_in():
            logger.info("ログイン成功")
            self._save_cookies()
        else:
            logger.warning("ログイン状態が不明（続行を試みます）")

    def _wait_for_manual_login(self, timeout: int = 300):
        """Poll until user completes login manually in the visible browser."""
        logger.info(f"Please log in to Stake.com in the browser window (timeout={timeout}s)")
        try:
            from agent_api import send_action, send_log
            send_action("Please log in to Stake.com in the browser window")
            send_log(f"Waiting up to {timeout}s for manual login...")
        except Exception:
            pass
        interval = 10
        elapsed = 0
        while elapsed < timeout:
            if self._is_logged_in():
                logger.info("Manual login detected")
                try:
                    from agent_api import send_action
                    send_action("Login detected -- continuing")
                except Exception:
                    pass
                self._save_cookies()
                return
            time.sleep(interval)
            elapsed += interval
            remaining = timeout - elapsed
            if remaining > 0 and remaining % 60 < interval:
                logger.info(f"Still waiting for login... ({remaining}s remaining)")
        logger.warning(f"Manual login not detected within {timeout}s -- continuing anyway")

    def _handle_email_code(self):
        """Email Code（2FA）画面を検出し、Telegram経由でコードを取得して入力"""
        logger.info("Email Code画面を確認中...")
        code_input = self.page.locator(
            'input[placeholder*="Code" i], '
            'input[name*="code" i], '
            'input[placeholder*="code" i]'
        )
        if code_input.count() == 0:
            try:
                body_text = self.page.locator("body").inner_text()[:500]
                if "email code" in body_text.lower() or "verification" in body_text.lower():
                    code_input = self.page.locator('input[type="text"]:visible')
                else:
                    return
            except Exception:
                return

        if code_input.count() == 0:
            return

        logger.info("📱 Telegram Bot経由でメール認証コードを待機中...")
        email_code = ask_email_code(timeout=300)
        if not email_code:
            logger.error("認証コードが取得できませんでした")
            return

        # コード入力 (fill → JS → keyboard の順でフォールバック)
        try:
            code_input.first.fill(email_code, timeout=10000)
            logger.info("2FAコード入力完了 (fill)")
        except Exception:
            logger.warning("fill()タイムアウト — JS入力にフォールバック")
            try:
                self.page.evaluate(f"""() => {{
                    const sels = ['input[placeholder*="Code" i]', 'input[name*="code" i]',
                                  'input[type="text"]:not([name*="email"]):not([name*="password"])'];
                    for (const s of sels) {{
                        const el = document.querySelector(s);
                        if (el && el.offsetParent !== null) {{
                            el.focus(); el.value = '{email_code}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            el.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                logger.info("2FAコード入力完了 (JS)")
            except Exception:
                logger.warning("JS入力失敗 — keyboard入力にフォールバック")
                try:
                    self.page.keyboard.type(email_code, delay=50)
                    logger.info("2FAコード入力完了 (keyboard)")
                except Exception as e:
                    logger.error(f"2FAコード入力全て失敗: {e}")
                    return

        time.sleep(0.5)

        self.page.evaluate("""() => {
            const btn = document.querySelector('button[type="submit"], [data-testid="button-login"]');
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        time.sleep(8)

    def _save_cookies(self):
        """現在のCookieを保存"""
        try:
            cookie_file = config.AUTH_STATE_DIR / "stake_cookies.json"
            cookies = self.page.context.cookies()
            with open(cookie_file, "w") as f:
                json.dump(cookies, f, indent=2)
            logger.info(f"Cookie保存: {len(cookies)}件 → {cookie_file}")
        except Exception as e:
            logger.warning(f"Cookie保存エラー: {e}")

    def _is_logged_in(self) -> bool:
        """ログイン状態を確認"""
        try:
            return self.page.evaluate("""() => {
                const body = document.body.innerText;
                if (/wallet/i.test(body) && /\\d+\\.\\d{2,}/.test(body)) return true;
                return !!document.querySelector('[data-test="balance"], [class*="wallet"], [data-testid="user-menu"], [data-testid="balance"]');
            }""")
        except Exception:
            return False

    def _login_from_lobby(self):
        """ロビー遷移後にログインモーダルが表示された場合の再ログイン。
        モーダルが既に開いている前提で、Email/Password入力→Submit。

        【失敗時の挙動】
        - クレデンシャルあり + フォーム未表示 → RuntimeError 送出 (caller が full_recovery にエスカレーション)
        - クレデンシャルなし → 手動ログイン待ち (短縮 60秒)
        """
        has_credentials = bool(config.STAKE_USERNAME and config.STAKE_PASSWORD)
        if not has_credentials:
            logger.info("クレデンシャル未設定 — 手動ログイン待ち (60s)")
            self._wait_for_manual_login(timeout=60)
            return

        # ログインモーダルが表示されるまで待機
        # "Login" / "Sign In" ボタンを探してクリック
        logger.info("ロビー上のログインボタンを検索...")
        try:
            self.page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
                for (const b of btns) {
                    if (/^login$/i.test(b.textContent.trim()) || /^sign.?in$/i.test(b.textContent.trim())) {
                        b.click(); return true;
                    }
                }
                return false;
            }""")
            time.sleep(3)
        except Exception:
            pass

        # モーダル内のEmail inputを待機
        _form_ready = False
        for _w in range(10):
            time.sleep(1)
            try:
                has = self.page.evaluate("""() => {
                    const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])');
                    for (const inp of inputs) { if (inp.offsetParent !== null) return true; }
                    return false;
                }""")
                if has:
                    _form_ready = True
                    break
            except Exception:
                pass

        if not _form_ready:
            # クレデンシャルがあるのにフォームが出ない = ページ状態が壊れている
            # 300秒の手動待機ではなく、例外を投げて caller (agent_api) に
            # full_recovery エスカレーションを任せる
            logger.warning("ログインフォーム未表示 — full_recovery へエスカレーション")
            raise RuntimeError("Login form not visible after page state recovery — escalate to full_recovery")

        # Email入力
        logger.info("ロビー再ログイン: 認証情報入力中...")
        self.page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])'));
            const visible = inputs.filter(i => i.offsetParent !== null);
            if (visible.length > 0) { visible[0].focus(); visible[0].click(); visible[0].value = ''; }
        }""")
        time.sleep(0.3)
        self.page.keyboard.type(config.STAKE_USERNAME, delay=30)
        time.sleep(0.3)
        self.page.keyboard.press("Tab")
        time.sleep(0.3)
        self.page.keyboard.type(config.STAKE_PASSWORD, delay=30)
        time.sleep(0.5)

        # Submit
        self.page.evaluate("""() => {
            const btn = document.querySelector('button[type="submit"]');
            if (btn) { btn.click(); return true; }
            const els = Array.from(document.querySelectorAll('button'));
            for (const el of els) {
                if (/sign.?in/i.test(el.textContent)) { el.click(); return true; }
            }
            return false;
        }""")
        time.sleep(5)

        # 2FA確認
        if not self._is_logged_in():
            self._handle_email_code()

        if self._is_logged_in():
            logger.info("ロビー再ログイン成功")
            self._save_cookies()
            # ロビーをリロードしてEvolution iframeを再接続
            # クラウドPC等の低スペック環境向けに90秒に拡張（30秒だと不足）
            time.sleep(2)
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                logger.warning(f"ロビーリロードタイムアウト — 続行: {e}")
            time.sleep(8)
        else:
            logger.warning("ロビー再ログイン失敗 — full_recovery へエスカレーション")
            raise RuntimeError("Re-login submit failed — escalate to full_recovery")

    def _navigate_to_lobby(self):
        """バカラロビーに移動（テーブルに入らない）

        SPA内ナビゲーション (window.location) を試行し、失敗時のみ page.goto() にフォールバック。
        page.goto() はフルリロードを引き起こし、セッションCookieが無効化される場合がある。
        """
        logger.info("バカラロビーに移動中...")
        lobby_url = config.BACCARAT_LOBBY_URL

        # 方法1: page.goto (安定優先)
        try:
            self.page.goto(
                lobby_url,
                wait_until="domcontentloaded",
                timeout=90000,
            )
            time.sleep(8)
        except Exception as e:
            # 方法2: SPA内ナビゲーション (セッション維持)
            logger.warning(f"page.goto失敗 ({e}) — SPA遷移を試行")
            current_url = ""
            try:
                current_url = self.page.url or ""
            except Exception:
                pass
            if "stake.com" not in current_url:
                raise
            self.page.evaluate(f'() => {{ window.location.href = "{lobby_url}"; }}')
            logger.info("SPA内ナビゲーション実行")
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=90000)
            except Exception as _we:
                logger.warning(f"SPA遷移後のload待機に失敗（続行）: {_we}")
            time.sleep(8)

        # Cookieバナーがあれば閉じる
        try:
            accept_btn = self.page.locator('button:has-text("Accept")')
            if accept_btn.count() > 0 and accept_btn.first.is_visible():
                accept_btn.first.click()
                time.sleep(1)
        except Exception:
            pass

        try:
            self.page.screenshot(path=str(config.SCREENSHOTS_DIR / "baccarat_lobby.png"))
        except Exception as e:
            logger.warning(f"ロビー到着スクショ失敗（続行）: {e}")
        try:
            title = self.page.title()
        except Exception as e:
            title = f"<title-failed: {e}>"
        logger.info(f"バカラロビー到着 — タイトル: {title}")

    def _register_ws_listener(self):
        """WebSocketリスナーを登録（ナビゲーション前に呼ぶこと）"""
        logger.info("WebSocket傍受を設定中...")

        def _payload_to_str(data) -> str:
            try:
                if isinstance(data, dict) and "payload" in data:
                    data = data.get("payload")
                # Playwright's framereceived/framesent passes WebSocketFrame (has .payload)
                if hasattr(data, "payload"):
                    data = getattr(data, "payload")
                if isinstance(data, bytes):
                    return data.decode("utf-8", "ignore")
                if isinstance(data, str):
                    return data
                return str(data)
            except Exception:
                return str(data)

        def on_ws(ws: WebSocket):
            url = ws.url
            # EvolutionロビーWSのみ対象 (chat/tableは除外)
            is_evo = "evo-games.com" in url or "evolution" in url.lower()
            is_lobby = "lobby/socket" in url
            is_chat = "chat/table" in url

            if not is_evo:
                logger.info(f"WebSocket接続検出: {url[:120]}")

            if is_evo and is_lobby:
                # ロビーWS — 履歴データ処理
                logger.info(f"✅ EvolutionロビーWS検出: {url[:120]}")
                self._evo_ws_connected = True

                def on_message(data):
                    payload = _payload_to_str(data)
                    if self._evo_lobby_frame_samples < 2:
                        self._evo_lobby_frame_samples += 1
                        logger.info(f"[evo-lobby] frame sample: {payload[:120]}")
                    self._handle_evo_lobby_message(payload)

                def on_sent(data):
                    text = _payload_to_str(data)[:200]
                    logger.debug(f"WS送信: {text}")

                def on_close():
                    logger.warning("❌ EvolutionロビーWS切断")
                    self._evo_ws_connected = False

                ws.on("framereceived", on_message)
                ws.on("framesent", on_sent)
                ws.on("close", on_close)

            elif is_evo and not is_chat:
                # ゲーム内WS — game_wsモニターにメッセージ転送 + サイレンス判定更新
                def _forward_to_game_ws(data):
                    self._last_ws_message_time = time.time()
                    raw = _payload_to_str(data)
                    self.game_ws.on_ws_message(raw)

                ws.on("framesent", _forward_to_game_ws)
                ws.on("framereceived", _forward_to_game_ws)

            else:
                # 全ての非ロビーWSにもリスナーを登録 (stake.com等)
                def _forward_any_ws(data, ws_url=url):
                    self._last_ws_message_time = time.time()
                    raw = _payload_to_str(data)
                    # BET/BALANCE/SETTLED関連のメッセージのみ転送
                    if any(k in raw for k in ["CLIENT_BET", "CLIENT_BALANCE", "SETTLED", "MULTIPLIER"]):
                        logger.info(f"非EvoWS転送 ({ws_url[:40]}): {raw[:100]}")
                        self.game_ws.on_ws_message(raw)

                ws.on("framesent", _forward_any_ws)
                ws.on("framereceived", _forward_any_ws)

        self.page.on("websocket", on_ws)
        logger.info("WebSocket傍受設定完了")

    def setup_ws_intercept(self):
        """EvolutionロビーWSの接続を待機する（後方互換性のため残す）

        start()内で _register_ws_listener() → _navigate_to_lobby() の順で
        呼ばれるため、ここでは接続待機のみ行う。
        """
        def _wait_for_configs(timeout_sec: int) -> bool:
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if self._evo_table_configs:
                    return True
                time.sleep(1)
            return False

        # まずは自然に configs/histories が流れてくるのを待つ
        if _wait_for_configs(40):
            logger.info("EvolutionロビーWS: configs受信確認 ✅")
            return

        # configsが来ない＝WS取りこぼし or SPA状態不整合。強制リロードでWS張り直し。
        logger.warning("EvolutionロビーWS: configs未受信 — ページをリロードして再試行")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=120000)
        except Exception:
            logger.warning("リロードタイムアウト — ロビーに再遷移")
            try:
                self.page.goto(config.BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=120000)
            except Exception:
                pass
        time.sleep(10)

        if _wait_for_configs(40):
            logger.info("EvolutionロビーWS: configs受信確認（リロード後）✅")
            return

        logger.error("EvolutionロビーWS: configsが受信できませんでした（ブロック/通信不良の可能性）")

    def _handle_evo_lobby_message(self, payload: str):
        """EvolutionロビーWSメッセージを処理"""
        self._last_ws_message_time = time.time()
        try:
            if not isinstance(payload, str) or len(payload) < 10:
                return

            # Evolution lobby can be either plain JSON:
            #   {"type":"lobby.configs","args":{...}}
            # or socket.io framed:
            #   42["lobby.configs",{...}]
            msg_type = ""
            args = {}
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    msg_type = data.get("type", "")
                    args = data.get("args", {}) or {}
                else:
                    return
            except json.JSONDecodeError:
                # socket.io frame: leading digits then JSON array/object
                p = payload
                if p.startswith("42"):
                    try:
                        arr = json.loads(p[2:])
                        if isinstance(arr, list) and len(arr) >= 2 and isinstance(arr[0], str):
                            msg_type = arr[0]
                            args = arr[1] if isinstance(arr[1], dict) else {}
                        else:
                            return
                    except Exception:
                        pass
                if not msg_type:
                    # strip leading digits until first '{'/'['
                    i = 0
                    while i < len(p) and p[i].isdigit():
                        i += 1
                    if i > 0 and i < len(p) and p[i] in "{[":
                        try:
                            arr = json.loads(p[i:])
                            if isinstance(arr, list) and len(arr) >= 2 and isinstance(arr[0], str):
                                msg_type = arr[0]
                                args = arr[1] if isinstance(arr[1], dict) else {}
                            elif isinstance(arr, dict):
                                msg_type = arr.get("type", "")
                                args = arr.get("args", {}) or {}
                        except Exception:
                            pass
                if not msg_type:
                    if self._evo_lobby_json_fail_samples < 2:
                        self._evo_lobby_json_fail_samples += 1
                        logger.warning(f"[evo-lobby] non-json frame (head): {payload[:120]}")
                    return

            if msg_type == "lobby.configs":
                self._process_configs(args)
            elif msg_type == "lobby.histories":
                self._process_histories(args)
            elif msg_type == "lobby.historyUpdated":
                self._process_history_updated(args)
            elif msg_type == "lobby.configsUpdated":
                self._process_configs_updated(args)
            elif msg_type == "lobby.playersCount":
                self._process_players_count(args)

        except Exception as e:
            logger.debug(f"EvolutionロビーWS解析エラー: {e}")

    def _process_configs(self, args: dict):
        """lobby.configs → テーブルID→設定マッピングを構築"""
        configs = args.get("configs", {})
        baccarat_tables = []

        for table_id, cfg in configs.items():
            gt = cfg.get("gt", "")
            # バカラ系テーブルのみ記録
            if gt in ("baccarat",):
                self._evo_table_configs[table_id] = cfg
                title = cfg.get("title", table_id)
                baccarat_tables.append(f"{title} ({table_id})")

        logger.info(f"Evolutionバカラテーブル: {len(baccarat_tables)}件検出")

        # ターゲットテーブルIDを決定
        self._resolve_target_table()

    def _process_configs_updated(self, args: dict):
        """lobby.configsUpdated → テーブル設定の差分更新"""
        configs = args.get("configs", {})
        for table_id, cfg in configs.items():
            gt = cfg.get("gt", "")
            if gt in ("baccarat",):
                self._evo_table_configs[table_id] = cfg

    def _resolve_target_table(self):
        """ターゲットテーブルを解決。

        table_name が空 or "all" → 全バカラテーブル (Salon Prive除外)
        それ以外 → 名前フィルタ
        """
        target_name = self.table_name.lower().strip()
        match_all = not target_name or target_name == "all"
        self._target_table_ids.clear()
        self._target_table_names.clear()

        for tid, cfg in self._evo_table_configs.items():
            title = cfg.get("title", "")
            title_lower = title.lower()

            if any(ex in title_lower for ex in self._TABLE_EXCLUDE):
                continue

            if match_all or all(w in title_lower for w in target_name.split()):
                self._target_table_ids.add(tid)
                self._target_table_names[tid] = title
                if tid not in self._shoe_epochs:
                    self._shoe_epochs[tid] = int(time.time())
                    self._new_shoe_signals[tid] = False

        if self._target_table_ids:
            first_id = next(iter(self._target_table_ids))
            self._target_table_id = first_id
            logger.info(f"監視テーブル: {len(self._target_table_ids)}件 (全バカラ, Salon Prive除外)")
        else:
            logger.warning(f"テーブルが見つかりません (フィルタ: '{self.table_name}')")

    def _process_players_count(self, args: dict):
        """lobby.playersCount → テーブル別参加者数更新"""
        players = args.get("players", {})
        with self._lock:
            for table_id, count in players.items():
                self._evo_players_count[table_id] = count

    def _process_histories(self, args: dict):
        """lobby.histories → 一括ロード (リコネクト時にも送られる)

        リコネクト時にシューリセットも検出する。
        old_results >= 30 かつ new_results < 5 → シューリセットと判定。
        """
        histories = args.get("histories", {})

        # 全テーブルの生履歴を保存 (テーブル選定用)
        with self._lock:
            for table_id, hist_data in histories.items():
                self._evo_table_raw_histories[table_id] = hist_data.get("results", [])

        for table_id, hist_data in histories.items():
            new_results = hist_data.get("results", [])

            if table_id in self._target_table_ids:
                old_results = self._evo_table_histories.get(table_id, [])
                tname = self._target_table_names.get(table_id, table_id)

                # シューリセット検出 (histories でも判定する)
                if (old_results and len(old_results) >= 30
                        and len(new_results) < 5):
                    with self._lock:
                        self._shoe_epochs[table_id] = int(time.time())
                        self._new_shoe_signals[table_id] = True
                    logger.info(f"シューリセット検出 (histories): {tname} {len(old_results)}→{len(new_results)}")

                # 差分のみ追加
                added = self._diff_results(old_results, new_results)
                if added:
                    for entry in added:
                        result_info = self._parse_evo_bead_entry(entry, table_id)
                        if result_info:
                            with self._lock:
                                self._ws_results.append(result_info)
                                self._last_result_per_table[table_id] = time.time()

            self._evo_table_histories[table_id] = new_results

        for tid in self._target_table_ids:
            hist = self._evo_table_histories.get(tid, [])
            tname = self._target_table_names.get(tid, tid)
            logger.info(f"履歴ロード: {tname} ({tid}) — {len(hist)}件")

    def _process_history_updated(self, args: dict):
        """lobby.historyUpdated → 全ターゲットテーブルのリアルタイム更新"""
        # 全テーブルの生履歴を更新 (テーブル選定用)
        with self._lock:
            for table_id, update_data in args.items():
                new_r = update_data.get("results", []) if isinstance(update_data, dict) else []
                if new_r:
                    self._evo_table_raw_histories[table_id] = new_r

        for table_id, update_data in args.items():
            if table_id not in self._target_table_ids:
                continue

            new_results = update_data.get("results", [])
            if not new_results:
                continue

            old_results = self._evo_table_histories.get(table_id, [])
            tname = self._target_table_names.get(table_id, table_id)

            if (old_results and len(old_results) >= 30
                    and len(new_results) < 5):
                with self._lock:
                    self._shoe_epochs[table_id] = int(time.time())
                    self._new_shoe_signals[table_id] = True
                logger.info(f"シューリセット検出 (historyUpdated): {tname} {len(old_results)}→{len(new_results)}")

            added = self._diff_results(old_results, new_results)
            self._evo_table_histories[table_id] = new_results

            if added:
                for entry in added:
                    result_info = self._parse_evo_bead_entry(entry, table_id)
                    if result_info:
                        with self._lock:
                            self._ws_results.append(result_info)
                            self._last_result_per_table[table_id] = time.time()

    def _diff_results(self, old: list, new: list) -> list:
        """前回と今回の履歴を比較して新しいエントリを返す。

        old が空の場合: 初回ロードまたはブラウザ再起動後。
        main.py側で初期履歴を直接shoe に読み込むため、ここでは空を返す。
        ただし _evo_table_histories は更新されるので、次回以降の差分は正しく検出される。
        """
        if not new:
            return []

        if not old:
            # 初回ロード — main.py / monitor が直接 shoe に読み込むため空を返す
            return []

        old_len = len(old)
        new_len = len(new)
        added = []

        # シューリセット: 履歴が大幅縮小 → 新しい結果を全て返す
        if new_len < old_len - 5:
            added.extend(new)
        # 新しいエントリが追加された場合
        elif new_len > old_len:
            added.extend(new[old_len:])

        # 既存エントリのTie更新をチェック（直近数エントリのみ）
        overlap_end = min(old_len, new_len)
        if overlap_end > 0:
            check_start = max(0, overlap_end - 3)
            for i in range(check_start, overlap_end):
                old_entry = old[i]
                new_entry = new[i]
                if isinstance(old_entry, dict) and isinstance(new_entry, dict):
                    old_ties = old_entry.get("ties", 0)
                    new_ties = new_entry.get("ties", 0)
                    if new_ties > old_ties:
                        tie_entry = dict(new_entry)
                        tie_entry["_is_tie_update"] = True
                        tie_entry["_tie_count"] = new_ties - old_ties
                        added.append(tie_entry)

        return added

    def _parse_evo_bead_entry(self, entry: dict | str, table_id: str) -> dict | None:
        """Evolution Big Road/BeadのエントリからResult情報を抽出"""
        tname = self._target_table_names.get(table_id, table_id)
        # BacBo形式（文字列リスト）
        if isinstance(entry, str):
            mapping = {"player": "player", "banker": "banker", "tie": "tie"}
            result = mapping.get(entry.lower())
            if result:
                return {
                    "round_id": f"evo_{table_id}_{int(time.time()*1000)}",
                    "result": result,
                    "table_id": table_id,
                    "table_name": tname,
                    "player_score": None,
                    "banker_score": None,
                    "player_pair": False,
                    "banker_pair": False,
                }
            return None

        if not isinstance(entry, dict):
            return None

        pos = entry.get("pos", [0, 0])

        shoe_epoch = self._shoe_epochs.get(table_id, int(time.time()))

        # Tie更新の場合 — 結果を "tie" として返す
        if entry.get("_is_tie_update"):
            ties = entry.get("ties", 1)
            round_id = f"evo_{table_id}_s{shoe_epoch}_c{pos[0]}r{pos[1]}_t{ties}"
            return {
                "round_id": round_id,
                "result": "tie",
                "table_id": table_id,
                "table_name": tname,
                "player_score": None,
                "banker_score": None,
                "player_pair": False,
                "banker_pair": False,
            }

        # Big Road形式: {"pos":[col,row], "s":score, "c":"B"/"R", ...}
        color = entry.get("c", "")
        result = EVO_COLOR_MAP.get(color)

        if not result:
            return None

        score = entry.get("s")
        pp = bool(entry.get("pp"))
        bp = bool(entry.get("bp"))
        nat = bool(entry.get("nat"))

        round_id = f"evo_{table_id}_s{shoe_epoch}_c{pos[0]}r{pos[1]}"

        result_info = {
            "round_id": round_id,
            "result": result,
            "table_id": table_id,
            "table_name": tname,
            "player_score": score if result == "player" else None,
            "banker_score": score if result == "banker" else None,
            "player_pair": pp,
            "banker_pair": bp,
            "natural": nat,
            "ties": entry.get("ties", 0),
        }

        return result_info

    def _format_entry_short(self, entry) -> str:
        """結果エントリを短い表示形式に変換"""
        if isinstance(entry, str):
            mapping = {"player": "🔵P", "banker": "🔴B", "tie": "🟢T"}
            return mapping.get(entry.lower(), "?")

        if isinstance(entry, dict):
            color = entry.get("c", "")
            score = entry.get("s", "?")
            ties = entry.get("ties", 0)
            emoji = {"B": "🔵P", "R": "🔴B"}.get(color, "?")
            tie_str = f"+🟢T×{ties}" if ties else ""
            return f"{emoji}({score}){tie_str}"

        return "?"

    def get_ws_results(self) -> list[dict]:
        """WebSocket経由で受信した未処理の結果を取得"""
        with self._lock:
            results = list(self._ws_results)
            self._ws_results.clear()
        return results

    # テーブル選定から除外するキーワード (get_all_table_configs / _resolve_target_table 共通)
    # 除外理由:
    # - salon/prive/elite vip/first person/rng: 高額/RNG/特殊
    # - lightning/prosperity/golden wealth/peek/control squeeze/no commission: コミッション/特殊ルール
    # - super speed: BET タイミングが間に合わない
    # - always 9: Banker dominant な特殊ルール (Player BET 不利)
    # - ao vivo / rápido / rapido: ポルトガル語版 (DOM 構造が標準と違い iframe 死亡)
    # - en vivo / en directo: スペイン語版
    # - en direct: フランス語版
    # - in diretta: イタリア語版
    # - 直播 / ライブ (中国語/日本語ローカル名): 同上
    _TABLE_EXCLUDE = ("salon", "prive", "first person", "rng",
                      "lightning", "prosperity", "golden wealth",
                      "peek", "control squeeze", "no commission",
                      "elite vip", "super speed", "always 9",
                      "ao vivo", "rápido", "rapido",
                      "en vivo", "en directo",
                      "en direct", "in diretta",
                      "直播", "ライブカジノ")

    def get_all_table_configs(self) -> dict[str, dict]:
        """全バカラテーブルのconfig (選定用)。除外テーブルはフィルタ済み。"""
        with self._lock:
            return {
                tid: cfg for tid, cfg in self._evo_table_configs.items()
                if not any(ex in cfg.get("title", "").lower() for ex in self._TABLE_EXCLUDE)
            }

    def get_players_count(self, table_id: str | None = None):
        """参加者数取得。table_id指定なら1件、なしなら全件dict"""
        with self._lock:
            if table_id is None:
                return dict(self._evo_players_count)
            return self._evo_players_count.get(table_id, 0)

    def get_raw_history(self, table_id: str) -> list:
        """テーブルの生履歴エントリを取得 (c=B/R, ties等)"""
        with self._lock:
            return list(self._evo_table_raw_histories.get(table_id, []))

    def get_new_shoe_signals(self) -> dict[str, bool]:
        """新シュー信号があるテーブルをチェックして消費する"""
        with self._lock:
            signals = {}
            for tid, sig in self._new_shoe_signals.items():
                if sig:
                    signals[tid] = True
                    self._new_shoe_signals[tid] = False
            return signals

    def has_new_shoe_signal(self) -> bool:
        """後方互換: いずれかのテーブルで新シュー信号があるか"""
        return any(self._new_shoe_signals.values())

    def process_results(self, results: list[dict]) -> int:
        """結果をDBに保存。新規挿入数を返す"""
        new_count = 0
        for r in results:
            round_id = r.get("round_id", "")
            if not round_id or round_id == self.last_round_id:
                continue

            tname = r.get("table_name", self.table_name)
            inserted = insert_round(
                table_name=tname,
                round_id=round_id,
                result=r["result"],
                player_pair=r.get("player_pair", False),
                banker_pair=r.get("banker_pair", False),
                player_score=r.get("player_score"),
                banker_score=r.get("banker_score"),
            )
            if inserted:
                new_count += 1
                self.last_round_id = round_id
                self.round_count += 1
                emoji = {"player": "🔵P", "banker": "🔴B", "tie": "🟢T"}.get(r["result"], "?")
                logger.info(
                    f"Round #{self.round_count}: {emoji} "
                    f"[{tname}] id={round_id}"
                )

        return new_count

    def poll_dom_results(self) -> list[dict]:
        """DOM経由の結果取得（ロビーWS方式では不要だが互換性のため残す）"""
        return []

    def take_screenshot(self, name: str = "current"):
        """デバッグ用スクリーンショット"""
        try:
            path = config.SCREENSHOTS_DIR / f"{name}.png"
            self.page.screenshot(path=str(path))
            logger.debug(f"スクリーンショット: {path}")
        except Exception as e:
            logger.error(f"スクリーンショットエラー: {e}")

    def seconds_since_last_ws_message(self) -> float:
        """最後にWSメッセージを受信してからの経過秒数"""
        if self._last_ws_message_time == 0:
            return 999
        return time.time() - self._last_ws_message_time

    def get_last_result_per_table(self) -> dict[str, float]:
        """テーブルごとの最終結果受信時刻を返す"""
        with self._lock:
            return dict(self._last_result_per_table)

    def reload_lobby(self):
        """ロビーページをリロードしてWS再接続する

        リロード後は必ず _last_ws_message_time をリセットし、
        連続リロードループを防止する。
        3回連続失敗時はフルナビゲーションで復帰を試みる。
        """
        # 連続失敗3回以上 → フルナビゲーション
        if self._consecutive_reload_fails >= 3:
            return self._full_navigate_lobby()

        try:
            self._evo_ws_connected = False
            self.page.reload(wait_until="commit", timeout=15000)
            time.sleep(3)

            for _ in range(15):
                if self._evo_ws_connected:
                    self._consecutive_reload_fails = 0
                    logger.info("ロビーリロード後 WS再接続成功 ✅")
                    return True
                time.sleep(1)

            self._consecutive_reload_fails += 1
            logger.warning(f"ロビーリロード後もWS未接続 (連続失敗: {self._consecutive_reload_fails})")
        except Exception as e:
            self._consecutive_reload_fails += 1
            logger.warning(f"ロビーリロードエラー: {e} (連続失敗: {self._consecutive_reload_fails})")
            time.sleep(3)

        # 成功・失敗に関わらずタイムスタンプをリセット（連続リロード防止）
        self._last_ws_message_time = time.time()
        return self._evo_ws_connected

    def _full_navigate_lobby(self):
        """フルナビゲーションでロビーに再移動（リロード連続失敗時の復帰策）"""
        logger.info("フルナビゲーションでロビー復帰を試行...")
        try:
            self._evo_ws_connected = False
            self.page.goto(
                config.BACCARAT_LOBBY_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            time.sleep(8)

            for _ in range(20):
                if self._evo_ws_connected:
                    self._consecutive_reload_fails = 0
                    logger.info("フルナビゲーション後 WS再接続成功 ✅")
                    # テーブルID再解決を待つ
                    for _ in range(15):
                        if self._target_table_ids:
                            break
                        time.sleep(1)
                    self._last_ws_message_time = time.time()
                    return True
                time.sleep(1)

            self._consecutive_reload_fails += 1
            logger.error(f"フルナビゲーション後もWS未接続 (連続失敗: {self._consecutive_reload_fails})")
        except Exception as e:
            self._consecutive_reload_fails += 1
            logger.error(f"フルナビゲーションエラー: {e}")

        self._last_ws_message_time = time.time()
        return False

    def is_alive(self) -> bool:
        """ブラウザセッションが生きているか。

        ページ応答可能かつ、WS接続中 or 最後のWSメッセージから5分以内であれば alive。
        WS再接続中の一時的な切断で偽陰性を防ぐ。
        """
        try:
            self.page.evaluate("1 + 1")
            if self._evo_ws_connected:
                return True
            # WS切断中でも、最後のメッセージから5分以内なら再接続の可能性がある
            if self._last_ws_message_time > 0:
                return (time.time() - self._last_ws_message_time) < 300
            return False
        except Exception:
            return False

    def is_page_alive(self) -> bool:
        """ページ自体が応答するか (WS接続は問わない)"""
        try:
            self.page.evaluate("1 + 1")
            return True
        except Exception:
            return False

    def stop(self):
        """ブラウザを閉じる"""
        try:
            if self._camoufox_ctx:
                self._camoufox_ctx.__exit__(None, None, None)
                self._camoufox_ctx = None
        except Exception as e:
            logger.error(f"停止エラー: {e}")
        try:
            state = self._load_profile_state()
            state.update({"booting": False, "last_stop_ts": time.time()})
            self._save_profile_state(state)
        except Exception:
            pass
        logger.info("スクレイパー停止")
