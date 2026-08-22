"""人間化エンジン

ブラウザ操作を人間的にするためのユーティリティ。
ベジェ曲線マウス移動、ランダム待機、BETスキップ、休憩判断。
async/sync 両対応。
"""
import math
import random
import asyncio
import time as _time
import logging

logger = logging.getLogger("baccarat.humanize")


class Humanizer:
    """ブラウザ操作を人間的にする"""

    def __init__(self, config: dict):
        self.mouse_speed_min = config.get("mouse_speed_min", 200)
        self.mouse_speed_max = config.get("mouse_speed_max", 600)
        self.bet_interval_min = config.get("bet_interval_min", 2)
        self.bet_interval_max = config.get("bet_interval_max", 8)
        self.skip_bet_probability = config.get("skip_bet_probability", 0.07)
        self.session_minutes_min = config.get("session_minutes_min", 25)
        self.session_minutes_max = config.get("session_minutes_max", 40)
        self.break_minutes_min = config.get("break_minutes_min", 3)
        self.break_minutes_max = config.get("break_minutes_max", 8)
        self.click_offset_px = config.get("click_offset_px", 5)
        self.enabled = config.get("enabled", True)

    async def move_mouse(self, page, target_x: int, target_y: int):
        """ベジェ曲線でマウス移動"""
        if not self.enabled:
            await page.mouse.move(target_x, target_y)
            return

        try:
            current = await page.evaluate("() => ({x: window._mouseX || 0, y: window._mouseY || 0})")
            start_x = current.get("x", random.randint(100, 500))
            start_y = current.get("y", random.randint(100, 400))
        except Exception:
            start_x = random.randint(100, 500)
            start_y = random.randint(100, 400)

        points = self._bezier_curve(start_x, start_y, target_x, target_y)
        duration = random.uniform(self.mouse_speed_min, self.mouse_speed_max) / 1000
        step_delay = duration / len(points) if points else 0

        for px, py in points:
            await page.mouse.move(px, py)
            await asyncio.sleep(step_delay)

        # マウス位置を記録
        try:
            await page.evaluate(f"() => {{ window._mouseX = {target_x}; window._mouseY = {target_y}; }}")
        except Exception:
            pass

    def _bezier_curve(
        self, x0: int, y0: int, x3: int, y3: int, num_points: int = 0
    ) -> list[tuple[int, int]]:
        """2-3制御点のベジェ曲線を生成"""
        if num_points == 0:
            num_points = random.randint(20, 40)

        dx = x3 - x0
        dy = y3 - y0
        dist = math.sqrt(dx * dx + dy * dy)

        spread = max(30, dist * 0.3)
        x1 = x0 + dx * 0.3 + random.uniform(-spread, spread)
        y1 = y0 + dy * 0.3 + random.uniform(-spread, spread)
        x2 = x0 + dx * 0.7 + random.uniform(-spread, spread)
        y2 = y0 + dy * 0.7 + random.uniform(-spread, spread)

        points = []
        for i in range(num_points + 1):
            t = i / num_points
            inv = 1 - t
            bx = inv**3 * x0 + 3 * inv**2 * t * x1 + 3 * inv * t**2 * x2 + t**3 * x3
            by = inv**3 * y0 + 3 * inv**2 * t * y1 + 3 * inv * t**2 * y2 + t**3 * y3
            points.append((int(bx), int(by)))

        return points

    async def click_with_offset(self, page, x: int, y: int):
        """中心から微小ランダムオフセットを加えてクリック"""
        offset = self.click_offset_px
        final_x = x + random.randint(-offset, offset)
        final_y = y + random.randint(-offset, offset)

        await self.move_mouse(page, final_x, final_y)
        await asyncio.sleep(random.uniform(0.05, 0.2))  # ホバー
        await page.mouse.click(final_x, final_y)

    async def click_element(self, page, selector: str):
        """要素をベジェ曲線で移動してからクリック"""
        try:
            box = await page.locator(selector).first.bounding_box()
            if not box:
                logger.warning(f"要素が見つかりません: {selector}")
                return False

            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
            await self.click_with_offset(page, x, y)
            return True
        except Exception as e:
            logger.error(f"要素クリックエラー ({selector}): {e}")
            return False

    async def wait_before_bet(self):
        """BET前の自然な待機"""
        wait = random.uniform(self.bet_interval_min, self.bet_interval_max)
        logger.debug(f"BET前待機: {wait:.1f}秒")
        await asyncio.sleep(wait)

    async def wait_human_like(self, min_sec: float = 0.5, max_sec: float = 2.0):
        """汎用の人間的待機"""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    def should_skip_bet(self) -> bool:
        """確率的にBETをスキップ (デフォルト7%)"""
        skip = random.random() < self.skip_bet_probability
        if skip:
            logger.info("BETスキップ（人間化ランダム）")
        return skip

    def should_take_break(self, session_minutes: float) -> bool:
        """セッション時間に応じて休憩判断"""
        threshold = random.randint(self.session_minutes_min, self.session_minutes_max)
        if session_minutes >= threshold:
            logger.info(f"セッション{session_minutes:.0f}分 → 休憩推奨")
            return True
        return False

    def get_break_duration(self) -> float:
        """休憩時間を返す（秒）"""
        return random.uniform(self.break_minutes_min, self.break_minutes_max) * 60

    def randomize_bet_amount(self, base_amount: float) -> float:
        """BET額に5-15%のランダム変動を加える"""
        variation = random.uniform(0.85, 1.15)
        return round(base_amount * variation, 2)

    # === Sync版 (executor.py用) ===

    def move_mouse_sync(self, page, target_x: int, target_y: int):
        """ベジェ曲線でマウス移動 (sync版)"""
        if not self.enabled:
            page.mouse.move(target_x, target_y)
            return

        try:
            current = page.evaluate("() => ({x: window._mouseX || 0, y: window._mouseY || 0})")
            start_x = current.get("x", random.randint(100, 500))
            start_y = current.get("y", random.randint(100, 400))
        except Exception:
            start_x = random.randint(100, 500)
            start_y = random.randint(100, 400)

        points = self._bezier_curve(start_x, start_y, target_x, target_y)
        duration = random.uniform(self.mouse_speed_min, self.mouse_speed_max) / 1000
        step_delay = duration / len(points) if points else 0

        for px, py in points:
            page.mouse.move(px, py)
            _time.sleep(step_delay)

        try:
            page.evaluate(f"() => {{ window._mouseX = {target_x}; window._mouseY = {target_y}; }}")
        except Exception:
            pass

    def click_with_offset_sync(self, page, x: int, y: int):
        """sync版: 中心から微小オフセットを加えてクリック"""
        offset = self.click_offset_px
        final_x = x + random.randint(-offset, offset)
        final_y = y + random.randint(-offset, offset)

        self.move_mouse_sync(page, final_x, final_y)
        _time.sleep(random.uniform(0.05, 0.2))
        page.mouse.click(final_x, final_y)

    def wait_before_bet_sync(self):
        """sync版: BET前の自然な待機"""
        wait = random.uniform(self.bet_interval_min, self.bet_interval_max)
        logger.debug(f"BET前待機: {wait:.1f}秒")
        _time.sleep(wait)

    def wait_human_like_sync(self, min_sec: float = 0.5, max_sec: float = 2.0):
        """sync版: 汎用の人間的待機"""
        _time.sleep(random.uniform(min_sec, max_sec))
