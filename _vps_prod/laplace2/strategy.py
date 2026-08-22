"""BET判断エンジン — プレイヤー3落ち目狙い + 1-2-3打法

⚠️  SERVER-ONLY — DO NOT SHIP TO CLIENT ⚠️
このモジュールは 1-2-3 打法戦略とテーブル選定ロジックを含みます。
legacy/backtest 用途のみ。client distribution には含めてはいけません
(.dist_excludes 参照)。

テーブル選定:
  ① シューの30%-50%のテーブル (半分以上終了は入らない)
  ② なるべく参加人数が多いテーブル (データ不足時はSpeedBac優先)
  ③ プレイヤー > バンカー のテーブル (P優勢ほど優先)

BETタイミング:
  - プレイヤーが2連続(2落ち)した時に3落ち目をプレイヤーで狙う
  - 当たればそのまま追う (ドラゴン追い)
  - ハズレたら次のP2連続を待つ or テーブルを変える

資金管理 (1-2-3打法):
  - BET額: 基準額 × [1, 2, 3] の順で進行
  - 1回目と2回目の結果が違う → リセット
  - 1回目と2回目の結果が同じ → 3回目へ
  - 3回目の後は必ずリセット
"""
import logging
from shoe import ShoeTracker

logger = logging.getLogger("baccarat.strategy")

TYPICAL_SHOE_HANDS = 70


class BetStrategy:
    """プレイヤー3落ち目狙い + 1-2-3打法"""

    def __init__(self, config: dict):
        self.base_bet = config.get("base_bet", 1.0)
        self._bet_level = 0       # 0=1回目, 1=2回目, 2=3回目
        self._bet_results = []
        self._riding_streak = False

    @property
    def current_bet_amount(self) -> float:
        multipliers = [1, 2, 3]
        return self.base_bet * multipliers[self._bet_level]

    # ─── テーブル選定 ─── 

    @staticmethod
    def is_table_eligible(shoe: ShoeTracker) -> bool:
        """テーブルがBET対象かどうか判定。

        条件:
          ① シューの半分以下 (50%以下)
          ③ プレイヤー数 >= バンカー数
        """
        h = shoe.hand_count
        if h < 6:
            return False
        max_h = int(TYPICAL_SHOE_HANDS * 0.50)  # 35
        if h > max_h:
            return False
        if shoe.player_count < shoe.banker_count:
            return False
        return True

    @staticmethod
    def table_score(shoe: ShoeTracker) -> float:
        """テーブルの優先度スコア (大きいほど良い)。
        P優勢度が高いテーブルを優先。
        """
        p = shoe.player_count
        b = shoe.banker_count
        total = p + b
        if total == 0:
            return 0.0
        return (p - b) / total

    # ─── BET判断 ─── 

    def has_bet_signal(self, shoe: ShoeTracker) -> bool:
        """テーブル内でBETすべきタイミングかどうか。
        P2連続 (2落ち) の直後 → True (3落ち目を狙う)
        ドラゴン追い中 → True
        """
        streaks = shoe._compute_streaks()
        if not streaks:
            return False

        # ドラゴン追い中: 直前Pが3連続以上 → 追い続ける
        if self._riding_streak:
            last = streaks[-1]
            if last["type"] == "player" and last["len"] >= 3:
                return True
            self._riding_streak = False

        # P2連続以上 → 3落ち目を狙う
        if len(streaks) >= 1:
            last = streaks[-1]
            if last["type"] == "player" and last["len"] >= 2:
                return True

        return False

    def get_bet_info(self, shoe: ShoeTracker) -> dict:
        """BET情報を返す。has_bet_signal() が True の時に呼ぶ。"""
        streaks = shoe._compute_streaks()
        last = streaks[-1] if streaks else {"type": "?", "len": 0}
        reason = f"P{last['len']}連続→{last['len']+1}落ち目狙い"
        if self._riding_streak:
            reason = f"ドラゴン追い: P{last['len']}連続→継続"

        return {
            "side": "player",
            "amount": self.current_bet_amount,
            "reason": f"{reason} (1-2-3: {self._bet_level + 1}回目 ${self.current_bet_amount:.0f})",
            "strategy_name": "player_3dan",
        }

    # ─── 旧互換: evaluate (テーブル選定 + BETタイミング一括) ───

    def evaluate(self, shoe: ShoeTracker) -> dict | None:
        """テーブル選定用。is_table_eligible + has_bet_signal の合わせ技。"""
        if not self.is_table_eligible(shoe):
            return None
        if not self.has_bet_signal(shoe):
            return None
        return self.get_bet_info(shoe)

    # ─── 結果記録 + 1-2-3打法 ─── 

    def record_result(self, won: bool):
        """BET結果を記録して1-2-3打法の状態を更新"""
        self._bet_results.append(won)

        if won:
            self._riding_streak = True
        else:
            self._riding_streak = False

        if self._bet_level == 0:
            self._bet_level = 1
        elif self._bet_level == 1:
            if len(self._bet_results) >= 2:
                if self._bet_results[-1] == self._bet_results[-2]:
                    self._bet_level = 2
                else:
                    self._reset_123()
        elif self._bet_level == 2:
            self._reset_123()

    def _reset_123(self):
        self._bet_level = 0
        self._bet_results.clear()
        logger.info("1-2-3打法リセット → 1回目に戻る")

    def reset_losses(self):
        self._reset_123()
        self._riding_streak = False

    def get_status(self) -> dict:
        return {
            "bet_level": self._bet_level + 1,
            "bet_amount": self.current_bet_amount,
            "riding_streak": self._riding_streak,
            "recent_results": ["W" if r else "L" for r in self._bet_results[-5:]],
        }
