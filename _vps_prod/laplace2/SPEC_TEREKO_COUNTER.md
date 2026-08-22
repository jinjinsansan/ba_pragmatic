# テレコ逆張りモード 実装仕様書

## 概要

LAPLACE に新しい BET モード「テレコ逆張り (counter)」を追加する。
既存の sync / 1drop / mix / pattern とは独立した新手法。

### コンセプト

- ロビーから全62テーブルの大路をリアルタイム監視
- 「テレコ状態」のテーブルを自動選定して入室
- 逆張り (前手の逆にBET) で Player / Banker 両方に BET
- テレコ崩壊を検知したら即退室 → 次のテレコテーブルへ
- 資金管理は 〇✖ MaruBatsu ($50利確) または $1フラットBET

### バックテスト根拠

- 17,240 シュー / 62 テーブル / 7日間のデータで検証済み
- テレコ混合シューでの逆張り勝率: **53.50%**
- 最適パラメータ (200パターン総当たりで導出):
  - 入室: 直近15列で 1落ち+2落ちが 85%以上
  - 退室: 3落ち以上×2回 or 5落ち×1回
- レポート: `report/entry_exit_optimization.html` 参照

---

## 1. 新規 BET モード

### 1.1 モード名

```
bet_mode = "counter"       # テレコ逆張り (〇✖ MaruBatsu)
bet_mode = "counter_flat"  # テレコ逆張り ($1 フラットBET、開発テスト用)
```

### 1.2 GUI での選択

`gui/src/renderer/app.js` の `inputBetMode` に `"counter"` / `"counter_flat"` を入力可能にする。
将来的にはドロップダウンに追加しても良いが、まずはテキスト入力で動作させる。

### 1.3 既存モードとの関係

| モード | 動作 | 変更 |
|---|---|---|
| 1drop | 既存 | 変更なし |
| mix | 既存 | 変更なし |
| sync | 既存 | 変更なし |
| pattern | 既存 | 変更なし |
| pattern_test | 既存 | 変更なし |
| **counter** | **新規** | テレコ逆張り + 〇✖ |
| **counter_flat** | **新規** | テレコ逆張り + $1フラット |

---

## 2. テーブル選定 (入室条件)

### 2.1 ロビー監視

既存の `scraper.py` の `_evo_table_raw_histories` で全テーブルの bead road はリアルタイムに取得済み。
この bead road から「大路の列長リスト」をリアルタイムに計算する。

### 2.2 大路列長の計算

```python
def compute_column_lengths(bead_road: list[str]) -> list[int]:
    """bead road (P/B/T のリスト) から大路の列長リストを返す。
    
    例: ['B','P','B','B','P','P'] → [1, 1, 2, 2]
    タイ (T) は無視。
    """
    columns = []
    current_len = 0
    last_side = None
    for ch in bead_road:
        if ch == 'T':
            continue
        if ch == last_side:
            current_len += 1
        else:
            if last_side is not None:
                columns.append(current_len)
            current_len = 1
            last_side = ch
    if current_len > 0:
        columns.append(current_len)
    return columns
```

### 2.3 入室条件

```python
ENTRY_WINDOW = 15       # 直近何列で判定
ENTRY_THRESHOLD = 0.85  # 1落ち+2落ちの割合

def is_tereko_state(column_lengths: list[int]) -> bool:
    """テレコ状態か判定"""
    if len(column_lengths) < ENTRY_WINDOW:
        return False
    recent = column_lengths[-ENTRY_WINDOW:]
    short_count = sum(1 for L in recent if L <= 2)
    return (short_count / len(recent)) >= ENTRY_THRESHOLD
```

**入室判定のタイミング:**
- BET中のテーブルがない時 (退室直後 or 初回起動時)
- 全テーブルの `_evo_table_raw_histories` を走査
- `is_tereko_state()` が True のテーブルの中から1つ選んで入室
- 複数テーブルが条件を満たす場合: 直近15列中の短列率が最も高いテーブルを選ぶ

### 2.4 テーブル選定の実装場所

`agent_api.py` 内の counter モード用ループで、`scraper._evo_table_raw_histories` を参照して
`is_tereko_state()` を呼ぶ。既存の `table_selector.py` の `select_table()` とは別系統。

---

## 3. 退室条件

### 3.1 パラメータ

```python
EXIT_DROP3_LIMIT = 2    # 入室後に3落ち以上が2回で退室
EXIT_DROP5_IMMEDIATE = True  # 5落ちが1回で即退室
```

### 3.2 退室判定

```python
def should_exit(columns_since_entry: list[int], current_column_length: int) -> str | None:
    """退室すべきか判定。退室理由を返す (None=継続)。
    
    Args:
        columns_since_entry: 入室後に確定した列長リスト
        current_column_length: 現在進行中の列の長さ
    Returns:
        退室理由の文字列 or None
    """
    # 確定列 + 進行中の列をチェック対象に
    check = list(columns_since_entry)
    if current_column_length >= 3:
        check.append(current_column_length)
    
    # 5落ち即退室
    if any(L >= 5 for L in check):
        return "5落ち発生"
    if current_column_length >= 5:
        return "5落ち発生(進行中)"
    
    # 3落ち以上が2回で退室
    drop3_count = sum(1 for L in check if L >= 3)
    if drop3_count >= EXIT_DROP3_LIMIT:
        return f"3落ち以上×{drop3_count}"
    
    return None
```

### 3.3 退室判定のタイミング

- **毎ハンドの結果が出た直後** (BET前ではなく結果確認後)
- 退室条件が満たされたら:
  1. テーブルから退室 (ロビーに戻る)
  2. ログに退室理由を出力
  3. 次のテレコテーブルを探す (入室条件に戻る)

### 3.4 退室時の〇✖セッション

- 退室しても〇✖セッションは **リセットしない**
- セッションの途中 (7ターン未完了) で退室した場合、次のテーブルで続きのターンから再開
- セッションが $50 に達したらリセット (通常の利確)

---

## 4. BET ロジック (逆張り)

### 4.1 逆張りルール

```
前手が P → Banker に BET
前手が B → Player に BET
前手が T → T は無視、その前の P/B を基準にする
シュー最初のハンド (前手なし) → BET しない (SKIP)
```

### 4.2 BET 側の決定

```python
def decide_counter_bet(last_non_tie: str | None) -> str | None:
    """逆張り BET 側を返す。
    
    Args:
        last_non_tie: 直前の非タイ結果 ('P' or 'B')
    Returns:
        'player' or 'banker' or None (BETしない)
    """
    if last_non_tie is None:
        return None  # 前手がない → SKIP
    if last_non_tie == 'P':
        return 'banker'
    if last_non_tie == 'B':
        return 'player'
    return None
```

### 4.3 executor.py への指示

既存の `executor.place_bet(side, amount)` をそのまま使う。
- `side` パラメータ: `"player"` or `"banker"` (既に両方対応済み)
- `amount`: 〇✖ SEQ から計算された金額、または $1 (flat)

```python
# counter モード
side = decide_counter_bet(last_non_tie)
if side is not None:
    amount = get_current_unit()  # 〇✖ SEQ[unit_idx]
    executor.place_bet(side, amount)
```

### 4.4 Banker 手数料の扱い

- Banker BET 時に ×1.0526 増額は **しない** (シンプルさ優先)
- Banker 勝利時は 5% 手数料が自動的に引かれる (Stake.com の仕様)
- 勝率 53.50% で 5% 手数料を吸収できることはバックテストで確認済み
- 〇✖ の勝敗判定: BET 額に対して利益が出たら ○、損失が出たら ✖
  - Player 勝ち: +amount → ○
  - Banker 勝ち: +amount × 0.95 → ○ (手数料引かれても利益なので ○)
  - 負け: -amount → ✖

---

## 5. 資金管理

### 5.1 counter モード (〇✖ MaruBatsu)

既存の `MaruBatsuBetSession` (`marubatsu_bet.py`) をそのまま使う。

- SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, ...]
- 7ターン1セット
- 利確: cumulative >= $50 でセッションリセット
- 損切: 既存の設定に従う (config の losscut 値)

**変更点:**
- 現在は Player BET のみ → Player / Banker 両方に対応
- `run_round()` に `side` パラメータを追加するか、外部から side を指定できるようにする

### 5.2 counter_flat モード ($1 フラットBET)

- 毎ハンド $1 固定 BET
- 〇✖ progression なし
- セッション管理: 単純に累計 PNL を追跡
- 利確/損切は設定に従う
- **目的**: 開発者テスト用。逆張りロジックの動作確認に使う

```python
if bet_mode == "counter_flat":
    amount = 1.0  # 固定 $1
    side = decide_counter_bet(last_non_tie)
    if side:
        executor.place_bet(side, amount)
```

---

## 6. agent_api.py のフロー

### 6.1 counter モード用の新しいメインループ

`_run_bet_session_inner()` 内に counter モード用の分岐を追加。
既存の 1drop / sync / pattern 等のフローとは独立。

```python
# _run_bet_session_inner() 内
if effective_mode in ("counter", "counter_flat"):
    _run_counter_mode(config, stop_event, skip_event)
    return
```

### 6.2 _run_counter_mode() の疑似コード

```python
def _run_counter_mode(config, stop_event, skip_event):
    """テレコ逆張りモード メインループ"""
    
    is_flat = (config['bet_mode'] == 'counter_flat')
    
    # 〇✖ セッション (flat なら使わない)
    if not is_flat:
        session = MaruBatsuBetSession(target=50, lc=config['losscut'])
    
    # 状態
    current_table_id = None
    current_table_name = None
    columns_since_entry = []
    current_col_len = 0
    current_col_side = None
    last_non_tie = None
    cumulative_pnl = 0.0
    
    while not stop_event.is_set():
        
        # ─── テーブル選定 (入室) ───
        if current_table_id is None:
            send_log("[counter] テレコテーブル検索中...")
            
            # 全テーブルの bead road から列長を計算
            best_table = find_best_tereko_table()
            
            if best_table is None:
                send_log("[counter] テレコテーブルなし。30秒待機")
                wait(30, stop_event)
                continue
            
            current_table_id, current_table_name = best_table
            columns_since_entry = []
            current_col_len = 0
            current_col_side = None
            
            # テーブルに入室
            send_log(f"[counter] 入室: {current_table_name}")
            send_action(f"Entering {current_table_name}...")
            if not executor.enter_table(current_table_id, current_table_name):
                send_log(f"[counter] 入室失敗: {current_table_name}")
                current_table_id = None
                continue
            
            # テーブルの直近結果から last_non_tie を設定
            bead = get_current_bead_road(current_table_id)
            for ch in bead:
                if ch in ('P', 'B'):
                    last_non_tie = ch
        
        # ─── ハンド結果を待つ ───
        result = wait_for_hand_result(stop_event)
        if result is None:
            break  # 停止
        
        hand = result  # 'P', 'B', or 'T'
        
        # ─── 大路 列長更新 ───
        if hand != 'T':
            if hand == current_col_side:
                current_col_len += 1
            else:
                if current_col_side is not None:
                    columns_since_entry.append(current_col_len)
                current_col_len = 1
                current_col_side = hand
        
        # ─── 退室判定 ───
        exit_reason = should_exit(columns_since_entry, current_col_len)
        if exit_reason:
            send_log(f"[counter] 退室: {current_table_name} ({exit_reason})")
            send_action(f"Exiting: {exit_reason}")
            executor.exit_to_lobby()
            current_table_id = None
            current_table_name = None
            continue  # 次のテレコテーブルを探す
        
        # ─── BET 判定 ───
        side = decide_counter_bet(last_non_tie)
        
        if hand != 'T':
            last_non_tie = hand
        
        if side is None:
            continue  # 前手がない → SKIP
        
        # ─── BET 実行 ───
        if is_flat:
            amount = 1.0
        else:
            amount = session.get_current_unit()
        
        send_log(f"[counter] BET {side.upper()} ${amount}")
        
        # 次のハンドの結果を待ってから BET 結果を判定
        # ※ 実際の実装では executor.place_bet() → 結果待ち の順
        
        bet_success = executor.place_bet(side, amount)
        if not bet_success:
            send_log("[counter] BET失敗")
            continue
        
        # 結果判定
        next_result = wait_for_hand_result(stop_event)
        if next_result is None:
            break
        
        won = (next_result == side[0].upper())  # 'P' or 'B'
        
        if is_flat:
            if won:
                if side == 'banker':
                    cumulative_pnl += amount * 0.95
                else:
                    cumulative_pnl += amount
            else:
                cumulative_pnl -= amount
            send_log(f"[counter] {'WIN' if won else 'LOSS'} PNL=${cumulative_pnl:+.1f}")
        else:
            session.record_result(won)
            # セッション完了チェックは session 内部で処理
```

### 6.3 テーブル検索関数

```python
def find_best_tereko_table() -> tuple[str, str] | None:
    """全テーブルからテレコ状態のテーブルを探す。
    
    Returns: (table_id, table_name) or None
    """
    candidates = []
    for table_id, raw_history in scraper._evo_table_raw_histories.items():
        bead = [entry_to_pb(e) for e in raw_history]  # P/B/T に変換
        col_lengths = compute_column_lengths(bead)
        if is_tereko_state(col_lengths):
            # 短列率を計算 (高いほど良いテレコ)
            recent = col_lengths[-ENTRY_WINDOW:]
            short_rate = sum(1 for L in recent if L <= 2) / len(recent)
            table_name = scraper.get_table_name(table_id)
            candidates.append((table_id, table_name, short_rate))
    
    if not candidates:
        return None
    
    # 短列率が最も高いテーブルを選択
    candidates.sort(key=lambda x: -x[2])
    return (candidates[0][0], candidates[0][1])
```

---

## 7. GUI 表示

### 7.1 LIVE FEED への表示

counter モード動作中は以下のログを GUI に送信:

```
[counter] テレコテーブル検索中...
[counter] 入室: Japanese Speed Baccarat A (短列率93%)
[counter] BET BANKER $1
[counter] WIN +$0.95  PNL=+$3.45
[counter] BET PLAYER $1
[counter] LOSS -$1  PNL=+$2.45
[counter] 退室: Japanese Speed Baccarat A (5落ち発生)
[counter] テレコテーブル検索中...
[counter] 入室: Korean Speed Baccarat G (短列率87%)
```

### 7.2 GUI に表示する統計

既存の Session PNL / Daily PNL / Win/Loss カウントはそのまま使う。
追加で表示できると良いもの (必須ではない):

- 現在のテーブル名
- 入室回数 / 退室回数
- 直近15列の列長 (テレコ度合いの可視化)

---

## 8. 修正ファイル一覧

| ファイル | 変更内容 | 新規/修正 |
|---|---|---|
| `agent_api.py` | `_run_counter_mode()` 追加、counter/counter_flat の分岐 | 修正 |
| `counter_logic.py` | 入室条件・退室条件・逆張り判定の関数群 | **新規** |
| `executor.py` | 変更なし (既に Player/Banker 両対応済み) | 変更なし |
| `scraper.py` | `_evo_table_raw_histories` のアクセサ追加 (必要に応じて) | 軽微修正 |
| `gui/src/renderer/app.js` | counter/counter_flat モードの認識 | 軽微修正 |
| `gui/src/main.js` | 変更なし | 変更なし |
| `marubatsu_bet.py` | Player/Banker 両対応 (side パラメータ追加) | 修正 |

---

## 9. 定数・設定値

```python
# counter_logic.py に定義

# 入室条件
ENTRY_WINDOW = 15           # 直近何列で判定
ENTRY_THRESHOLD = 0.85      # 1落ち+2落ちの割合 (85%)

# 退室条件
EXIT_DROP3_LIMIT = 2        # 3落ち以上がN回で退室
EXIT_DROP5_IMMEDIATE = True # 5落ちが1回で即退室

# BET
FLAT_BET_AMOUNT = 1.0       # counter_flat モードの BET 額

# テレコテーブル検索
SEARCH_INTERVAL = 30        # テレコテーブルが見つからない時の待機秒数
```

---

## 10. テスト手順

### 10.1 開発テスト (counter_flat)

1. GUI で `bet_mode = counter_flat` に設定
2. START
3. ロビーからテレコテーブルを自動選定 → 入室
4. $1 フラットで逆張り BET が実行される
5. 退室条件発動 → ロビーに戻る → 次のテーブルへ
6. LIVE FEED でログを確認

**確認項目:**
- [ ] テレコテーブルが正しく選定される
- [ ] Player / Banker 両方に BET される
- [ ] 逆張り (前手の逆) が正しく動作する
- [ ] 退室条件 (3落ち×2 or 5落ち×1) で退室する
- [ ] 退室後に次のテレコテーブルに入室する
- [ ] タイ (T) は無視される
- [ ] BET 額が $1 固定

### 10.2 本番テスト (counter)

1. `counter_flat` で動作確認後、`counter` に切り替え
2. 〇✖ SEQ progression が正しく動作することを確認
3. $50 利確でセッションリセットされることを確認
4. 退室してもセッションが継続することを確認

---

## 11. 注意事項

### 11.1 既存モードへの影響

- counter / counter_flat は完全に独立した分岐
- 既存の 1drop / mix / sync / pattern には一切影響しない
- 共通インフラ (executor, scraper, GUI) は共有するが、ロジックは分離

### 11.2 シューの切り替わり

- テーブル内でシューが切り替わった場合:
  - 大路の列長は新シューの1列目からリスタート
  - ただし `last_non_tie` は保持 (新シュー最初のハンドも逆張り可能)
  - 入室後の列カウント (`columns_since_entry`) もリセットしない
  - 退室条件は入室時点からの累計で判定

### 11.3 テレコテーブルが見つからない場合

- 30秒待機してリトライ
- バックテストでは平均31.8テーブルが常時テレコ状態
- 実運用で0テーブルになることは極めて稀だが、深夜帯で一時的にあり得る

### 11.4 executor の enter_table 失敗

- 既存の段階的リカバリ (`full_recovery` → 3回リトライ → `restart_browser`) を適用
- リカバリ後は改めてテレコテーブルを検索し直す

---

## 12. 将来の拡張 (今回は実装しない)

- 複数テーブル同時 BET (ブラウザ1つでは不可)
- テレコ度合いに応じた BET 額の調整
- 退室条件のパラメータを GUI から変更可能にする
- Banker BET 時の ×1.0526 増額オプション
