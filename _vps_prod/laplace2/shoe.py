"""シュー（Shoe）管理 + 罫線分析

バカラの1シュー（6〜8デッキのカードセット）を追跡し、
シュー終了時の統計・規則性判定・パターン分類を計算する。

⚠️  SERVER-ONLY — DO NOT SHIP TO CLIENT ⚠️
このモジュールは規則性スコアリング (_compute_regularity) と
パターン分類の機密ロジックを含みます。VPS の data collector /
marubatsu monitor からのみ import され、client distribution には
含めてはいけません (.dist_excludes 参照)。

分析項目:
  - 規則性 vs 不規則性 判定 (スコア0-100)
  - 大路パターン分類 (テレコ/ニコニコ・ニコイチ/サンイチ/ドラゴン/ブリッジ)
  - 流れの分割数 (1分割〜5分割)
  - 大路罫線テキスト生成
"""
import json
import logging
import statistics
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("baccarat.shoe")

JST = timezone(timedelta(hours=9))

PATTERN_NAMES = {
    "tereko": "テレコ",
    "nikoniko": "ニコニコ・ニコイチ",
    "sanichi": "サンイチ・4落",
    "dragon": "ドラゴン",
    "bridge": "ブリッジ",
}

# 通常1シューは60〜80ハンド。安全マージンを持たせる
MAX_HANDS_PER_SHOE = 100
# 新シュー判定のための無結果時間（秒）
NEW_SHOE_TIMEOUT = 180  # 3分（シャッフル＋カット時間）


class ShoeTracker:
    """1シューの結果を追跡"""

    def __init__(self, table_name: str = ""):
        self.table_name = table_name
        self.results: list[str] = []  # ["player", "banker", "tie", ...]
        self.shoe_number = 0
        self.started_at: datetime | None = None
        self._new_shoe_detected = False

    @property
    def hand_count(self) -> int:
        return len(self.results)

    @property
    def player_count(self) -> int:
        return self.results.count("player")

    @property
    def banker_count(self) -> int:
        return self.results.count("banker")

    @property
    def tie_count(self) -> int:
        return self.results.count("tie")

    @property
    def result_sequence(self) -> str:
        """出目履歴を文字列で返す（例: PPBBPPBBBT）"""
        mapping = {"player": "P", "banker": "B", "tie": "T"}
        return "".join(mapping.get(r, "?") for r in self.results)

    @property
    def max_banker_streak(self) -> int:
        """シュー内のバンカー最大連続数"""
        max_streak = 0
        current = 0
        for r in self.results:
            if r == "banker":
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    @property
    def max_player_streak(self) -> int:
        """シュー内のプレイヤー最大連続数"""
        max_streak = 0
        current = 0
        for r in self.results:
            if r == "player":
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    @property
    def all_streaks(self) -> list[dict]:
        """全ての連続記録を返す（バンカー連続のリスト）"""
        streaks = []
        current_type = ""
        current_count = 0
        for r in self.results:
            if r == current_type:
                current_count += 1
            else:
                if current_type and current_count > 0:
                    streaks.append({"type": current_type, "count": current_count})
                current_type = r
                current_count = 1
        if current_type and current_count > 0:
            streaks.append({"type": current_type, "count": current_count})
        return streaks

    def add_result(self, result: str):
        """結果を追加"""
        if result not in ("player", "banker", "tie"):
            logger.warning(f"不明な結果: {result}")
            return

        if not self.started_at:
            self.started_at = datetime.now(JST)

        self.results.append(result)
        logger.debug(
            f"Shoe #{self.shoe_number} Hand #{self.hand_count}: "
            f"{result.upper()} | 出目: {self.result_sequence[-20:]}"
        )

    def signal_new_shoe(self):
        """WebSocketから新シュー信号を受信"""
        self._new_shoe_detected = True
        logger.info("新シュー信号検出")

    def is_shoe_complete(self) -> bool:
        """シューが完了したかどうか"""
        # 1. WSから新シュー信号があった場合
        if self._new_shoe_detected and self.hand_count > 0:
            return True

        # 2. ハンド数がMAXを超えた場合（安全策）
        if self.hand_count >= MAX_HANDS_PER_SHOE:
            logger.info(f"最大ハンド数({MAX_HANDS_PER_SHOE})到達 — シュー完了と判定")
            return True

        return False

    def analyze(self) -> dict:
        """シュー全体の分析: 規則性判定 + パターン分類 + 流れ分割"""
        streaks = self._compute_streaks()
        regularity = self._compute_regularity(streaks)
        patterns = self._classify_patterns(streaks)
        flow = self._analyze_flow(streaks)
        big_road = self._build_big_road(streaks)
        now = datetime.now(JST)

        return {
            "regularity": regularity["label"],
            "regularity_score": regularity["score"],
            "pattern_breakdown": patterns["breakdown"],
            "dominant_pattern": patterns["dominant"],
            "flow_changes": flow["changes"],
            "flow_type": flow["type"],
            "big_road_text": big_road,
            "day_of_week": now.weekday(),
            "hour_of_day": now.hour,
            "day_of_month": now.day,
        }

    def _compute_streaks(self) -> list[dict]:
        """Tie を除いた P/B の連続 (落) を計算"""
        streaks = []
        current_type = ""
        current_count = 0
        for r in self.results:
            if r == "tie":
                continue
            if r == current_type:
                current_count += 1
            else:
                if current_type:
                    streaks.append({"type": current_type, "len": current_count})
                current_type = r
                current_count = 1
        if current_type:
            streaks.append({"type": current_type, "len": current_count})
        return streaks

    def _compute_regularity(self, streaks: list[dict]) -> dict:
        """規則性スコア (0-100) を計算。高い=規則的"""
        if len(streaks) < 5:
            return {"label": "判定不可", "score": 0}

        lengths = [s["len"] for s in streaks]
        score = 50.0

        # 1. 連続長の分散 — 低い方が規則的
        variance = statistics.variance(lengths) if len(lengths) > 1 else 0
        if variance < 0.5:
            score += 25
        elif variance < 1.5:
            score += 15
        elif variance < 3.0:
            score += 5
        elif variance > 6.0:
            score -= 15
        elif variance > 4.0:
            score -= 5

        # 2. 支配的パターンの割合 — 高い方が規則的
        len_counts = {}
        for ln in lengths:
            bucket = min(ln, 5)
            len_counts[bucket] = len_counts.get(bucket, 0) + 1
        max_ratio = max(len_counts.values()) / len(lengths)
        if max_ratio > 0.6:
            score += 20
        elif max_ratio > 0.45:
            score += 10
        elif max_ratio < 0.25:
            score -= 10

        # 3. 連続パターンの繰り返し検出 (AB AB AB...)
        repeat_score = self._detect_repeating_pattern(lengths)
        score += repeat_score

        # 4. 3落ラインの一貫性
        below3 = sum(1 for ln in lengths if ln <= 3)
        above3 = len(lengths) - below3
        ratio = below3 / len(lengths)
        if ratio > 0.85 or ratio < 0.15:
            score += 10
        elif ratio > 0.7 or ratio < 0.3:
            score += 5

        score = max(0, min(100, score))
        label = "規則性" if score >= 55 else "不規則性"
        return {"label": label, "score": round(score, 1)}

    def _detect_repeating_pattern(self, lengths: list[int]) -> float:
        """連続長の繰り返しパターンを検出 (例: 1,2,1,2,1,2)"""
        if len(lengths) < 6:
            return 0
        best = 0
        for period in (2, 3, 4):
            if len(lengths) < period * 2:
                continue
            matches = 0
            total = 0
            for i in range(period, len(lengths)):
                total += 1
                if lengths[i] == lengths[i - period]:
                    matches += 1
            ratio = matches / total if total > 0 else 0
            if ratio > 0.6:
                best = max(best, 15)
            elif ratio > 0.4:
                best = max(best, 8)
        return best

    def _classify_patterns(self, streaks: list[dict]) -> dict:
        """パターン分類: 各パターンの出現率を計算"""
        if not streaks:
            return {"breakdown": {}, "dominant": "不明"}

        lengths = [s["len"] for s in streaks]
        total = len(lengths)
        counts = {"tereko": 0, "nikoniko": 0, "sanichi": 0, "dragon": 0}

        for ln in lengths:
            if ln == 1:
                counts["tereko"] += 1
            elif ln == 2:
                counts["nikoniko"] += 1
            elif ln <= 4:
                counts["sanichi"] += 1
            else:
                counts["dragon"] += 1

        breakdown = {}
        for key, cnt in counts.items():
            pct = round(cnt / total * 100)
            if pct > 0:
                breakdown[PATTERN_NAMES[key]] = pct

        # ブリッジ検出: 前半テレコ + 後半ドラゴン
        if len(streaks) >= 8:
            mid = len(streaks) // 2
            first_half_1s = sum(1 for s in streaks[:mid] if s["len"] == 1)
            second_half_5s = sum(1 for s in streaks[mid:] if s["len"] >= 5)
            if first_half_1s / mid > 0.5 and second_half_5s >= 2:
                breakdown[PATTERN_NAMES["bridge"]] = round(
                    (first_half_1s + second_half_5s) / total * 100
                )

        # 支配パターン
        dominant = max(breakdown, key=breakdown.get) if breakdown else "不明"
        return {"breakdown": breakdown, "dominant": dominant}

    def _analyze_flow(self, streaks: list[dict]) -> dict:
        """流れの分割数を分析 (規則→不規則の切り替わり回数)"""
        if len(streaks) < 10:
            return {"changes": 1, "type": "1分割"}

        segment_size = max(len(streaks) // 5, 3)
        segments = []
        for i in range(0, len(streaks), segment_size):
            seg = streaks[i:i + segment_size]
            if len(seg) < 2:
                continue
            seg_lengths = [s["len"] for s in seg]
            var = statistics.variance(seg_lengths) if len(seg_lengths) > 1 else 0
            segments.append("regular" if var < 2.0 else "irregular")

        changes = 0
        for i in range(1, len(segments)):
            if segments[i] != segments[i - 1]:
                changes += 1

        flow_map = {0: "1分割", 1: "2分割", 2: "3分割", 3: "4分割"}
        flow_type = flow_map.get(changes, "5分割")
        return {"changes": changes + 1, "type": flow_type}

    def _build_big_road(self, streaks: list[dict]) -> str:
        """大路罫線のテキスト表現を生成"""
        if not streaks:
            return ""
        emoji = {"player": "🔵", "banker": "🔴"}
        max_rows = 6
        grid = []
        for s in streaks:
            col = [emoji.get(s["type"], "?")] * min(s["len"], max_rows)
            grid.append(col)

        # テキスト化 (最大6行 x 列数)
        lines = []
        for row in range(max_rows):
            line = ""
            has_content = False
            for col in grid:
                if row < len(col):
                    line += col[row]
                    has_content = True
                else:
                    line += "　"
            if has_content:
                lines.append(line)
        return "\n".join(lines)

    def get_summary(self) -> dict:
        """シューのサマリー + 分析結果を返す"""
        analysis = self.analyze() if self.hand_count >= 10 else {}
        base = {
            "shoe_number": self.shoe_number,
            "table_name": self.table_name,
            "hand_count": self.hand_count,
            "player_count": self.player_count,
            "banker_count": self.banker_count,
            "tie_count": self.tie_count,
            "result_sequence": self.result_sequence,
            "max_banker_streak": self.max_banker_streak,
            "max_player_streak": self.max_player_streak,
            "started_at": self.started_at.isoformat() if self.started_at else "",
            "ended_at": datetime.now(JST).isoformat(),
        }
        base.update(analysis)
        return base

    def reset(self):
        """新しいシューのためにリセット"""
        self.results.clear()
        self.started_at = None
        self._new_shoe_detected = False
        self.shoe_number += 1
        logger.info(f"新シュー開始: #{self.shoe_number}")
