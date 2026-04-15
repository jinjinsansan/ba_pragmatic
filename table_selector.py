"""テーブル選定ロジック (6つの条件を実装)

⚠️  SERVER-ONLY — DO NOT SHIP TO CLIENT ⚠️
このモジュールは compute_score スコアリング式、PLAYERS_PRIMARY /
PLAYERS_RELAXED / MIN_HANDS / MAX_HANDS / DRAGON_LIMIT 等の機密閾値、
および EXCLUDE_TITLE_KEYWORDS ブラックリストを含みます。VPS の
laplace_api (/api/select-table) からのみ import され、client
distribution には含めてはいけません (.dist_excludes 参照)。

条件:
  ① 除外テーブル: Always9, Lightning, XXXtreme, Golden Wealth, Prosperity, Peek,
                    No Commission, Salon Prive, Elite VIP, Stake Exclusive(0.1$)
  ② 参加者数: 10人優先 → 60秒待機後1人まで緩和 → 1人未満は打たない
  ③ バンカー5連続(ドラゴン)回避: 直近5ハンドがバンカー連続なら入らない
  ⑥ ゲーム進行度: 20~40ハンド進行中かつプレイヤー数 > バンカー数

条件④(ユーザー分散)と⑤(ボット検出)はhumanize/agent側で対応。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# === 除外ルール ===

EXCLUDE_FRONTEND_APPS = {
    "baccarat.alwaysCard",                         # Always 9
    "baccarat.lightning,baccarat.v1.lightning",    # Lightning
    "baccarat.xtreme",                              # XXXtreme Lightning
    "baccarat.goldenWealth,baccarat.v0,baccarat",   # Golden Wealth
    "baccarat.prosperity",                          # Prosperity Tree
    "baccarat.peek,baccarat.v0,baccarat",           # Peek
    "baccarat.regular",                             # No Commission (regular のみ)
}

# 候補となる frontendApp
ALLOWED_FRONTEND_APPS = {
    "baccarat.regular,baccarat.v1.regular",
}

# 除外するタイトルキーワード (保険)
EXCLUDE_TITLE_KEYWORDS = [
    "always 9", "lightning", "prosperity", "golden wealth",
    "peek", "control squeeze", "no commission", "xtreme",
    "salon", "prive", "elite vip", "stake exclusive",
    "squeeze", "insurance",
]

# BET制限 (bl.min=1 のみ許可)
REQUIRED_MIN_BET = 1

# 参加者数しきい値
# PRIMARY: 優先候補 (10人以上)
# RELAXED: 60秒待機後の緩和候補 (1人以上)
PLAYERS_PRIMARY = 10
PLAYERS_RELAXED = 1

# 待機時間 (primary閾値が見つからない場合、relaxedに緩和するまでの待機秒数)
RELAX_WAIT_SECONDS = 60

# ゲーム進行度
MIN_HANDS = 20
MAX_HANDS = 40

# ドラゴン回避
DRAGON_LIMIT = 5  # バンカー5連続で回避


@dataclass
class TableCandidate:
    table_id: str
    title: str
    players: int
    hands: int      # 履歴の有効ハンド数 (tie除く)
    p_count: int    # Player 勝ち数
    b_count: int    # Banker 勝ち数
    tie_count: int
    last_5: list[str]  # 直近5ハンドの "P"/"B"/"T"
    score: float = 0.0

    def __str__(self):
        return (f"{self.title} p={self.players} hands={self.hands} "
                f"P={self.p_count} B={self.b_count} T={self.tie_count}")


# === ① 除外判定 ===

def is_excluded(cfg: dict) -> str | None:
    """除外理由を返す。除外対象でなければNone。"""
    title = cfg.get("title", "")
    title_l = title.lower()
    fe = cfg.get("frontendApp", "")
    minbet = cfg.get("bl", {}).get("min", 0)

    if cfg.get("gt") != "baccarat":
        return "not-baccarat-gt"
    if not cfg.get("published", True):
        return "unpublished"
    if minbet != REQUIRED_MIN_BET:
        return f"min=${minbet}"
    if fe not in ALLOWED_FRONTEND_APPS:
        return f"fe={fe}"
    for kw in EXCLUDE_TITLE_KEYWORDS:
        if kw in title_l:
            return f"kw={kw}"
    return None


# === ③ ドラゴン判定 ===

def has_banker_dragon(raw_history: list, limit: int = DRAGON_LIMIT) -> bool:
    """直近 limit ハンド以上が連続バンカーか判定。limit=0 で無効。

    raw_history: [{c: "B"/"R", ties: N, ...}] — "B"=Banker, "R"=Player(Red)
    Evolution履歴は新しい順ではなく bead plate position順なので、末尾が最新。
    ties はセル内の引き分けカウントで、結果自体ではない。
    """
    if limit == 0:
        return False  # dragon avoidance disabled
    if len(raw_history) < limit:
        return False

    last_entries = raw_history[-limit:]
    for e in last_entries:
        if e.get("c") != "B":
            return False
    return True


# === ⑥ 履歴分析 ===

def analyze_history(raw_history: list) -> tuple[int, int, int, int, list[str]]:
    """履歴から P/B/Tカウントと直近5手を返す。

    Returns: (total_hands, p_count, b_count, tie_count, last_5_list)
    """
    p = 0
    b = 0
    ties = 0
    hands_seq: list[str] = []

    for entry in raw_history:
        c = entry.get("c", "")
        tie_cnt = entry.get("ties", 0) or 0

        if c == "R":
            p += 1
            hands_seq.append("P")
        elif c == "B":
            b += 1
            hands_seq.append("B")
        # tie は別カウント
        ties += tie_cnt

    total_hands = p + b
    last_5 = hands_seq[-5:]
    return total_hands, p, b, ties, last_5


# === スコアリング ===

def compute_score(c: TableCandidate) -> float:
    """候補テーブルのスコア (高いほど良い)

    - 参加者数が多いほど良い (50人以上で大きく加点)
    - ハンド数が 20-40 の中央に近いほど良い
    - プレイヤー数 > バンカー数 ほど加点
    """
    score = 0.0

    # 参加者数
    if c.players >= PLAYERS_PRIMARY:
        score += 50 + min((c.players - PLAYERS_PRIMARY) * 0.5, 30)
    else:
        score += c.players  # 30-49は参加者数分

    # ハンド数 (中央が理想)
    mid = (MIN_HANDS + MAX_HANDS) / 2  # 30
    hand_dist = abs(c.hands - mid)
    score += max(20 - hand_dist, 0)

    # プレイヤー優位性
    if c.hands > 0:
        p_ratio = c.p_count / c.hands
        score += (p_ratio - 0.5) * 40  # プレイヤー率60%なら+4点

    return score


# === メイン選定ロジック ===

class TableSelector:
    def __init__(self, scraper):
        self.scraper = scraper
        self._last_primary_check_ts = 0.0
        self._primary_wait_start: float | None = None
        self.excluded_table_ids: set[str] = set()  # ユーザー分散TODO用

    def find_best_table(self, fixed_name: str = None, selector_config: dict = None) -> TableCandidate | None:
        """現在の状況で最適なテーブルを選ぶ。なければNone。

        fixed_name: 指定するとそのテーブル名を含むテーブルのみ候補にする (検証モード用)
        selector_config: GUI から渡される動的しきい値設定
        """
        sc = selector_config or {}
        _players_primary = sc.get("players_primary", PLAYERS_PRIMARY)
        _players_relaxed = sc.get("players_relaxed", PLAYERS_RELAXED)
        _relax_wait = sc.get("relax_wait_sec", RELAX_WAIT_SECONDS)
        _min_hands = sc.get("min_hands", MIN_HANDS)
        _max_hands = sc.get("max_hands", MAX_HANDS)
        _dragon_limit = sc.get("dragon_limit", DRAGON_LIMIT)
        _require_pb = sc.get("require_pb", True)

        configs = self.scraper.get_all_table_configs()
        players = self.scraper.get_players_count()

        candidates: list[TableCandidate] = []
        debug_stats = {"excluded": 0, "no_players_data": 0, "low_players": 0,
                       "dragon": 0, "bad_hands": 0, "bad_pb_ratio": 0}

        for tid, cfg in configs.items():
            if tid in self.excluded_table_ids:
                continue

            reason = is_excluded(cfg)
            if reason:
                debug_stats["excluded"] += 1
                continue

            title = cfg.get("title", tid)
            if fixed_name and fixed_name.lower() not in title.lower():
                continue
            p_count = players.get(tid, None)
            if p_count is None:
                debug_stats["no_players_data"] += 1
                continue

            raw = self.scraper.get_raw_history(tid)
            hands, p, b, tie, last5 = analyze_history(raw)

            if not fixed_name:
                if has_banker_dragon(raw, limit=_dragon_limit):
                    debug_stats["dragon"] += 1
                    continue
                if hands < _min_hands or hands > _max_hands:
                    debug_stats["bad_hands"] += 1
                    continue
                if _require_pb and p <= b:
                    debug_stats["bad_pb_ratio"] += 1
                    continue

            candidates.append(TableCandidate(
                table_id=tid,
                title=title,
                players=p_count,
                hands=hands,
                p_count=p,
                b_count=b,
                tie_count=tie,
                last_5=last5,
            ))

        now = time.time()
        primary_cands = [c for c in candidates if c.players >= _players_primary]
        relaxed_cands = [c for c in candidates if c.players >= _players_relaxed]

        logger.info(
            f"[selector] configs={len(configs)} candidates={len(candidates)} "
            f"primary(>={_players_primary}p)={len(primary_cands)} "
            f"relaxed(>={_players_relaxed}p)={len(relaxed_cands)} "
            f"debug={debug_stats}"
        )

        chosen_list: list[TableCandidate] = []
        if primary_cands:
            chosen_list = primary_cands
            self._primary_wait_start = None
        else:
            if self._primary_wait_start is None:
                self._primary_wait_start = now
                logger.info(f"[selector] No primary(>={_players_primary}p) tables. Waiting {_relax_wait}s...")
                return None
            elif now - self._primary_wait_start < _relax_wait:
                remaining = _relax_wait - (now - self._primary_wait_start)
                logger.info(f"[selector] Still waiting for primary ({remaining:.0f}s left)")
                return None
            else:
                logger.info(f"[selector] Relaxing to {_players_relaxed}p threshold")
                if relaxed_cands:
                    chosen_list = relaxed_cands
                else:
                    logger.info("[selector] No relaxed candidates either. Skipping.")
                    return None

        for c in chosen_list:
            c.score = compute_score(c)
        chosen_list.sort(key=lambda x: -x.score)

        best = chosen_list[0]
        logger.info(f"[selector] BEST: {best} score={best.score:.1f}")
        if len(chosen_list) > 1:
            logger.info("[selector] Top 5:")
            for i, c in enumerate(chosen_list[:5]):
                logger.info(f"  {i+1}. {c} score={c.score:.1f}")
        return best

    def should_exit_table(self, table_id: str, selector_config: dict = None) -> str | None:
        """入場後に条件が崩れたかチェック。退出すべき理由を返す (問題なければNone)"""
        sc = selector_config or {}
        _players_relaxed = sc.get("players_relaxed", PLAYERS_RELAXED)
        _dragon_limit = sc.get("dragon_limit", DRAGON_LIMIT)

        players = self.scraper.get_players_count()
        p_count = players.get(table_id, 0)
        raw = self.scraper.get_raw_history(table_id)

        if p_count < _players_relaxed:
            return f"players dropped to {p_count}"

        if has_banker_dragon(raw, limit=_dragon_limit):
            return "banker dragon detected"

        return None
