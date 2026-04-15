# LAPLACE AI Prediction Project

## 概要

LAPLACE (バカラ自動BETシステム) に **AI予測レイヤー** を追加する長期プロジェクト。
単なる〇❌ロジックではなく、**シューの規則性・パターン・時間的特徴量**から次ハンドを予測する機械学習モデルを構築する。

---

## 背景と仮説

### ユーザー観察 (経験則)
- **土曜日は規則性のあるシューが多く勝ちやすい**
- **月末 (アフィリエイター締切時期) は不規則で負けやすい**
- **組み罫線**: カードは完全ランダムではなく、Evolution側が何らかのパターンを持たせている可能性
- バカラ仲間の複数人が同様の傾向を報告している

### 仮説 (数学的定式化)
```
従来の前提: シュー内カード = 独立一様分布 (真のランダム)
我々の仮説: シュー内カード = 時間的・コンテキスト依存の非定常過程
```

この仮説が正しければ、機械学習モデルが統計的優位 (52-55% の勝率) を発見できる可能性がある。
真のランダムなら51%のハウスエッジは越えられないが、コンテキスト依存なら越えられる余地がある。

### 既知のバカラ確率
- Player: **44.62%**
- Banker: **45.86%**
- Tie: **9.52%**
- Banker BET のハウスエッジ約 1.06%、Player BET 約 1.24%

---

## 段階的実装プラン

### フェーズ1: データ収集基盤 ✅ (実装済み)

**ファイル**: `analytics_db.py` / `monitor/run_data_collector.py`

- **DB**: `analytics.sqlite3` (SQLite, WAL mode)
- **対象**: Evolution Lobby に出ている全バカラテーブル (約60〜92)
- **記録内容**:
  - シュー単位: テーブル情報・時間特徴量・結果列・規則性スコア・パターン分布・流れ分析
  - ハンド単位: `hands` テーブル (将来のシーケンス学習用)
- **稼働方法**:
  ```powershell
  cd E:\dev\Cusor\ba\monitor
  python run_data_collector.py
  ```
  - ヘッドレスCamoufox (profile=`collector`)
  - Cookie は `monitor/auth_state/stake_cookies.json` を `auth_state_collector/` にコピー
- **想定蓄積速度**: 1日で 1,500〜3,000 シュー → 1ヶ月で 5〜10万シュー

### フェーズ2: 仮説検証 (データ蓄積 1-2週間後)

統計的に仮説を証明する SQL クエリを実行:

```sql
-- 曜日別の平均規則性スコア (土曜に偏りあるか)
SELECT day_of_week, AVG(regularity_score) as avg_score, COUNT(*) as n
FROM shoes_analytics
GROUP BY day_of_week
ORDER BY day_of_week;

-- 月末 (>=26日) vs 月中の規則性比較
SELECT
  CASE WHEN day_of_month >= 26 THEN 'month_end' ELSE 'mid' END as period,
  AVG(regularity_score) as avg_score,
  COUNT(*) as n
FROM shoes_analytics
GROUP BY period;

-- 週末 vs 平日
SELECT is_weekend, AVG(regularity_score), COUNT(*)
FROM shoes_analytics GROUP BY is_weekend;

-- 時間帯別 (カジノ負荷のピーク時間など)
SELECT hour_of_day, AVG(regularity_score), COUNT(*)
FROM shoes_analytics GROUP BY hour_of_day;

-- テーブル別傾向
SELECT table_name, AVG(regularity_score), COUNT(*) as shoes
FROM shoes_analytics
GROUP BY table_name
ORDER BY avg_score DESC LIMIT 20;
```

**目標**: t検定で有意差 (p < 0.05) を確認する。

### フェーズ3: ベースラインモデル (マルコフ連鎖)

過去データから「直近N手 → 次の手」の条件付き確率を計算:
- N=3, 5, 7 で試す
- 期待精度: 50.3〜50.8% (ランダムに近い)
- 目的: 基準を作って、後のモデルが本当に優位か比較できるようにする

### フェーズ4: ディープラーニングモデル

**候補モデル**:
1. **LSTM** (シーケンス学習、実装容易)
2. **Transformer** (長距離依存捕捉、高精度)
3. **GRU** (LSTM軽量版)

**入力特徴量**:
- 直近20手の履歴 (one-hot P/B/T)
- 曜日、時刻、日、月
- 現時点までの規則性スコア
- 現時点までのパターン分布 (テレコ/ニコニコ/サンイチ/ドラゴン%)
- 現在の連続数、流れ変化数
- 過去5シューの規則性平均 (ディーラー手癖近似)
- テーブルID embedding

**出力**: 次ハンドの P/B 確率分布 (softmax)

**目標精度**: 52-53% (達成できれば長期利益可能)

### フェーズ5: LAPLACE への統合

既存の〇❌ロジックと組み合わせ:
```python
# 擬似コード
prediction = ai_model.predict(current_context)
if prediction.confidence > 0.55:
    if prediction.side == "player":
        # 〇❌ロジック実行 (Player BET)
        place_bet(marubatsu_unit)
else:
    # 見送り (BETしない)
    skip_hand()
```

**期待効果**:
- 予測精度が高い時だけBET → **勝率UP**
- 予測精度が低い時は見送り → **無駄BET削減・損失軽減**
- 〇❌ロジックのチップ単位制御と組み合わせて累計利益最大化

---

## ファイル構成

### 実装済み
- `analytics_db.py` — SQLite DB管理 (`shoes_analytics` + `hands` テーブル)
- `monitor/run_data_collector.py` — 全テーブル監視・DB保存スクリプト
- `shoe.py` — `ShoeTracker.analyze()` で規則性・パターン・流れ判定

### 未実装 (予定)
- `ai/hypothesis_test.py` — SQL検証スクリプト (フェーズ2)
- `ai/markov_baseline.py` — マルコフ連鎖予測 (フェーズ3)
- `ai/model_lstm.py` — LSTM訓練 (フェーズ4)
- `ai/model_transformer.py` — Transformer訓練 (フェーズ4)
- `ai/predict_service.py` — 推論API (フェーズ5)
- `ai/integrate_laplace.py` — LAPLACE統合 (フェーズ5)

---

## 現在の稼働プロセス

1. **LAPLACE GUI** (`gui/` Electron) — 自動テーブル選定で〇❌BET (Dry Run中)
2. **monitor/run_marubatsu.py** — Japanese Speed Baccarat A 検証 → Telegram公開チャンネル投稿
3. **monitor/run_data_collector.py** — 全62テーブルのシュー結果を analytics.sqlite3 に蓄積 **(AI学習用)**

---

## DB スキーマ

### `shoes_analytics`
```sql
CREATE TABLE shoes_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT NOT NULL,
    -- Time features (hypothesis testing)
    day_of_week INTEGER NOT NULL,     -- 0=Mon ... 6=Sun
    day_of_month INTEGER NOT NULL,
    hour_of_day INTEGER NOT NULL,
    month INTEGER NOT NULL,
    year INTEGER NOT NULL,
    is_weekend INTEGER NOT NULL,
    is_month_end INTEGER NOT NULL,
    -- Counts
    hand_count INTEGER NOT NULL,
    player_count INTEGER NOT NULL,
    banker_count INTEGER NOT NULL,
    tie_count INTEGER NOT NULL,
    result_sequence TEXT NOT NULL,    -- "PBBPPBT..."
    -- Streaks
    max_player_streak INTEGER,
    max_banker_streak INTEGER,
    -- Regularity
    regularity_label TEXT,
    regularity_score REAL,
    dominant_pattern TEXT,
    pattern_breakdown TEXT,           -- JSON
    flow_changes INTEGER,
    flow_type TEXT,
    big_road_text TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(table_id, ended_at)
);
```

### `hands`
```sql
CREATE TABLE hands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shoe_id INTEGER NOT NULL,
    hand_index INTEGER NOT NULL,
    result TEXT NOT NULL,             -- 'player' / 'banker' / 'tie'
    FOREIGN KEY(shoe_id) REFERENCES shoes_analytics(id)
);
```

### インデックス
- `idx_shoes_table`, `idx_shoes_time`, `idx_shoes_dow`, `idx_shoes_dom`
- `idx_shoes_weekend`, `idx_shoes_month_end`, `idx_shoes_regularity`

---

## 現実的な評価と注意点

### 儲かる保証はない
- カジノが何十年も生き残ってる理由 = ハウスエッジが破れない
- AIで50.5-51.5%しか達成できない可能性が高い
- 手数料(Banker 5%)を考えると、53%以上の精度が必要

### しかし価値はある
- **1%でも勝率向上 = 無駄BET削減** で結果は大きく変わる
- データ収集自体が副産物として有用 (統計分析、パターン研究)
- 仮に予測精度が上がらなくても、**シュー分類・規則性判定の自動化** は実用価値あり

### 倫理・法的リスク
- Stake の利用規約は遵守する (スクレイピングのみ、BETは人間のように振る舞う)
- カジノ側が検知してアカウント停止するリスクあり
- 検証は Dry Run で続ける、LIVE は慎重に

---

## 次回セッション開始時のチェックリスト

1. **プロセス確認**
   ```powershell
   Get-Process -Name python*, camoufox*, electron*
   ```
2. **データ収集状況**
   ```powershell
   cd E:\dev\Cusor\ba
   python analytics_db.py
   # → Shoes: N  Hands: M を確認
   ```
3. **ログ確認**
   - `monitor/data_collector.log` (データ収集)
   - `monitor/marubatsu.log` (検証チャンネル)
   - `gui/logs/` (LAPLACE本体)
4. **必要に応じて再起動**
   ```powershell
   # Data collector
   cd E:\dev\Cusor\ba\monitor
   Start-Process python -ArgumentList "run_data_collector.py" -WindowStyle Hidden

   # Verification channel
   Start-Process python -ArgumentList "run_marubatsu.py" -WindowStyle Hidden

   # LAPLACE GUI
   cd E:\dev\Cusor\ba\gui
   npx electron . --dev
   ```

---

## 進捗マイルストーン

- [x] 2026-04-05: データ収集基盤構築 (`analytics_db.py`, `run_data_collector.py`)
- [x] 2026-04-05: 62テーブル監視稼働開始
- [ ] 2026-04-12 頃: 初期データ蓄積 1万シュー達成
- [ ] 2026-04-20 頃: 仮説検証 (曜日別・月末の有意差確認)
- [ ] 2026-05-01 頃: マルコフ連鎖ベースライン構築
- [ ] 2026-05-15 頃: LSTM/Transformer 訓練開始
- [ ] 2026-06-01 頃: AI統合 LAPLACE プロトタイプ

---

## 参考: 関連研究・キーワード

- **Time series classification** (時系列分類)
- **Sequence prediction with LSTM/Transformer**
- **Non-stationary stochastic process** (非定常確率過程)
- **Card counting statistical models**
- **Counterfactual regret minimization** (CFR)
- **Reinforcement Learning with imperfect information** (不完全情報下のRL)
