"""テーブル入場 + BET操作 — ゲーム内WS状態ベース

設計:
  - ラウンド状態は GameWSMonitor (game_ws.py) が管理
  - executor は WS状態を基に「いつBETするか」「いつ結果を拾うか」を判断
  - DOM操作は最低限: チップ選択 + BETスポットクリック のみ
  - 結果検出・BETフェーズ検出は全てWS経由 (DOMポーリング廃止)
"""
import re
import time
import logging

logger = logging.getLogger("baccarat.executor")


class BetExecutor:
    def __init__(self, page, game_ws, config: dict, humanizer=None):
        self.page = page
        self.game_ws = game_ws  # GameWSMonitor
        self.humanizer = humanizer
        self.in_table = False
        self.current_table_id = ""
        self.current_table_name = ""
        self.demo_mode = config.get("demo_mode", True)
        self._settled_seen = False
        self._pre_bet_balance = 0.0
        self._available_chips = None  # テーブル入場後にスキャン
        self._chip_plan_cache = {}   # BET額→チップ計画の事前計算キャッシュ
        self._bead_fail_count = 0
        self._bead_last_ok = 0.0
        self._entered_at = 0.0
        self._last_error_type = None
        try:
            self.page.set_default_timeout(10000)
        except Exception:
            pass

    # ─── iframe取得 ───

    def _get_evo_frames(self):
        frames = []
        for frame in self.page.frames:
            url = frame.url or ""
            if "evo-games.com" in url and "/frontend/" in url:
                frames.append(frame)
        return frames

    def _get_evo_inner(self):
        frames = self._get_evo_frames()
        if not frames:
            return None
        # frame_locatorで確認できない場合のフォールバック
        # evaluateはハングする可能性があるので最小限にする
        if len(frames) >= 2:
            return frames[-1]
        return frames[0]

    def _get_evo_game(self):
        frames = self._get_evo_frames()
        return frames[0] if frames else None

    def _get_evo_locator(self):
        # 1) 実 Frame オブジェクト経由（最も信頼性が高い - iframe 再構築の影響を受けにくい）
        try:
            inner_frame = self._get_evo_inner()
            if inner_frame is not None:
                return inner_frame
        except Exception:
            pass
        # 2) フォールバック: より具体的な src パターンで frame_locator
        outer = self.page.frame_locator('iframe[src*="evo-games.com"]').first
        try:
            inner = outer.frame_locator('iframe[src*="frontend"]').first
            return inner
        except Exception:
            return outer.frame_locator('iframe').first

    # ─── テーブル入場 ───

    def enter_table(self, table_id: str, table_name: str) -> bool:
        if self.demo_mode:
            logger.info(f"[DEMO] {table_name}に入ります")
            self.in_table = True
            self.current_table_id = table_id
            self.current_table_name = table_name
            self._entered_at = time.time()
            self._bead_fail_count = 0
            self._bead_last_ok = time.time()
            return True

        try:
            logger.info(f"{table_name}に入ります (table_id={table_id})")
            self.game_ws.reset()

            game = self._get_evo_game()
            if not game:
                all_frames = [f.url for f in self.page.frames]
                logger.error(f"Evolution iframe未検出 — page.url={self.page.url} frames={all_frames}")
                return False

            logger.info(f"Evolution iframe検出OK — hash遷移実行")
            game.evaluate(f"() => {{ window.location.hash = 'table_id={table_id}'; }}")

            # BETスポットが出現するまで待機 (最大60秒)
            for i in range(30):
                time.sleep(2)
                try:
                    evo = self._get_evo_locator()
                    if evo.locator('[data-betspot-destination]').first.is_visible(timeout=2000):
                        logger.info(f"テーブル読込完了 ({(i+1)*2}秒)")
                        break
                except Exception as e:
                    if i % 5 == 4:
                        logger.info(f"テーブル読込待機中... ({(i+1)*2}秒) err={e}")
                # TRY AGAIN ダイアログチェック
                if not self.check_and_dismiss_error():
                    logger.warning("テーブル読込中にエラーダイアログ → 入場失敗")
                    return False
            else:
                try:
                    self.page.screenshot(path=str(__import__('config').SCREENSHOTS_DIR / "entry_timeout.png"))
                except Exception:
                    pass
                logger.error("テーブル読込タイムアウト (60秒)")
                return False

            # スクリーンネームダイアログ処理
            self._dismiss_screen_name()

            self.in_table = True
            self.current_table_id = table_id
            self.current_table_name = table_name
            self._entered_at = time.time()
            self._bead_fail_count = 0
            self._bead_last_ok = time.time()
            self._scan_available_chips()
            logger.info(f"{table_name} 入場完了")
            return True

        except Exception as e:
            logger.error(f"テーブル入場エラー: {e}")
            return False

    def _scan_available_chips(self):
        """テーブル入場後に利用可能なチップ額をスキャンし、全BET額のチップ計画を事前計算"""
        inner = self._get_evo_inner()
        if not inner:
            self._available_chips = None
            self._chip_plan_cache = {}
            return
        try:
            # スタック展開して全チップを表示
            inner.evaluate('() => { const s = document.querySelector(\'[data-role="footer-perspective-chip-stack"]\'); if (s) s.click(); }')
            time.sleep(0.3)
            chips = inner.evaluate("""() => {
                const els = document.querySelectorAll('[data-role="chip"][data-value]');
                const vals = new Set();
                for (const el of els) {
                    const v = parseInt(el.getAttribute('data-value'));
                    if (v > 0) vals.add(v);
                }
                return Array.from(vals).sort((a, b) => a - b);
            }""")
            if chips:
                self._available_chips = chips
                logger.info(f"利用可能チップ: {chips}")
            else:
                self._available_chips = None
                logger.warning("チップスキャン: チップ未検出")
            # スタックを閉じる
            try:
                inner.evaluate('() => { const s = document.querySelector(\'[data-role="footer-perspective-chip-stack"]\'); if (s) s.click(); }')
            except Exception:
                pass
        except Exception as e:
            self._available_chips = None
            logger.warning(f"チップスキャン失敗: {e}")

        # 全BET額 ($1~$50) のチップ計画を事前計算してキャッシュ
        self._chip_plan_cache = {}
        for amt in range(1, 51):
            plan = self._calc_chip_plan(amt)
            self._chip_plan_cache[amt] = plan
        has_2 = self._available_chips and 2 in self._available_chips
        logger.info(f"チップ計画キャッシュ完了: $1~$50 ($2チップ{'あり' if has_2 else 'なし'})")

    def _dismiss_screen_name(self):
        inner = self._get_evo_inner()
        if not inner:
            return
        try:
            has_dialog = inner.evaluate("() => document.body.innerText.toUpperCase().includes('SCREEN NAME')")
            if not has_dialog:
                return
            evo = self._get_evo_locator()
            inp = evo.locator('input[type="text"]').first
            inp.click(timeout=5000)
            inp.fill("BacPlayer1", timeout=5000)
            time.sleep(1)
            evo.locator('button:has-text("PLAY"), button:has-text("Play")').first.click(timeout=5000)
            time.sleep(3)
            logger.info("スクリーンネーム設定完了")
        except Exception:
            pass

    # ─── BETフェーズ待機 ───

    def wait_for_betting_phase(self, timeout: float = 120, skip_round: bool = True) -> bool:
        """BETフェーズを待機 (WS + DOMハイブリッド)

        skip_round=True: 1ラウンド見送り後にBETフェーズを待つ
        skip_round=False: 即座にBETフェーズを待つ (連続BET時)
        """
        if self.demo_mode:
            return True
        return self.game_ws.wait_for_betting_phase(
            timeout, dom_checker=self._is_betting_phase_dom, skip_round=skip_round,
            error_checker=self.check_and_dismiss_error
        )

    def _is_betting_phase_dom(self) -> bool:
        """DOMでBETタイマー(円形カウントダウン)の表示を確認"""
        try:
            evo = self._get_evo_locator()
            return evo.locator('[class*="timerCircleContainer"]').first.is_visible(timeout=2000)
        except Exception:
            return False

    # ─── エラーダイアログ検出・回復 ───

    _error_dialog_count = 0

    def check_and_dismiss_error(self) -> bool:
        """TRY AGAIN / BACK TO LOBBY / SESSION EXPIRED ダイアログを検出して回復。
        Returns: True=回復済み or エラーなし, False=回復不能(再入場が必要)
        """
        try:
            evo = self._get_evo_locator()

            def _vis(locator, ms: int) -> bool:
                try:
                    return locator.first.is_visible(timeout=ms)
                except Exception:
                    return False

            def _click(locator, ms: int):
                try:
                    locator.first.click(timeout=ms, force=True)
                except Exception:
                    pass

            # SESSION EXPIRED / セッション切れ / EV.5 検出 → 即座にFalse (再入場必須)
            # NOTE: evo は Frame のことがあるため get_by_text は使わない（環境により未提供）。
            expired_any = evo.locator(
                'text=/SESSION\\s*EXPIRED|TIMED\\s*OUT|RECONNECT|EV\\.5|AUTHENTICATION\\s*FAILED|ERROR\\s*CODE/i'
            )
            if _vis(expired_any, 300):
                logger.warning("セッション切れ検出 — 再入場が必要")
                # OK / CLOSE / BACK TO LOBBY などが押せるなら押す
                for btn_sel in (
                    'text=/BACK\\s*TO\\s*LOBBY/i',
                    'text=/TRY\\s*AGAIN/i',
                    'text=/OK/i',
                    'text=/CLOSE/i',
                    'text=/BACK/i',
                    'text=/LOBBY/i',
                ):
                    btn = evo.locator(btn_sel)
                    if _vis(btn, 200):
                        _click(btn, 2000)
                        time.sleep(2)
                        break
                self._error_dialog_count = 0
                self._last_error_type = "session_expired"
                return False

            # TRY AGAIN ダイアログ
            back_to_lobby = evo.locator('text=/BACK\\s*TO\\s*LOBBY/i')
            if _vis(back_to_lobby, 300):
                logger.warning("エラーダイアログ検出 → BACK TO LOBBY")
                _click(back_to_lobby, 3000)
                time.sleep(3)
                self._error_dialog_count = 0
                self._last_error_type = "try_again_failed"
                return False

            try_again = evo.locator('text=/TRY\\s*AGAIN/i')
            if _vis(try_again, 500):
                self._error_dialog_count += 1
                if self._error_dialog_count <= 2:
                    logger.warning(f"エラーダイアログ検出 → TRY AGAIN ({self._error_dialog_count}/2)")
                    _click(try_again, 3000)
                    time.sleep(5)
                    self._last_error_type = "try_again"
                    return True
                else:
                    logger.warning("TRY AGAIN 3回失敗 → BACK TO LOBBY")
                    try:
                        back = evo.locator('text=/BACK\\s*TO\\s*LOBBY|BACK/i')
                        _click(back, 3000)
                        time.sleep(3)
                    except Exception:
                        pass
                    self._error_dialog_count = 0
                    self._last_error_type = "try_again_failed"
                    return False
        except Exception:
            pass
        self._error_dialog_count = 0
        self._last_error_type = None
        return True

    # ─── ビーズロード読み取り ───

    def read_bead_road(self) -> str:
        """テーブル内DOMからビーズロードのP/B/T文字列を取得。
        例: "BPBTPPPTBTBBTBBBBPPBT"
        """
        # 方法1: data-role="Bead-road"
        try:
            evo = self._get_evo_locator()
            text = evo.locator('[data-role="Bead-road"]').first.text_content(timeout=3000)
            if text and text.strip():
                road = text.strip()
                if self.in_table:
                    self._bead_fail_count = 0
                    self._bead_last_ok = time.time()
                return road
        except Exception:
            pass
        # 方法2: beadRoadクラス
        try:
            evo = self._get_evo_locator()
            text = evo.locator('[class*="beadRoad"]').first.text_content(timeout=3000)
            if text and text.strip():
                road = text.strip()
                if self.in_table:
                    self._bead_fail_count = 0
                    self._bead_last_ok = time.time()
                return road
        except Exception:
            pass
        # 方法3: roads DIV
        try:
            evo = self._get_evo_locator()
            text = evo.locator('[class*="roads--"]').first.text_content(timeout=3000)
            if text and text.strip():
                road = ''.join(ch for ch in text.strip() if ch in 'PBT')
                if road:
                    if self.in_table:
                        self._bead_fail_count = 0
                        self._bead_last_ok = time.time()
                    return road
        except Exception:
            pass
        logger.warning("ビーズロード取得失敗 — 全方法が失敗")
        if self.in_table:
            self._bead_fail_count += 1
        return ""

    def get_last_streaks_from_bead(self) -> list[dict]:
        """ビーズロードからP/B連続(streaks)を計算。TIEは無視。"""
        road = self.read_bead_road()
        if not road:
            return []
        streaks = []
        current_type = ""
        current_count = 0
        for ch in road:
            if ch == "T":
                continue
            side = "player" if ch == "P" else "banker" if ch == "B" else None
            if not side:
                continue
            if side == current_type:
                current_count += 1
            else:
                if current_type:
                    streaks.append({"type": current_type, "len": current_count})
                current_type = side
                current_count = 1
        if current_type:
            streaks.append({"type": current_type, "len": current_count})
        return streaks

    # ─── BET実行 ───

    def is_shuffle_state(self) -> bool:
        """シャッフル中/ディーラー交代中の状態を検知。

        Layer 1: DOM テキスト (高速 ~100ms)
          - 単一 JS evaluate で全キーワードを一度にスキャン
        Layer 2: AI Vision (Layer 1 空振り時のみ、5秒キャッシュ)
          - Evolution の Canvas 描画シャッフル画面 (DOM では取れない) を検知
          - ANTHROPIC_API_KEY 未設定なら即 False で素通り
        """
        # === Layer 1: DOM テキスト ===
        try:
            inner = self._get_evo_inner()
            if inner:
                found = inner.evaluate(
                    """() => {
                        try {
                            const text = (document.body?.innerText || '').toUpperCase();
                            const keywords = ['SHUFFLING', 'PLEASE WAIT', 'DEALER CHANGE', 'DEALER WILL', 'SHOE CHANGE', 'BE RIGHT BACK'];
                            for (const kw of keywords) {
                                if (text.includes(kw)) return kw;
                            }
                            return null;
                        } catch (e) { return null; }
                    }"""
                )
                if found:
                    logger.warning(f"シャッフル状態検知 (DOM): '{found}'")
                    return True
        except Exception:
            pass

        # === Layer 2: AI Vision (DOM 空振り時のみ、5秒キャッシュ) ===
        try:
            import ai_vision
            if ai_vision.is_enabled() and ai_vision.check_shuffle(self.page):
                logger.warning("シャッフル状態検知 (AI Vision)")
                return True
        except Exception as _ae:
            logger.debug(f"AI shuffle check skipped: {_ae}")

        return False

    def place_bet(self, side: str, amount: float, strict: bool = False) -> bool:
        """チップ選択 → BETスポットクリック → 受理確認

        高速化設計:
        - evo locator / bet_loc は最初に1回取得、失敗時のみ再取得
        - 同じチップ額が連続する場合はチップ切替スキップ
        - BETスポットクリック間のsleepを最小化 (0.15→0.08s)
        - タイムリミット11秒、部分BETでもTrueを返す

        Returns: True=BET受理(全額または部分), False=未BET (シャッフル中含む)
        """
        if self.demo_mode:
            logger.info(f"[DEMO] ${amount:.0f} {side.upper()} BET")
            return True

        # === Phase 1: シャッフル状態の事前チェック (DOM テキスト、高速) ===
        # シャッフル中だと BET が受理されず偽 Tie になる → 事前回避
        # AI Vision は BET 直前では呼ばない (遅延 500ms-2秒のため)
        # AI は観戦モード中 + wait_for_result 失敗時のエラー識別でのみ使用
        if self.is_shuffle_state():
            logger.warning("BET スキップ: シャッフル状態検知 (DOM)")
            return False

        logger.info(f"${amount:.0f} {side.upper()} BETします")

        if self.game_ws and self.game_ws.balance > 0:
            self._pre_bet_balance = self.game_ws.balance
        else:
            self._pre_bet_balance = self._get_balance_dom()
        if self.game_ws:
            self.game_ws.mark_bet_placed()

        dest = "Player" if side == "player" else "Banker"

        amt_int = int(amount)
        chip_plan = self._chip_plan_cache.get(amt_int) if self._chip_plan_cache else None
        if not chip_plan:
            chip_plan = self._calc_chip_plan(amt_int)
        total_clicks = sum(count for _, count in chip_plan)
        logger.info(f"チップ計画: {chip_plan} ({total_clicks}クリック)")

        _bet_start = time.time()
        _time_limit = 13.0

        # locator を最初に1回だけ取得
        evo = self._get_evo_locator()

        # WS でBET phase を検出した直後はDOMチップが描画途中の場合がある。
        # チップ選択がある場合のみ、最大400msだけチップが現れるのを待つ。
        # 既に描画済みなら即座にbreakするので通常時のオーバーヘッドはほぼ無し。
        try:
            for _wait_chips in range(8):  # 8 * 50ms = 400ms
                try:
                    if evo.locator('[data-role="chip"]').first.is_visible(timeout=50):
                        break
                except Exception:
                    pass
                time.sleep(0.05)
        except Exception:
            pass

        # 前回BETの残りチップをクリア (遅延クリックによる超過BET防止)
        try:
            undo = evo.locator('[data-role="undo-button"]').first
            if undo.is_visible(timeout=300):
                undo.click(timeout=300, force=True)
                time.sleep(0.1)
        except Exception:
            pass

        bet_loc = evo.locator(f'[data-betspot-destination="{dest}"]').first
        _current_chip = None  # 現在選択中のチップ額

        for chip_value, count in chip_plan:
            # チップ額が同じなら切替スキップ
            if chip_value != _current_chip:
                if not self._select_chip(evo, chip_value):
                    fallback_clicks = chip_value * count
                    logger.warning(f"チップ${chip_value}不在 → $1 x {fallback_clicks}クリックにフォールバック")
                    if not self._select_chip(evo, 1):
                        total = self._get_total_bet()
                        if total > 0:
                            logger.warning(f"$1チップも選択失敗だが${total:.2f}が置かれている — 部分BETで続行")
                            return True
                        return False
                    chip_value = 1
                    count = fallback_clicks
                _current_chip = chip_value

            for click_i in range(count):
                elapsed = time.time() - _bet_start
                if elapsed > _time_limit:
                    total = self._get_total_bet()
                    if total > 0:
                        logger.warning(f"BETタイムリミット({elapsed:.1f}s) — 部分BET ${total:.2f} で続行")
                        return True
                    logger.error(f"BETタイムリミット({elapsed:.1f}s) — 未BET")
                    return False

                clicked = False
                try:
                    bet_loc.click(timeout=500, force=True)
                    clicked = True
                except Exception:
                    try:
                        evo = self._get_evo_locator()
                        bet_loc = evo.locator(f'[data-betspot-destination="{dest}"]').first
                        bet_loc.click(timeout=500, force=True)
                        clicked = True
                    except Exception:
                        pass

                if not clicked:
                    total = self._get_total_bet()
                    if total > 0:
                        logger.warning(f"クリック失敗だが${total:.2f}が置かれている — 部分BETで続行")
                        return True
                    logger.error(f"BETスポットクリック失敗 (chip=${chip_value} {click_i+1}/{count})")
                    return False

        # BET受理確認: WS BET_CHIP → DOM total-bet の順で検出
        _confirm_deadline = time.time() + 5.0
        while time.time() < _confirm_deadline:
            # WS で CLIENT_BET_CHIP が来ていれば即受理
            if self.game_ws and self.game_ws.status == "Betting":
                # BET_CHIP は status 更新しないが、bet_accepted で Betting のまま
                # → total-bet DOM で確認
                pass
            total = self._get_total_bet()
            if total > 0:
                logger.info(f"BET受理: ${total:.2f}")
                return True
            time.sleep(0.2)

        # DOM確認がタイムアウトでも、BETスポットクリックが成功していれば
        # WS CLIENT_BET_CHIP で受理されている可能性が高い → 楽観的に受理
        if strict:
            logger.warning("BET受理確認タイムアウト — strict=True のため未受理扱い")
            return False
        logger.warning("BET受理確認タイムアウト — クリック成功のため受理とみなす")
        return True

    def _calc_chip_plan(self, amount: int) -> list[tuple[int, int]]:
        """金額を最大3種のチップで組む (チップ切替は最大2回)。
        切替回数が少ない方を優先し、同じ切替回数ならクリック数が少ない方を選ぶ。
        """
        if self._available_chips:
            all_chips = sorted([c for c in self._available_chips if 0 < c <= amount], reverse=True)
            if not all_chips:
                all_chips = [1]
        else:
            all_chips = [c for c in [500, 100, 25, 5, 2, 1] if c <= amount] or [1]

        best_plan = [(1, amount)]
        best_clicks = amount

        # 1種: 単一チップで割り切れる場合
        for c in all_chips:
            if amount % c == 0:
                n = amount // c
                if n < best_clicks:
                    best_plan = [(c, n)]
                    best_clicks = n

        # 2種: 大チップ + 小チップ端数
        for i, big in enumerate(all_chips):
            if big == 1:
                continue
            n_big = amount // big
            rem = amount % big
            if rem == 0 or n_big == 0:
                continue
            # 端数を$1以外の小チップで最適化
            for small in all_chips[i+1:]:
                if small == 0:
                    continue
                if rem % small == 0:
                    clicks = n_big + rem // small
                    if clicks < best_clicks:
                        best_clicks = clicks
                        best_plan = [(big, n_big), (small, rem // small)]
                    break
            else:
                # $1で補完
                clicks = n_big + rem
                if clicks < best_clicks:
                    best_clicks = clicks
                    best_plan = [(big, n_big), (1, rem)]

        # 3種: 大チップ + 中チップ + 小チップ端数
        for i, big in enumerate(all_chips):
            if big <= 1:
                continue
            for j, mid in enumerate(all_chips[i+1:], i+1):
                if mid <= 1:
                    continue
                n_big = amount // big
                for nb in range(n_big, 0, -1):
                    rem = amount - big * nb
                    n_mid = rem // mid
                    if n_mid == 0:
                        continue
                    r = rem - mid * n_mid
                    clicks = nb + n_mid + (r if r > 0 else 0)
                    if clicks < best_clicks:
                        best_clicks = clicks
                        plan = [(big, nb), (mid, n_mid)]
                        if r > 0:
                            plan.append((1, r))
                        best_plan = plan

        return best_plan

    def _select_chip(self, evo, chip_value: int) -> bool:
        """チップ選択。

        WS検出で BET phase に入った直後は DOM チップが未描画の場合があるため、
        timeout 1200ms + 3段階リトライで信頼性を確保する（クラウドPC等のスペック向け）。
        """
        # 1回目: 直接クリック
        try:
            chip = evo.locator(f'[data-role="chip"][data-value="{chip_value}"]').first
            chip.click(timeout=1200, force=True)
            logger.info(f"チップ${chip_value}選択OK")
            return True
        except Exception:
            pass

        # 2回目: スタック展開してリトライ
        try:
            evo.locator('[data-role="footer-perspective-chip-stack"]').first.click(timeout=1200, force=True)
            time.sleep(0.15)
            evo.locator(f'[data-role="chip"][data-value="{chip_value}"]').first.click(timeout=1200, force=True)
            logger.info(f"チップ${chip_value}選択OK (展開後)")
            return True
        except Exception:
            pass

        # 3回目: iframe を再取得してから直接クリック
        # （iframe 再構築後のフォールバック）
        try:
            time.sleep(0.2)
            fresh_evo = self._get_evo_locator()
            fresh_evo.locator(f'[data-role="chip"][data-value="{chip_value}"]').first.click(timeout=1200, force=True)
            logger.info(f"チップ${chip_value}選択OK (iframe再取得後)")
            return True
        except Exception:
            pass

        logger.error(f"チップ${chip_value}選択失敗")
        return False

    def cancel_bet(self) -> bool:
        """BET直後のUNDOを試みる。成功したらTrue。"""
        try:
            evo = self._get_evo_locator()
            undo = evo.locator('[data-role="undo-button"]').first
            if undo.is_visible(timeout=300):
                undo.click(timeout=1000, force=True)
                time.sleep(0.2)
                logger.info("UNDOクリックでBET取消")
                return True
        except Exception:
            pass
        return False

    def _select_chip_value(self, amount: float) -> int:
        chips = [1, 2, 5, 25, 100, 500]
        selected = chips[0]
        for c in chips:
            if c <= amount:
                selected = c
            else:
                break
        return selected

    def _get_total_bet(self) -> float:
        inner = self._get_evo_inner()
        if not inner:
            return 0.0
        try:
            text = inner.evaluate(
                '() => { const e = document.querySelector(\'[data-role="total-bet-label-value"]\'); '
                "return e ? e.textContent : ''; }",
                timeout=2000,
            )
            nums = re.findall(r'[\d.]+', text)
            return float(nums[0]) if nums else 0.0
        except Exception:
            return 0.0

    # ─── 結果待ち ───

    def wait_for_result(self, timeout: float = 60, bet_amount: float = 0) -> dict | None:
        """DOM + WS ハイブリッドで結果を検出。

        Step 1: タイマー消失(ディーリング中)を待つ
        Step 2: タイマー再出現 OR WS Settled/Idle を待つ (DOM失敗時の90sハング防止)
        Step 3: WS multiplier → 残高変化 → DOM の優先順位で結果判定
        """
        if self.demo_mode:
            import random
            time.sleep(random.uniform(2, 5))
            r = random.choices(
                ["player", "banker", "tie"],
                weights=[44.62, 45.86, 9.52],
                k=1,
            )[0]
            logger.info(f"[DEMO] Result: {r}")
            return {"result": r, "balance": 0.0}

        logger.info("結果を待っています...")
        deadline = time.time() + timeout
        pre_balance = self._pre_bet_balance
        try:
            pre_bead = self.read_bead_road() or ""
        except Exception:
            pre_bead = ""

        # Step 1: BETフェーズ終了を待つ (タイマー消失)
        while time.time() < deadline:
            # WS Settled/Idle/Betting で即脱出 (DOM確認より高速)
            if self.game_ws and self.game_ws.status in ("Settled", "Idle"):
                logger.info(f"WS状態で脱出 (Step1): {self.game_ws.status}")
                break
            if not self._is_betting_phase_dom():
                logger.info("ディーリング中...")
                break
            if not self.check_and_dismiss_error():
                return None
            time.sleep(0.15)

        # Step 2: 次のBETフェーズ開始を待つ (タイマー再出現 = 結果確定済み)
        _ws_settled_seen = False
        while time.time() < deadline:
            if self.game_ws:
                ws_status = self.game_ws.status
                if ws_status == "Settled":
                    _ws_settled_seen = True
                if ws_status == "Betting" and _ws_settled_seen:
                    logger.info("WS Betting検出 (Settled後) — DOM待機をスキップ")
                    break
                if _ws_settled_seen and ws_status == "Idle":
                    logger.info("WS Idle検出 (Settled後) — DOM待機をスキップ")
                    break
            if self._is_betting_phase_dom():
                break
            if not self.check_and_dismiss_error():
                return None
            time.sleep(0.15)

        # Step 2.5: ビーズロード差分で結果を優先判定
        bead_result = None
        if pre_bead:
            bead_deadline = min(deadline, time.time() + 8)
            while time.time() < bead_deadline:
                if not self.check_and_dismiss_error():
                    return None
                try:
                    new_bead = self.read_bead_road() or ""
                    if len(new_bead) > len(pre_bead):
                        new_chars = new_bead[len(pre_bead):]
                        for ch in reversed(new_chars):
                            if ch in ("P", "B", "T"):
                                bead_result = {"P": "player", "B": "banker", "T": "tie"}[ch]
                                break
                        if bead_result:
                            logger.info(
                                f"ビーズロード結果検出: {bead_result.upper()} (diff='{new_chars[-5:] if new_chars else ''}')"
                            )
                            break
                except Exception:
                    pass
                time.sleep(0.3)

        # Step 3: WS multiplier (来ないテーブルが多いので最大0.3秒のみ待機)
        ws_result = None
        if self.game_ws:
            ws_result = self.game_ws.get_result_side()
            if not ws_result:
                time.sleep(0.3)
                ws_result = self.game_ws.get_result_side()
            if ws_result:
                logger.info(f"WS結果検出: {ws_result.upper()}")

        # 方法2: WS残高diff (CLIENT_BALANCE_UPDATEDで即更新されるため高速)
        new_balance = self.game_ws.balance if self.game_ws and self.game_ws.balance > 0 else 0.0
        if new_balance <= 0:
            new_balance = self._get_balance_dom()

        balance_result = None
        if bet_amount > 0 and pre_balance > 0 and new_balance > 0:
            diff = new_balance - pre_balance
            logger.info(f"残高変化: ${pre_balance:.2f} → ${new_balance:.2f} (差${diff:+.2f}, BET${bet_amount:.0f})")
            if diff > 0.01:
                balance_result = "player"
            elif diff < -0.01:
                balance_result = "banker"
            else:
                # 残高変化なし → Tie候補だが、WS Settled確認が必要
                # シャッフル中など BET未受理だと残高変化なし＝Tie誤判定の危険
                # _settled_seen_since_bet は BET後にSettled eventを見た場合にTrue
                # (mark_bet_placed でFalseにリセット、_handle_round_updateでTrueに)
                # 現在の status は次ラウンドの "Betting" に移行しているため使えない
                settled_seen = False
                try:
                    if self.game_ws:
                        settled_seen = getattr(self.game_ws, '_settled_seen_since_bet', False)
                except Exception:
                    pass
                if settled_seen:
                    balance_result = "tie"
                else:
                    logger.warning(
                        f"残高変化なし AND BET後Settled未受信 "
                        f"(settled_seen={settled_seen}, status={getattr(self.game_ws, 'status', 'N/A')}) "
                        f"— BET未受理 (Tie誤判定を回避)"
                    )
                    balance_result = None

        # 方法3: DOM結果オーバーレイ
        dom_result = self._detect_result_dom()

        # 最終判定: ビーズロード > WS > 残高 > DOM
        result_side = bead_result or ws_result or balance_result or dom_result

        if not result_side:
            if bet_amount == 0:
                return {"result": "unknown", "balance": new_balance}
            logger.warning(f"結果取得失敗 (ws={ws_result}, balance={balance_result}, dom={dom_result})")
            return None

        logger.info(f"結果検出: {result_side.upper()} (ws={ws_result}, bal={balance_result}, dom={dom_result}) 残高: ${new_balance:.2f}")
        return {"result": result_side, "balance": new_balance}

    def _detect_result_dom(self) -> str | None:
        """DOM: 結果オーバーレイ検出"""
        try:
            evo = self._get_evo_locator()
            for selector in ['[class*="gameResult"]', '[class*="winResult"]', '[class*="resultText"]']:
                loc = evo.locator(selector).first
                if loc.is_visible(timeout=500):
                    text = loc.inner_text(timeout=500).upper().strip()
                    if 'PLAYER' in text:
                        return 'player'
                    if 'BANKER' in text:
                        return 'banker'
                    if 'TIE' in text:
                        return 'tie'
        except Exception:
            pass
        return None

    # ─── 残高 ───

    def get_balance(self) -> float:
        # WS残高を優先 (DOMはiframe内のため更新が遅れる場合がある)
        if self.game_ws and self.game_ws.balance > 0:
            return self.game_ws.balance
        return self._get_balance_dom()

    def _get_balance_dom(self) -> float:
        try:
            evo = self._get_evo_locator()
            text = evo.locator('[data-role="balance-label-value"]').first.inner_text(timeout=3000)
            nums = re.findall(r'[\d.]+', text)
            if nums:
                return float(nums[0])
        except Exception:
            pass
        return 0.0

    # ─── テーブル退出 ───

    def exit_table(self) -> bool:
        if self.demo_mode:
            logger.info("ロビーに戻ります")
            self._reset_state()
            return True

        try:
            logger.info("ロビーに戻ります")

            # lobby-button クリック（タイムアウト付き）
            _lobby_ok = False
            try:
                evo = self._get_evo_locator()
                evo.locator('[data-role="lobby-button"]').first.click(timeout=5000)
                _lobby_ok = True
            except Exception:
                pass

            if not _lobby_ok:
                try:
                    game = self._get_evo_game()
                    if game:
                        game.evaluate("() => { window.location.hash = 'category=baccarat_sicbo'; }", timeout=5000)
                except Exception:
                    pass

            time.sleep(5)
            self.game_ws.reset()
            self._reset_state()

            # iframe が about:blank になっていないか確認（タイムアウト保護付き）
            try:
                evo_check = self._get_evo_game()
                if not evo_check:
                    logger.warning("ロビー復帰後にiframeが消失 — ページリロードします")
                    try:
                        self.page.reload(wait_until="domcontentloaded", timeout=15000)
                        time.sleep(5)
                    except Exception as _rl:
                        logger.warning(f"リロード失敗: {_rl}")
                        # リロードも失敗 → page.goto でフルリカバリ
                        try:
                            self.page.goto("https://stake.com/casino/games/evolution-baccarat-lobby",
                                           wait_until="domcontentloaded", timeout=20000)
                            time.sleep(5)
                        except Exception as _gt:
                            logger.warning(f"page.goto失敗: {_gt}")
            except Exception as _ec:
                logger.warning(f"iframe確認エラー: {_ec}")

            logger.info("ロビー復帰完了")
            return True
        except Exception as e:
            logger.error(f"テーブル退出エラー: {e}")
            self._reset_state()
            return False

    def _reset_state(self):
        self.in_table = False
        self.current_table_id = ""
        self.current_table_name = ""
        self._bead_fail_count = 0
        self._entered_at = 0.0

    def get_bead_fail_count(self) -> int:
        return self._bead_fail_count

    def reset_bead_fail_count(self):
        self._bead_fail_count = 0

    def get_table_uptime(self) -> float:
        if self._entered_at <= 0:
            return 0.0
        return time.time() - self._entered_at

    def get_last_error_type(self) -> str | None:
        return self._last_error_type

    def clear_last_error_type(self):
        self._last_error_type = None
