# バカラ自動BETシステム — 完全設計書

> **目的**: 新しいDroidチャットがこのファイルだけを読んで、ゼロから自動BETシステムを実装できるようにする。
> **作成日**: 2026-03-30
> **現在の状態**: Phase 1 (監視のみ) 稼働中。本書はPhase 2-4の設計ガイド。

---

## 目次
1. [現行システムの全体像](#1-現行システムの全体像)
2. [ファイル構成と各モジュールの役割](#2-ファイル構成と各モジュールの役割)
3. [Evolution WSプロトコル仕様](#3-evolution-wsプロトコル仕様)
4. [友人の自動BETシステム (実績あり)](#4-友人の自動betシステム-実績あり)
5. [ブラウザ技術の選択肢](#5-ブラウザ技術の選択肢)
6. [自動BETシステム設計](#6-自動betシステム設計)
7. [BET戦略ロジック (記事ベース)](#7-bet戦略ロジック-記事ベース)
8. [検出回避 (Anti-Detection)](#8-検出回避-anti-detection)
9. [開発ステップ](#9-開発ステップ)
10. [重要な注意事項](#10-重要な注意事項)

---

## 1. 現行システムの全体像

### 何をしているか
Stake.com のEvolution Gaming ライブバカラのロビーに接続し、WebSocketを傍受して全Japanese系テーブル(9テーブル)のラウンド結果をリアルタイム監視。結果をSQLiteに記録し、シュー完了時にTelegramへ分析付き通知を送信する。

### 動作フロー
```
1. Camoufoxブラウザ起動 → Stake.comにCookieログイン
2. Evolution バカラロビーページに遷移
3. EvolutionロビーWSを自動検出・傍受
   - WSは wss://babylonstkn.evo-games.com/public/lobby/socket/v2/... 形式
4. lobby.configs → 全テーブル設定 (89テーブル) 受信
5. lobby.histories → 全テーブルの履歴データ受信
6. lobby.historyUpdated → リアルタイム結果更新 (2-3分バッチ)
7. 結果をテーブルごとのShoeTrackerに振り分け
8. シュー完了検出 → 分析 → DB保存 → Telegram通知
```

### 技術スタック
- **Python 3.12** (Windows)
- **Camoufox** — フィンガープリント偽装付きFirefox (Playwright互換)
- **Playwright** — ブラウザ自動操作 + WS傍受
- **SQLite** — ローカルDB
- **Telegram Bot API** — 通知

### 現在の監視対象 (9テーブル)
| テーブル名 | ID | タイプ |
|---|---|---|
| Japanese Speed Baccarat A | JapSpeedBac00001 | Speed (~25秒/ラウンド) |
| Japanese Speed Baccarat B | JapSpeedBac00002 | Speed |
| Japanese Speed Baccarat C | JapSpeedBac00003 | Speed |
| Japanese Speed Baccarat D | JapSpeedBac00004 | Speed |
| Japanese Speed Baccarat E | JapSpeedBac00005 | Speed |
| Japanese Lightning Baccarat | JapLightBac00001 | Lightning |
| Japanese Golden Wealth Baccarat | Japgwbaccarat001 | Golden Wealth |
| Japanese Prosperity Tree Baccarat | JaPTBaccarat0001 | Prosperity Tree |
| Japanese Salon Prive Baccarat | JapSPBacc0000001 | Salon Prive |

---

## 2. ファイル構成と各モジュールの役割

```
E:\dev\Cusor\ba\
├── main.py            # エントリポイント。メインループ。テーブルごとのShoeTracker管理
├── scraper.py         # Camoufox起動、Stake.comログイン、WS傍受、結果パース
├── shoe.py            # ShoeTracker: 1シューの結果追跡 + 分析 (規則性/パターン/流れ)
├── db.py              # SQLite管理。rounds/shoesテーブル。分析カラム含む
├── notify.py          # TelegramNotifier: シュー完了通知、レポート送信
├── config.py          # 設定管理。.envからSTAKE認証情報・Telegram設定を読み込み
├── telegram_auth.py   # Telegramボット初期設定用ユーティリティ
├── requirements.txt   # playwright>=1.40.0, python-dotenv>=1.0.0, requests>=2.31.0
├── .env               # STAKE_USERNAME, STAKE_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
├── .gitignore
├── auth_state/        # Cookieファイル保存ディレクトリ
│   └── stake_cookies.json
├── data/
│   └── baccarat.db    # SQLiteデータベース
├── screenshots/       # デバッグ用スクリーンショット
└── baccarat.log       # 全ログ出力
```

### 各ファイルの詳細

#### scraper.py (~750行) — 最も重要
- `BaccaratScraper` クラス
- `start()` → Camoufox起動 → Cookie読み込み → Stake.comにアクセス → ログイン
- `_register_ws_listener()` → `page.on("websocket", on_ws)` で全WS接続を監視
  - `evo-games.com` を含むURLのWSを検出 → `framereceived` イベントでメッセージ処理
  - **重要**: `on_message(data)` のdataは `dict` の場合あり → `data.get("payload", data)` で抽出
- `_handle_evo_lobby_message(payload)` → JSON解析 → type別にdispatch
- `_resolve_target_table()` → TARGET_TABLE名で部分一致、Japanese系全マッチ
- `_process_histories()` / `_process_history_updated()` → 差分検出 → `_ws_results` に追加
- `_diff_results(old, new)` → `new_len > old_len` で新規エントリ抽出。Tie更新も検出
- `_parse_evo_bead_entry(entry, table_id)` → Evolution Big Road形式をパース
  - `entry["c"]`: "B"=🔵Player, "R"=🔴Banker (Evolutionの色コード)
  - `entry["pos"]`: [col, row] Big Roadの位置
  - `entry["s"]`: スコア, `entry["ties"]`: タイ回数
  - round_id形式: `evo_{table_id}_s{shoe_epoch}_c{col}r{row}`
- マルチテーブル管理:
  - `_target_table_ids: set[str]` — 監視対象テーブルID群
  - `_target_table_names: dict[str, str]` — ID → テーブル名
  - `_shoe_epochs: dict[str, int]` — テーブルごとのシューエポック
  - `_new_shoe_signals: dict[str, bool]` — テーブルごとの新シュー信号
- `reload_lobby()` → ページリロードしてWS再接続。3回失敗で `_full_navigate_lobby()`
- `process_results()` → DBに保存。結果dictに `table_id`, `table_name` フィールド含む

#### shoe.py (~370行)
- `ShoeTracker` クラス — 1テーブル1インスタンス
- `analyze()` → 規則性判定 + パターン分類 + 流れ分割 + 大路罫線テキスト生成
- `_compute_regularity(streaks)` → スコア0-100 (分散/支配率/繰返し/3落ライン)
  - 55以上 = "規則性", 未満 = "不規則性"
- `_classify_patterns(streaks)` → テレコ(1落)/ニコニコ(2落)/サンイチ(3-4落)/ドラゴン(5落+)/ブリッジ
- `_analyze_flow(streaks)` → 1-5分割
- `get_summary()` → 全分析結果を含むdict (DB保存・Telegram通知に使用)

#### main.py (~280行)
- `shoes: dict[str, ShoeTracker]` — テーブルIDごとにShoeTracker管理
- メインループ: シュー信号チェック → WS結果取得 → テーブル振り分け → シュー完了チェック
- WSキープアライブ: 120秒無沈黙でリロード
- セッション生存チェック: 10分無結果で再接続

#### db.py
- `shoes` テーブルに分析カラム9個:
  `regularity, regularity_score, dominant_pattern, pattern_breakdown(JSON), flow_type, flow_changes, day_of_week, hour_of_day, day_of_month`
- `rounds` テーブル: `table_name, round_id(UNIQUE), result, player_pair, banker_pair, player_score, banker_score`

---

## 3. Evolution WSプロトコル仕様

### 接続
- URL: `wss://babylonstkn.evo-games.com/public/lobby/socket/v2/{session_id}?messageFormat=json&device=Desktop&features=opens`
- Stake.comのバカラロビーページ読み込み時にブラウザが自動接続
- セッションIDはページごとに異なる

### メッセージ形式
```json
{"type": "lobby.configs", "args": {"configs": {table_id: {config}}}}
{"type": "lobby.histories", "args": {"histories": {table_id: {"results": [...]}}}}
{"type": "lobby.historyUpdated", "args": {table_id: {"results": [...]}}}
{"type": "lobby.configsUpdated", "args": {"configs": {table_id: {config}}}}
```

### Big Road エントリ形式
```json
{
  "pos": [col, row],   // Big Road上の位置
  "c": "B" or "R",     // B=Player(Blue), R=Banker(Red) ← 注意: 色が逆に見えるが正しい
  "s": 7,              // スコア
  "pp": true,          // Player Pair
  "bp": false,         // Banker Pair
  "nat": true,         // Natural
  "ties": 2            // Tie回数
}
```

### 重要な挙動
- **historyUpdatedはバッチ配信**: リアルタイムではなく2-3分間隔
- **履歴サイズ上限**: テーブルあたり約60エントリ
- **シューリセット**: 履歴が大幅縮小 (例: 60→1) で検出
- **configsは初回のみフル送信**: 以降はconfigsUpdatedで差分

---

## 4. 友人の自動BETシステム (実績あり)

### 基本情報
- **カジノ**: ビットカジノ (クリプト系)
- **対象**: Evolutionバカラ
- **稼働時間**: 24時間連続
- **ブロック**: 一度もなし

### 構成
```
フォルダ/
├── config.ini    # 設定ファイル
├── BaccaratBot.exe   # 本体 (コンパイル済み)
└── (その他リソース)
```

### 動作フロー
```
1. exeクリック → ブラウザ + ターミナル起動
2. カジノにログイン
3. 全テーブルを順番に巡回監視
4. ロジック条件一致テーブル発見 → ロビーからテーブルに入場
5. 1ターン見送り (パターン確認 + 人間的振る舞い)
6. ロジック通りに自動BET開始
7. ターミナルに日本語ログ (BET額、BET先、結果、残高)
8. 手動で停止可能
```

### ブロックされない理由 (推測)
1. **クリプト系カジノはBot対策が緩い** — KYCが緩く、アカウント管理が緩い
2. **Evolutionはプレイヤー管理をカジノに委ねている** — Evolution自体は検出しない
3. **カジノにとってBETユーザーは収益源** — 勝ちすぎなければ排除メリットなし
4. **1ターン見送り等の人間的振る舞い** — 検出回避に寄与
5. **Stakeも同じクリプト系** — 同様にBot対策が緩い可能性が高い

---

## 5. ブラウザ技術の選択肢

### 現在: Camoufox + Playwright
- **利点**: フィンガープリント偽装が最強。Playwright APIで操作が楽
- **欠点**: Firefoxベースで重い (RAM 250MB+)。起動に10秒以上
- **用途**: BETまで含めた完全自動化に最適 (フィンガープリント偽装必須)

### 候補A: Browser Use (https://browser-use.com/)
- **概要**: AIエージェント向けブラウザ自動化プラットフォーム。GitHub 85,000+ stars。Y Combinator出資
- **特徴**:
  - **Stealth Browsers** — 検出不可能なブラウザインフラ (フィンガープリント偽装内蔵)
  - **Web Agents** — AIでブラウザ操作を自然言語指示
  - **195+ 国のプロキシ** — IP分散が可能
  - Playwright上に構築されている
- **インストール**: `pip install browser-use`
- **BETシステムでの活用**:
  - Stealth BrowsersがCamoufoxの代替になる可能性
  - AI Agentモードで「このテーブルに入ってPlayerにBET」のような指示が可能
  - ただしAI APIコストがかかる
- **判定**: 監視にはオーバースペック。BET操作のフォールバック or Stealth Browser部分のみ活用が現実的

### 候補B: Lightpanda (https://github.com/lightpanda-io/browser)
- **概要**: AI・自動化専用ヘッドレスブラウザ。Zig言語で実装
- **特徴**:
  - **超軽量**: レンダリングエンジンなし、DOM + JSのみ
  - **超高速**: Chromium Headlessの10-100倍高速
  - **低メモリ**: Chromiumの数分の1
  - CDP (Chrome DevTools Protocol) 互換
- **BETシステムでの活用**:
  - **監視用に最適**: WSデータ取得だけなら描画不要
  - ただし: Evolution WSのJavaScript初期化が動くか未検証
  - ブラウザフィンガープリント偽装は不明
- **判定**: 監視専用のサブブラウザとして使える可能性あり。BET操作はUI描画が必要なのでCamoufoxが必要

### 推奨構成 (段階的)
```
Phase 1-2: Camoufox + Playwright (現在の構成そのまま)
Phase 3:   Camoufox + Playwright でBET追加
Phase 4:   検討 — Browser Use Stealth Browsers に移行 or Lightpanda で監視を分離

理由:
- まずは動くものを作る (Camoufox実績あり)
- Lightpandaは発展途上で、Evolution WSの互換性が不明
- Browser Useは将来の検出回避強化に有効
```

---

## 6. 自動BETシステム設計

### アーキテクチャ
```
┌─────────────────────────────────────────────────────────────────────┐
│  BaccaratBot (main.py → main.exe)                                  │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────────┐│
│  │   監視モジュール  │  │   判断モジュール  │  │      BETモジュール          ││
│  │  (scraper.py) │→│ (strategy.py)│→│  (executor.py)             ││
│  │              │  │              │  │  テーブル入場→BET→結果確認  ││
│  │  全テーブルWS  │  │  規則性チェック │  │  humanize.pyで人間化     ││
│  │  同時傍受     │  │  パターン判定  │  │                          ││
│  │  (ロビー滞在)  │  │  エントリー判定 │  │  (テーブルに入るのは      ││
│  │              │  │              │  │   1テーブルずつ)           ││
│  └──────────────┘  └──────────────┘  └────────────────────────────┘│
│         ↓                 ↓                     ↓                   │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  DB(SQLite) ← 全ラウンド + BET履歴 + 収支                     │   │
│  │  Telegram ← シュー通知 + BETログ + セッション収支              │   │
│  │  Terminal ← リアルタイムログ (日本語)                          │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 新規ファイル (Phase 3で作成)

#### strategy.py — BET判断エンジン
```python
class BetStrategy:
    """シューの状態を評価し、BETすべきかどうかを判断する"""

    def __init__(self, config: dict):
        self.min_regularity = config.get("min_regularity_score", 60)
        self.strategy_name = config.get("strategy", "yokonagare")

    def evaluate(self, shoe: ShoeTracker) -> dict | None:
        """BETすべきならBET情報を返す。BET不要ならNone"""
        analysis = shoe.analyze()
        if analysis["regularity_score"] < self.min_regularity:
            return None

        if self.strategy_name == "yokonagare":
            return self._yokonagare(shoe, analysis)
        elif self.strategy_name == "regularity":
            return self._regularity_based(shoe, analysis)
        return None

    def _yokonagare(self, shoe, analysis) -> dict | None:
        """横流れ攻略: 2落後の1落を狙う"""
        streaks = shoe._compute_streaks()
        if len(streaks) < 5:
            return None
        # 5列までに3落が1-2個、4落以上なし
        first5 = streaks[:5]
        drops3 = sum(1 for s in first5 if s["len"] == 3)
        drops4plus = sum(1 for s in first5 if s["len"] >= 4)
        if drops4plus > 0 or drops3 > 2:
            return None
        # 直近が2落の場合 → 逆サイドにBET (1落を狙う)
        last_streak = streaks[-1]
        if last_streak["len"] == 2:
            bet_side = "banker" if last_streak["type"] == "player" else "player"
            return {"side": bet_side, "reason": "横流れ: 2落後の1落狙い"}
        return None

    def _regularity_based(self, shoe, analysis) -> dict | None:
        """規則性ベース: 支配パターンに従う"""
        dominant = analysis["dominant_pattern"]
        streaks = shoe._compute_streaks()
        if not streaks:
            return None
        last = streaks[-1]
        if dominant == "テレコ" and last["len"] == 1:
            bet_side = "banker" if last["type"] == "player" else "player"
            return {"side": bet_side, "reason": f"テレコ: 前回{last['type']}→逆"}
        if dominant in ("ニコニコ・ニコイチ",) and last["len"] == 2:
            bet_side = "banker" if last["type"] == "player" else "player"
            return {"side": bet_side, "reason": f"ニコニコ: 2落後→逆"}
        if dominant == "ドラゴン" and last["len"] >= 3:
            return {"side": last["type"], "reason": f"ドラゴン: {last['len']}連続→継続"}
        return None
```

#### executor.py — テーブル入場 + BET操作
```python
class BetExecutor:
    """ブラウザ操作でテーブル入場とBETを実行"""

    def __init__(self, page, humanizer):
        self.page = page
        self.humanizer = humanizer
        self.in_table = False
        self.current_table_id = ""

    async def enter_table(self, table_id: str, table_name: str) -> bool:
        """ロビーからテーブルに入場"""
        # Evolutionロビーのテーブルサムネイルをクリック
        # セレクタ例: [data-table-id="JapSpeedBac00001"]
        # 入場後、テーブル内WSを傍受開始
        ...

    async def place_bet(self, side: str, amount: float) -> bool:
        """BETを実行: side = "player" or "banker" """
        # 1. チップ額を選択 (クリック)
        # 2. BETエリアをクリック (Player or Banker)
        # 3. 確定ボタンをクリック
        # 全操作にhumanizer.move_mouse() + humanizer.wait() を挟む
        ...

    async def exit_table(self) -> bool:
        """テーブルを退出してロビーに戻る"""
        # 戻るボタン or ロビーURLに遷移
        ...
```

#### humanize.py — 人間化エンジン
```python
import random
import math
import asyncio

class Humanizer:
    """ブラウザ操作を人間的にする"""

    def __init__(self, config: dict):
        self.mouse_speed_min = config.get("mouse_speed_min", 200)
        self.mouse_speed_max = config.get("mouse_speed_max", 600)
        self.bet_interval_min = config.get("bet_interval_min", 2)
        self.bet_interval_max = config.get("bet_interval_max", 8)

    async def move_mouse(self, page, target_x: int, target_y: int):
        """ベジェ曲線でマウス移動"""
        # 現在位置を取得
        # ランダムな制御点でベジェ曲線を生成
        # 速度にゆらぎを持たせて移動
        steps = random.randint(20, 40)
        duration = random.uniform(self.mouse_speed_min, self.mouse_speed_max) / 1000
        # 各ステップでpage.mouse.move()
        ...

    async def wait_before_bet(self):
        """BET前の自然な待機"""
        wait = random.uniform(self.bet_interval_min, self.bet_interval_max)
        await asyncio.sleep(wait)

    async def wait_human_like(self, min_sec=0.5, max_sec=2.0):
        """汎用の人間的待機"""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    def should_skip_bet(self) -> bool:
        """確率的にBETをスキップ (5-10%)"""
        return random.random() < 0.07

    def should_take_break(self, session_minutes: int) -> bool:
        """セッション時間に応じて休憩判断"""
        if session_minutes >= random.randint(25, 40):
            return True
        return False
```

### BET実行フロー (詳細)
```
メインループ (main.py内):

while running:
    # === 監視フェーズ (現在と同じ) ===
    ws_results = scraper.get_ws_results()
    → テーブルごとのshoes[tid]に結果追加
    → シュー完了 → 分析 → DB保存 → Telegram通知

    # === BET判断フェーズ (新規) ===
    if not executor.in_table:
        for tid, shoe in shoes.items():
            if shoe.hand_count >= 10:
                bet_info = strategy.evaluate(shoe)
                if bet_info:
                    # 条件一致テーブル発見
                    logger.info(f"BET対象テーブル発見: {shoe.table_name} - {bet_info['reason']}")

                    # テーブル入場
                    success = await executor.enter_table(tid, shoe.table_name)
                    if success:
                        # 1ターン見送り (友人方式)
                        await humanizer.wait_before_bet()
                        logger.info("1ターン見送り完了")

                        # BET実行
                        if not humanizer.should_skip_bet():
                            success = await executor.place_bet(bet_info["side"], bet_amount)
                            if success:
                                logger.info(f"BET実行: {bet_info['side']} ${bet_amount}")
                            # 結果待ち → DB記録 → 次BETへ
                        break  # 1テーブルずつ

    # === 休憩判断 ===
    if humanizer.should_take_break(session_minutes):
        await executor.exit_table()
        break_time = random.uniform(3, 8) * 60
        logger.info(f"休憩: {break_time/60:.1f}分")
        await asyncio.sleep(break_time)
```

### Telegram通知 (BET版)
```
━━━━━━━━━━━━━━━━━━━
💰 BETレポート
📍 Japanese Speed Baccarat A
━━━━━━━━━━━━━━━━━━━

📊 セッション結果
  BET回数: 12
  勝ち: 8 (66.7%)
  負け: 4
  収支: +$32.50

🎯 使用ロジック: 横流れ攻略
✅ テーブル判定: 規則性 (78)
📋 パターン: ニコニコ 45%

💵 資金状況
  開始: $500.00
  現在: $532.50
  本日収支: +$32.50
━━━━━━━━━━━━━━━━━━━
```

### DB追加テーブル (bets)
```sql
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    table_id TEXT NOT NULL,
    shoe_number INTEGER,
    hand_number INTEGER,
    bet_side TEXT NOT NULL,         -- 'player' or 'banker'
    bet_amount REAL NOT NULL,
    result TEXT,                    -- 'win', 'lose', 'tie_push'
    profit REAL DEFAULT 0,
    strategy_name TEXT,
    strategy_reason TEXT,
    regularity_score REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_bets INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_profit REAL DEFAULT 0,
    starting_balance REAL,
    ending_balance REAL
);
```

---

## 7. BET戦略ロジック (記事ベース)

4つの有料記事から抽出した攻略法:

### 戦略A: 横流れ攻略 (最も勝率が高いとされる)
```
条件:
  - 5列までに3落が1-2個、4落以上なし、2落連続あり
  - 20目までの3落以上:2落以下 = 3:7以上

エントリー:
  - 基本: 2落後の1落を狙う (逆サイドにBET)
  - 拡張: 1落が1列以下なら1落も狙う (ただし1落2連続なら止め)

BET方式:
  - フラットBET基本
  - 1回マーチンゲール (1敗→倍額→初期値に戻る)
  - 2回ダランベール (2回目で勝ち負け関係なく初期値に戻る)

注意:
  - 3落から2落の4回目・5回目は3落→4落の可能性
  - 2落連続しない時間帯は回避
  - 2連敗・3連敗 → その時間帯は休止
```

### 戦略B: テレコ狙い
```
条件: テレコ(1落)が50%以上
エントリー: 直近が1落 → 逆サイドBET
```

### 戦略C: ニコニコ狙い
```
条件: ニコニコ(2落)が40%以上
エントリー: 直近が2落 → 逆サイドBET (1落を期待)
```

### 戦略D: ドラゴン追従
```
条件: 5落以上が30目中3回以上
エントリー: 3落以上で同サイド継続BET
```

### 規則性の判断基準 (全戦略共通)
```
- 規則性スコア >= 60 → BET可能
- 規則性スコア < 55 → BET禁止 (回収テーブル)
- 土曜日: 規則性が出やすい傾向
- 日曜日: 不規則が多い傾向
- 月初: 規則性が出やすい
- 月末: 不規則が多い
※ 上記傾向はデータ蓄積で検証する (Phase 2)
```

---

## 8. 検出回避 (Anti-Detection)

### 現在の対策 (監視のみ)
- Camoufoxのフィンガープリント偽装
- ロビーに留まりBETなし
- WSを傍受するだけでサーバーに追加リクエストなし

### BET追加時の追加対策

#### マウス操作
```
- 直線移動禁止 → ベジェ曲線 (2-3制御点)
- 移動速度: 200-600ms (ランダム)
- クリック前ホバー: 50-200ms
- クリック位置: 中心から±5pxのランダムオフセット
- ダブルクリック禁止
```

#### タイミング
```
- BET間隔: 2-8秒のランダム
- ラウンド開始から BETまで: 3-10秒 (即座にBETしない)
- セッション: 20-40分プレイ → 3-8分休憩
- 1日の稼働: 最大8-12時間 (24時間連続は避ける ← ただし友人は24時間OK)
```

#### BET行動
```
- 5-10%の確率でBETをスキップ
- たまに少額で「負け筋」にBET (カモフラージュ)
- BET額に5-15%のランダム変動
- 1テーブルに長時間滞在しない (最大3シュー)
```

#### テクニカル
```
- User-Agent: Camoufoxが自動管理
- Canvas/WebGL/Audio FP: Camoufoxが自動偽装
- WebRTC: リーク防止設定
- タイムゾーン: 日本 (JST) 固定
- 言語: ja-JP 固定
```

---

## 9. 開発ステップ

### Phase 1: データ蓄積 ✅ 完了
- 9テーブル同時監視
- 規則性・パターン・流れ分析
- SQLite + Telegram通知

### Phase 2: バックテスト + ロジック検証 (次のステップ)
```
目的: 蓄積データで「もしBETしていたら」をシミュレーション

実装:
1. backtest.py を新規作成
2. SQLiteのroundsテーブルからシュー単位でデータ取得
3. 各戦略 (横流れ/テレコ/ニコニコ/ドラゴン) を適用
4. 仮想BETの勝率・収支をシミュレーション
5. 最適パラメータ (規則性閾値、エントリー条件) を決定

出力例:
  戦略A (横流れ): 勝率 62.3%, 月間収支 +$340 (1000シュー中BET対象230)
  戦略B (テレコ): 勝率 58.1%, 月間収支 +$120

必要データ量: 最低500シュー (約1週間の監視)
```

### Phase 3: BET機能実装
```
1. strategy.py — BET判断エンジン作成
2. humanize.py — マウス移動/タイミング人間化
3. executor.py — テーブル入場 + BET操作
   - Evolutionテーブル画面のDOM構造を調査
   - チップ選択 → エリアクリック → 確定の3ステップ
   - 結果確認 (テーブル内WSまたはDOM監視)
4. main.py — BETモードの追加 (--bet フラグ)
5. db.py — bets / sessions テーブル追加
6. notify.py — BETログ通知
7. デモモード: --dry-bet で実BETなしにログだけ出す

テスト手順:
  1. まず --dry-bet でロジックが正しく判断するか確認 (最低3日)
  2. 次に最小BET額で実BET (最低1週間)
  3. 問題なければBET額を段階的に引き上げ
```

### Phase 4: パッケージング
```
1. config.ini で全設定を外部化 (.envから移行)
2. PyInstallerでexe化
   pip install pyinstaller
   pyinstaller --onefile --name BaccaratBot --console main.py
3. フォルダ構成:
   BaccaratBot/
   ├── BaccaratBot.exe
   ├── config.ini
   ├── .env              # 機密情報
   ├── auth_state/       # Cookie
   └── data/             # DB
```

### config.ini 設計 (Phase 4)
```ini
[casino]
platform = stake
url = https://stake.com
lobby_url = https://stake.com/casino/games/evolution-baccarat-lobby
target_tables = Japanese Baccarat

[monitor]
poll_interval = 5
ws_silence_threshold = 120
max_retries = 10

[bet]
enabled = false
min_bet = 1.00
max_bet = 10.00
daily_loss_limit = 100.00
daily_profit_target = 50.00
strategy = yokonagare
demo_mode = true

[strategy.yokonagare]
min_regularity_score = 60
max_drop = 3
entry_after_drop2 = true
flat_bet = true
martingale_max = 1
max_consecutive_loss = 3

[humanize]
enabled = true
mouse_speed_min = 200
mouse_speed_max = 600
bet_interval_min = 2
bet_interval_max = 8
session_minutes_min = 25
session_minutes_max = 40
break_minutes_min = 3
break_minutes_max = 8
skip_bet_probability = 0.07

[notification]
telegram_enabled = true
notify_every_bet = false
notify_session_summary = true
notify_shoe_complete = true
```

---

## 10. 重要な注意事項

### 資金管理 (最重要)
- **日次損失上限を必ず設定** — 超えたら自動停止
- **日次利益目標を設定** — 達成したら停止 (深追い禁止)
- **BET額は残高の1-2%以下** — リスク管理
- **3連敗で一旦停止** → テーブル変更 or 休憩

### セキュリティ
- **パスワード・トークンは .env にのみ保存** — config.ini / ログに絶対書かない
- **Git にpush禁止** — .env, auth_state/, data/ は .gitignore 済み
- **ログにBET額は記録OK** — ただしパスワードは絶対NG

### デモモードファースト
- **必ずデモモード (BETなし) で最低3日間テスト** してからBET開始
- ロジックの判断精度をログで確認
- 「もしBETしていたら」の仮想収支を毎日確認

### カジノリスク
- **アカウント凍結のリスクはゼロではない** — 友人は24時間OKだが保証はない
- **複数アカウント禁止** — 凍結時のリカバリ用アカウントは作らない
- **出金は少額ずつ** — 一度に大額出金しない
