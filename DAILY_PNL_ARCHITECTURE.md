# Daily PnL 計算アーキテクチャ

実装日: 2026-05-17

## 背景・問題

旧方式は `daily_pnl = current_balance - daily_open_balance`（残高差分）で計算していた。  
マスターがセッションを停止したあと、受け子ユーザーが勝ち利益をStakeから出金するのは自然な行動であり、
その場合 `current_balance` が下がり、正しい勝ちPnLが「損失」として記録される致命的な問題があった。

例:
```
当日開始残高: $10,000
BETで +$500 勝利 → 残高: $10,500
ユーザーが $1,000 を出金 → 残高: $9,500
cron 00:05 計算: $9,500 - $10,000 = -$500  ← 誤り（正しくは +$500）
```

## 新方式: `daily_bet_pnl` — ベット結果直接累積

### 原則

- BETの**勝敗確定ごと**に純損益を累積する
- 残高の増減は**一切参照しない**
- 出金・入金・ボーナスに完全に無影響

### 計算式（`apply_round()` 内）

```
バンカーBET勝利: daily_bet_pnl += bet_amount × 0.95  (Stake 5%手数料適用)
プレイヤーBET勝利: daily_bet_pnl += bet_amount
BET敗北: daily_bet_pnl -= bet_amount
引き分け(タイ)プッシュ: daily_bet_pnl += 0  (増減なし)
```

`bet_amount = seq[current_unit_idx] × chip_base`  
small系・newseq系は `chip_base=1.0` のため `bet_amount = seq値そのまま (USD)`

### JST日付管理 (深夜0時またぎ対応)

`apply_round()` 呼び出しごとに JST 日付を確認し、日付が変わったら:

```python
prev_daily_bet_pnl      = daily_bet_pnl       # 前日確定値を退避
prev_daily_bet_pnl_date = daily_bet_pnl_date
daily_bet_pnl           = 0.0                 # 当日を0リセット
daily_bet_pnl_date      = 新しいJST日付
```

**なぜ `prev_` が必要か:**  
cron は JST 00:05 に実行される。10時間セッションなどで 00:00〜00:05 の間に新しいBETが
入ると `daily_bet_pnl` が当日リセット済みになり、cron が $0 を読んでしまう。  
`prev_daily_bet_pnl_date == 昨日` を参照することでこのエッジケースを解決する。

### 長時間セッション・複数SEQリセット

`daily_bet_pnl` は SEQ リセット（利確・損切）とは**独立して累積**される。  
同一日に何度リセットが発生しても正確に集計される。

```
14:00-17:00 第1セッション: +$150 (利確リセット)
17:00-20:00 第2セッション: -$80  (損切リセット)
20:00-23:00 第3セッション: +$200 (利確リセット)
──────────────────────────────────
daily_bet_pnl = +$270  ← 全セッション通算
```

## データフロー

```
受け子 Executor (Seq7Session)
  │
  ├─ apply_round() 呼び出しごとに daily_bet_pnl 累積
  │   ├─ bet_side=="banker" and won → += bet_amount × 0.95
  │   ├─ won → += bet_amount
  │   ├─ lost → -= bet_amount
  │   └─ push (tie) → += 0
  │
  ├─ _save_state() でローカルファイルに永続化
  │   フィールド: daily_bet_pnl, daily_bet_pnl_date,
  │              prev_daily_bet_pnl, prev_daily_bet_pnl_date
  │
  └─ _schedule_session_state_sync_bafather() が60秒ごとにサーバーへPOST
      → billing.session_state に上書き保存

サーバー (web/src/app/api/cron/settle/route.ts) JST 00:05
  │
  ├─ 優先度1: session_state.daily_bet_pnl (当日分, date一致確認)
  ├─ 優先度2: session_state.prev_daily_bet_pnl (前日スナップショット, date一致確認)
  ├─ 優先度3: master executor heartbeat の balance-diff (フォールバック)
  └─ 優先度4: session_state の balance-diff (最終フォールバック)
  │
  └─ settleUser() で手数料計算・deduction記録・Telegram通知・紹介報酬
```

## 手数料・紹介報酬

- `billing.profit_share_rate` (%) に基づき operator fee を計算
- `billing.is_free = true` のユーザーは手数料なし・deduction なし
- `billing.balance = 0` のユーザーも settle 対象（carry_loss 記録のため）
- `LAPLACE_ENABLE_DYNAMIC_REFERRAL_SPLIT=true` で紹介者報酬が有効
- 紹介者報酬 = operator fee × `referrerShareRate` (デフォルト 20%)

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `bacopy_executor_pragmatic_ws_live.py` | `Seq7Session.apply_round()` で daily_bet_pnl 累積 |
| `bacopy_executor_pragmatic_ws_live.py` | `_schedule_session_state_sync_bafather()` で60秒ごとPOST |
| `web/src/app/api/session-state/route.ts` | session_state を billing テーブルに保存 |
| `web/src/app/api/cron/settle/route.ts` | JST 00:05 に daily_bet_pnl を最優先で使用して精算 |
| `web/.env.local` + Vercel env | `LAPLACE_ENABLE_DYNAMIC_REFERRAL_SPLIT=true` |

## 旧方式との比較

| 観点 | 旧方式 (balance-diff) | 新方式 (daily_bet_pnl) |
|------|----------------------|----------------------|
| 出金影響 | **CRITICAL**: 誤計算 | 無影響 |
| 入金影響 | **CRITICAL**: 過大計上 | 無影響 |
| バンカー手数料 | 残高ベースで自動反映 | 0.95倍を明示適用 |
| 長時間セッション | SEQリセット後も残高から計算 | `prev_` でまたぎ対応 |
| 実装シンプルさ | シンプルだが不正確 | やや複雑だが正確 |

## 注意事項

- Vercel 本番環境にも `LAPLACE_ENABLE_DYNAMIC_REFERRAL_SPLIT=true` を設定すること
- 受け子GUIを**リビルド・再配布**しないと executor 側の変更が反映されない
- 旧バージョンの executor は `daily_bet_pnl` フィールドを送信しない
  → cron は自動的に優先度3/4 (balance-diff) にフォールバックする
- `daily_bet_pnl_date` が昨日と一致しない場合は daily_bet_pnl を使用しない
  (同日に複数台起動している場合の安全装置)
