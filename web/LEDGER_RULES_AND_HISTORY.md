# FX 運用家計簿 ルールと変更履歴

最終更新: 2026-05-10

このドキュメントは新しいセッションで Claude が家計簿関連の作業をする際の引継ぎ書。
仕様変更は必ずこのファイルにも履歴を追記すること。

---

## 1. システム全体像

### 1.1 関係者

| 役割 | 識別子 | 説明 |
|---|---|---|
| **運営** | `goldbenchan@gmail.com` | このシステムを運営する人 (= ユーザー本人) |
| **投資家 H さん** | `shojihashimoto0922@gmail.com` | $96,900 を運営に預けた投資家 (橋本さん) |
| J / K | (個人名) | 運営側の取り分受取者 |
| 会社内部留保 | (概念) | 運営の会社経費プール (K の兄 報酬 / 配当 / AI 開発費 等を統合) |

### 1.2 口座構成

```
Hさんの $96,900 投資
├── 1つめ口座 ($50,000)        ← Hさん自身が BET 用に運用してると認識
└── チャージ資金 ($49,000)
    ├── 2つめ口座 ($46,900)    ← 実は運営が裏で BET 運用 (Hさんに非開示)
    └── 別チャージ ($2,100)    ← 運営自己資金 (歴史的経費立替に使用、消化済)
```

**Hさんの認識**: 自分は 1つめで BET、チャージ資金 ($49,000) はただ預けているだけ。
**実態**: チャージ資金 = 運営が 2つめ口座として裏で BET 運用 (Hさんには非開示)。

---

## 2. 利益分配ルール (2026-05-10 確定)

| 口座 | H | J | K | 会社内部留保 | 計 |
|---|---|---|---|---|---|
| **1つめ** | 20% | 20% | 30% | 30% | 100% |
| **2つめ** | 0% | 20% | 30% | 50% | 100% |

- 2つめは Hさん取り分なし (= 全額運営側で按分)
- 「会社配当」「会社累計」→ **「会社内部留保」**に用語統一

---

## 3. Hさん補正期間 (2026-05-09〜進行中)

### 3.1 背景

当初設計では Hさんは毎日 **1つめ利益の 80%** を別チャージ口座へ自発入金すべきだった。
しかし 2026-05-08 まで実施されず、Hさんが過剰受取していた:

```
過剰受取累計 = 1つめ累計利益 × 80% = $14,641 × 0.80 = $11,712.80 (5/8 時点)
```

### 3.2 補正フェーズの運用

5/9 から:
- Hさん は 1つめ利益を毎日全額出金 → **即座に 100% を別チャージへ物理入金**
- Hさん の手取り は実質 $0 (補正期間中)

| 状態 | Hさん入金 | Hさん取り分 | 過剰返済進度 |
|---|---|---|---|
| 通常運用 (補正後) | 80% × P | 20% × P | (なし) |
| **補正期間 (現在)** | **100% × P** | **0** | **20% × P /day** |

### 3.3 補正完了見込み

過剰残 $11,712.80 ÷ ($4,000 × 20% = $800/day) ≒ **14.6 日**

5/9 進度:
- 5/8 過剰残: $11,712.80
- 5/9 返済: $800
- 5/9 過剰残: $10,912.80

---

## 4. 経費内訳ページ (2026-05-08 追加)

`/admin/ledger/expense-breakdown` で「会社内部留保 出金累計」の使途を時系列管理。

- 受取累計と内訳合計を常に一致させる (`company_breakdown_remaining = 0`)
- カテゴリ: 給与/報酬/会社配当/賃料/設備投資/AI 開発費/ソフトウェア/税金/通信費/交通費/その他
- DB: `ledger_company_expense_breakdown` テーブル

---

## 5. OPERATOR vs UNPAID $2,100 差 (歴史的特例)

OPERATOR.残利益 と UNPAID 合計 の差は **永久固定 $2,100**:

- 2026-05-06 に AI 開発費 / Kの兄 等を別チャージ ($2,100) から立て替えた一回きりの動き
- 今後は新規発生しない (補正期間後の正常運用では 1つめ 80% が直接 chargeBalance を補充する設計)
- UI: PROFIT LOCATION セクション下部に注記 (`expense_from_reserve > 0` のときのみ)

---

## 6. 毎日の運用フロー

### 6.1 ユーザー (運営) → Claude

毎日、以下のいずれかの形式で報告:

```
本日 (M/D):
- 1つめ純利益: $X
- 2つめ純利益: $Y
- (補正期間中なら) Hさん再入金: $X (= 1つめと同額)
- (通常運用なら) Hさん再入金: 0.80 × X
```

### 6.2 Claude が行う処理

1. **ledger SQL INSERT**:
   ```sql
   INSERT INTO ledger_account1_daily (..., daily_profit, investor_recharge) VALUES (..., X, X 補正期間または 0.80×X 通常);
   INSERT INTO ledger_account2_daily (..., daily_profit) VALUES (..., Y);
   ```

2. **bafather billing.balance 同期**:
   ```js
   // node + @supabase/supabase-js で
   const newBal = oldBal - (X * 0.80);
   await sb.from('billing').update({balance: newBal}).eq('user_id', '<hashimoto user_id>');
   ```
   `<hashimoto user_id>` = `ad6fdb57-4459-4a1f-9d03-d266bccede37`

3. **検証 SELECT** で `displayed_charge_balance` と `billing.balance` が一致することを確認

### 6.3 ledger と bafather billing が乖離していたら...

= 同期エラーのサイン。最近の入力漏れがないか調査。

---

## 7. DB スキーマ要点

### 7.1 主要テーブル

- `ledger_investors` (投資家マスタ)
- `ledger_account1_daily` (1つめ日次入力, **`investor_recharge` カラム 2026-05-10 追加**)
- `ledger_account2_daily` (2つめ日次入力)
- `ledger_expense_withdrawals` (経費出金イベント)
- `ledger_company_expense_breakdown` (会社内部留保 内訳, 2026-05-08 追加)
- `ledger_distribution_rules` (1つめ分配ルール、時系列管理)
- `ledger_reserve_funds` (別チャージ初期額管理)

### 7.2 view: `ledger_investor_summary`

集計値を全部出してくれる view。`/admin/ledger` ページが参照。

主要フィールド:
- `account1_total_profit`, `account2_total_profit`
- `investor_recharge_total`, `investor_net_received`, `investor_overpaid`
- `displayed_charge_balance`
- `j_share_total_pool`, `k_share_total_pool`, `company_share_total_pool` (統合プール)
- `j_unpaid_total`, `k_unpaid_total`, `company_unpaid_total` (統合プール基準未出金)
- `operator_net_profit`, `operator_remaining_profit`
- `company_total_merged`, `company_breakdown_total`, `company_breakdown_remaining`

### 7.3 bafather billing 連携

- table: `billing` (bafather 用)
- field: `billing.balance` ≡ ledger の `displayed_charge_balance`
- **手動同期** (毎日)
- 自動同期は未実装 (将来候補: Supabase trigger or API webhook)

---

## 8. 変更履歴

| 日付 | 変更内容 | ba/ コミット |
|---|---|---|
| 2026-05-08 | 経費内訳ページ追加 / 「会社累計」概念導入 (K兄/旧会社配当/AI開発費 統合) | `ad3a2ad` |
| 2026-05-08 | EXPENSE RECIPIENTS を 5 行→3 行に集約、UNPAID 計算更新 | (上記コミット内) |
| 2026-05-10 | 2つめ利益分配導入 (J20/K30/会社50) + 統合プール基準 + 用語統一「会社内部留保」 | `a3fd77f` |
| 2026-05-10 | $2,100 reserve 先払い分を歴史的特例として注記表示 | `db69632` |
| 2026-05-10 | `investor_recharge` カラム追加 + Hさん補正期間運用開始 | (本コミット) |
| 2026-05-10 | bafather `billing.balance` 手動同期運用開始 | (運用ルール変更、コード変更なし) |

---

## 9. 5/9 時点 検証値 (新セッション時の整合チェック用)

新しい Claude セッションで「数字合ってる?」と聞かれたら、以下の値が DB から返ってくることを確認:

```
account1_total_profit:    $18,641.00
account2_total_profit:    $16,712.00
investor_recharge_total:  $4,000.00     (5/9 補正期間 100% 入金)
investor_net_received:    $14,641.00    (= 粗出金 − 再入金)
investor_received_total:  $3,728.20     (= 1つめ累計 × 20% 本来取り分)
investor_overpaid:        $10,912.80    (= 14,641 − 3,728.20 補正残)
displayed_charge_balance: $38,087.20    (= 49,000 − 14,912.80 + 4,000)

j_share_total_pool:       $7,070.60
k_share_total_pool:       $10,605.90
company_share_total_pool: $13,948.30
j_unpaid_total:           $4,970.60
k_unpaid_total:           $8,605.90
company_unpaid_total:     $10,029.30

operator_net_profit:      $31,624.80
operator_remaining_profit:$25,705.80    (注: $2,100 歴史的特例で UNPAID 合計と差)

bafather billing.balance: $38,087.20    (= displayed_charge_balance と同値、手動同期)
```

---

## 10. 注意点 (技術的落とし穴)

- **CREATE OR REPLACE VIEW はカラム順序の制約で失敗する**。`DROP VIEW + CREATE VIEW` の方が確実
- **Supabase SQL Editor は日本語コメント内の括弧 `()` でパース失敗する**。マイグレーションは可能な限り英語コメント or コメント無しで実行
- **bafather.uk の Vercel deploy は ba/ リポのみ watch**。bacopy/ への変更は本番反映されない (memory: `feedback_vercel_deploy_repo`)
- 期待値テスト: `web/src/lib/ledger/verify.ts` (npx tsx で実行)
- 2つめ分配率は固定 (`ACCOUNT2_DISTRIBUTION` 定数 in `types.ts`)、1つめは `ledger_distribution_rules` テーブル (時系列管理)
