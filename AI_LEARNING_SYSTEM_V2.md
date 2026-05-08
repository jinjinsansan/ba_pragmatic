# AI 学習システム 第2回 設計ドキュメント

作成日: 2026-05-09

---

## 1. 背景と方向転換

### 第1回（〜2026-05-08）の問題点

| 問題 | 内容 |
|---|---|
| overshoot 未記録 | 各 BET 時点の NEWSEQ 負け越し個数が記録されていなかった |
| 予想フェーズの混在 | 「予想なし BET（OS 0〜10）」と「予想あり BET（OS 11+）」が区別不能 |
| 勝率の誤計測 | 全体 51.8% は予想なし BET を含むため、友人の実際の予想精度を反映していない |
| LOOK の ils フラグなし | 自動 LOOK 記録に `in_learning_session` フラグが付いていなかった |
| UI 表示バグ | `limit=2000` により 1,673 件と過小表示（実際は 4,442 件） |
| 開始日の認識ミス | ユーザーは 4/30 開始と認識していたが、DB では 4/26 から `in_learning_session=1` が存在 |

### 第2回（2026-05-09〜）の方向転換

- 第1回データは削除せず `learning_round = 1` として保持（参考値）
- 第2回データは `learning_round = 2` として明示的に分離
- `overshoot`・`unit_idx` を BET/LOOK 送信時に記録開始
- 公式進捗カウントを DB 直接値（`samples_done`）で表示

---

## 2. NEWSEQ と AI 学習の関係

### NEWSEQ の前提条件

```
勝率 ≥ 52%  → 期待値プラス・NEWSEQ が機能する
勝率 < 51%  → 長期では破綻（資金管理の精度に関わらず）
```

### NEWSEQ の2フェーズ構造

| フェーズ | 負け越し個数（overshoot） | 内容 |
|---|---|---|
| 非予想フェーズ | 0 〜 10 | バンカー固定でほぼルックせずにBET。予想なし。 |
| 予想フェーズ | 11 以上 | 友人の予想根拠が発動。勝率を意識した BET/LOOK。 |

**AI 学習が評価すべきデータは「予想フェーズ（OS 11+）」のみ。**

第1回データには overshoot が記録されていないため、フェーズ分離が不可能。
第2回から初めて正確な計測が可能になる。

---

## 3. 友人の予想根拠（学習対象の特徴量）

### A. シューター内 P/B カウント

- P と B が同数に近いテーブルと、10〜20 差がつくテーブルが存在する
- 前半で P が 10 先行 → 後半で B が追いつくと予想（またはそのまま離れると予想）
- B > P のとき → B が 10 差をつけると予想してバンカー BET
- B < P のとき → B が追いつくまでバンカー BET

### B. 中国罫線（横読み）

- 横方向（左→右）に読む
- PPPPPP → プレイヤーが強い → ルック
- PPBBPPBB → 次は B と予想
- タイを挟んだシンメトリー構造を読む
- 1行上との真逆パターンを読む
- 3列以上でないと読まない
- 上3行 = P 勢力、下3行 = B 勢力として読むこともある

### C. 大路の組罫線

- テレコ・ニコニコ・ニコイチ・サンイチ・ブリッジ等のパターンを常に予想
- 中国罫線の予想と大路の予想が **2つ重なった時** に高確信 BET / LOOK
- バンカーは 4 連以上のドラゴンが多い → バンカー出現中は追い BET 傾向
- 2落ちライン・3落ちラインのブレイク → 強いドラゴン発生と予想

### D. 予想できないテーブル

- 組罫線が全く読めない形のテーブル・時間帯は SWITCH TABLE またはルック増加

---

## 4. 記録データの構造

### 各 BET / LOOK レコードに含まれる情報

```json
{
  "friend_action": {
    "action": "BET",
    "side": "BANKER",
    "in_learning_session": true,
    "overshoot": 15,        ← 第2回から追加
    "unit_idx": 3           ← 第2回から追加
  },
  "snapshot": {
    "shoe_summary": {
      "bankerWinCounter": "18",   ← バンカー数
      "playerWinCounter": "8",    ← プレイヤー数
      "tieCounter": "3",
      "totalGames": "29"
    },
    "statistics": "[[...]]",      ← 中国罫線（6×6グリッド）
    "sequence": "BPPPBB...",      ← 大路シーケンス
    "derived_roads": {
      "big_eye_boy": [...],        ← 大眼仔
      "small_road": [...],         ← 小路
      "cockroach_road": [...]      ← 蟑螂路
    },
    "good_roads_map": {
      "bankerStreak": true,        ← ドラゴン等のパターンフラグ
      "playerPingPong": false,
      ...
    }
  }
}
```

**友人が予想の根拠とする全情報がスナップショットに記録済み。**

---

## 5. DB 構造の変更

### decisions テーブルへの追加列

```sql
ALTER TABLE decisions ADD COLUMN learning_round INTEGER;
```

| learning_round | 意味 | 期間 | overshoot |
|---|---|---|---|
| 1 (または NULL) | 第1回データ（参考値） | 〜2026-05-08 | なし |
| 2 | 第2回データ（公式） | 2026-05-09〜 | あり |

### 第1回データの扱い

- 削除しない
- `learning_round = 1` として保持
- overshoot なしのため AI 学習には使用しない
- 参照・比較用として残す

---

## 6. UI 修正内容（2026-05-09 実施）

| 修正箇所 | 内容 |
|---|---|
| `bacopy_master_ui.py` `renderStatsBar()` | `limit=2000` バグ解消 → DB 直接値 `samples_done` を表示 |
| `bacopy_master_ui.py` `renderLearning()` | プログレスバーも DB 直接値に変更 |
| `bacopy_master_ui.py` `_sendFriendAction()` | `overshoot`・`unit_idx` を `friend_action` に追加 |
| `bacopy_master_ui.py` `_learnSessionCheck()` | 自動 LOOK に `in_learning_session: true` + `overshoot`・`unit_idx` を追加 |
| `bacopy_db.py` `get_stats()` | 第1回・第2回を分けて集計、合算して返す |

---

## 7. 公式進捗カウントの定義

### 第2回（公式）

```
samples_done = BET(learning_round=2, done, ils=1) + LOOK(learning_round=2)
目標: 5,000 サンプル
```

### 参考値（第1回）

```
bets_done_r1 = BET(learning_round=1, done, ils=1, 4/30以降)
looks_done_r1 = LOOK(learning_round=1, 4/30以降)
```

---

## 8. 今後の課題

| 優先度 | 内容 |
|---|---|
| 🔴 高 | 第2回データが十分溜まったら OS 11+ の真の予想精度を計測する |
| 🔴 高 | OS 0〜10（非予想フェーズ）と OS 11+（予想フェーズ）の勝率を分離して比較 |
| 🟡 中 | スナップショットから特徴量を抽出（P/B 差・罫線パターンの数値化） |
| 🟡 中 | 特徴量と勝敗の相関分析（どの条件で友人の予想が当たるか） |
| 🟢 低 | 特徴量 → 勝敗を予測する分類モデルの学習 |

---

## 9. 第1回データ統計（参考）

| 指標 | 値 |
|---|---|
| 期間 | 2026-04-26 〜 2026-05-08 |
| 公式開始日 | 2026-04-30（ユーザー認識）|
| DB 上の最初の ils=1 | 2026-04-26 |
| BET 総数（4/30〜, ils=1） | 4,214 件 |
| BET done（4/30〜, ils=1） | 3,875 件 |
| LOOK（4/30〜） | 2,107 件 |
| 全体勝率（タイ除く） | 51.8%（非予想フェーズ混在） |
| BANKER BET 率 | 99.8% |
| 最高勝率テーブル | Baccarat 3: 61.2% |
| 最低勝率テーブル | Baccarat 1: 47.9% |
